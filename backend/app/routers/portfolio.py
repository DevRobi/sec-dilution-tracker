from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List
from .. import database, models, schemas
from .auth import get_current_user
from ..services import adjusted_stats, sync, securities, dilution_engine
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/portfolio",
    tags=["portfolio"]
)

@router.get("/", response_model=List[schemas.CompanyResponse])
async def get_portfolio(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(database.get_db)
):
    # Fetch user's portfolio items and join with Company
    stmt = select(models.Company).distinct().join(models.PortfolioItem).where(models.PortfolioItem.username == current_user.username)
    result = await db.execute(stmt)
    return result.scalars().all()

@router.get("/full")
async def get_portfolio_with_stats(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(database.get_db)
):
    """
    Get portfolio with stats in ONE call - eliminates N+1 problem.
    Returns companies with their stats pre-joined.
    Auto-triggers AI summary generation for stocks missing summaries.
    """
    # Single query that joins Company with Stats
    stmt = (
        select(models.Company, models.Stats)
        .distinct()
        .join(models.PortfolioItem, models.PortfolioItem.cik == models.Company.cik)
        .outerjoin(models.Stats, models.Stats.cik == models.Company.cik)
        .where(models.PortfolioItem.username == current_user.username)
    )
    result = await db.execute(stmt)
    rows = result.all()
    
    portfolio = []
    stocks_needing_summary = []
    universe = await adjusted_stats.get_universe()  # split-adjusted changes
    
    for company, stats in rows:
        # Check if this stock needs AI summary generation
        needs_summary = stats is None or stats.dilution_summary is None
        if needs_summary:
            stocks_needing_summary.append((company.ticker, company.cik))
        
        item = {
            "cik": company.cik,
            "ticker": company.ticker,
            "title": company.title,
            "stats": adjusted_stats.adjusted_stats_dict(universe.get(company.cik), {
                "latest_shares": stats.latest_shares,
                "change_pct_1q": stats.change_pct_1q,
                "change_pct_1y": stats.change_pct_1y,
                "change_pct_5y": stats.change_pct_5y,
                "last_date": stats.last_date,
                "dilution_summary": stats.dilution_summary,
            }) if stats else {}
        }
        portfolio.append(item)
    
    # DO NOT auto-trigger sync here. It's too heavy for a GET list request.
    # Sync should be triggered explicitly by the user or by a background scheduler.
    
    return portfolio

@router.get("/alerts", response_model=List[schemas.OfferingWithTickerResponse])
async def get_portfolio_alerts(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(database.get_db)
):
    """
    Get alerts (dilutive offerings) for the last 30 days for all companies in the portfolio.
    """
    cutoff_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    stmt = (
        select(models.Offering, models.Company.ticker)
        .join(models.Company, models.Offering.cik == models.Company.cik)
        .join(models.PortfolioItem, models.PortfolioItem.cik == models.Company.cik)
        .where(
            models.PortfolioItem.username == current_user.username,
            models.Offering.date >= cutoff_date
        )
        .order_by(models.Offering.date.desc())
    )
    
    result = await db.execute(stmt)
    rows = result.all()
    
    alerts = []
    for offering, ticker in rows:
        # Pydantic's from_orm/model_validate will try to map fields.
        # Since logic is slightly complex (injecting ticker), let's construct dict or basic obj
        # offering is an ORM object.
        alert_dict = {
            "date": offering.date,
            "category": offering.category,
            "text": offering.text,
            "source_filing": (offering.source_filing.split(":", 1)[0] if offering.source_filing else offering.source_filing),
            "source_url": offering.source_url,
            "amount": offering.amount,
            "amount_text": offering.amount_text,
            "ticker": ticker
        }
        alerts.append(alert_dict)
        
    return alerts

@router.post("/{ticker}")
async def add_to_portfolio(
    ticker: str,
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(database.get_db)
):
    ticker = ticker.upper()
    # Find company
    stmt = select(models.Company).where(models.Company.ticker == ticker)
    result = await db.execute(stmt)
    company = result.scalars().first()
    
    if not company:
        raise HTTPException(status_code=404, detail="Company not found. Please sync it first.")

    # Check if already in portfolio
    stmt = select(models.PortfolioItem).where(
        (models.PortfolioItem.username == current_user.username) &
        (models.PortfolioItem.cik == company.cik)
    )
    existing = await db.execute(stmt)
    if existing.scalars().first():
        return {"message": f"{ticker} is already in your portfolio"}

    # Add
    item = models.PortfolioItem(username=current_user.username, cik=company.cik)
    db.add(item)
    await db.commit()
    
    # Do not trigger SEC/Polygon/AI work from a user request. The daily non-AI
    # worker preloads the test universe into Postgres, and the frontend reads
    # those tables directly.
    logger.info(f"Added preloaded ticker to portfolio without request-time sync: {ticker}")

    return {"message": f"Added {ticker} to portfolio."}

@router.delete("/{ticker}")
async def remove_from_portfolio(
    ticker: str,
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(database.get_db)
):
    ticker = ticker.upper()
    # Find company CIK to delete
    stmt = select(models.Company).where(models.Company.ticker == ticker)
    res = await db.execute(stmt)
    company = res.scalars().first()
    
    if not company:
         raise HTTPException(status_code=404, detail="Company not found")

    await db.execute(
        models.PortfolioItem.__table__.delete().where(
            (models.PortfolioItem.username == current_user.username) &
            (models.PortfolioItem.cik == company.cik)
        )
    )
    await db.commit()

    # If no user tracks this ticker anymore, purge only per-user alert state.
    # Offering rows and summaries are shared AI-sync output and must remain
    # available on ticker/search pages.
    if not await sync.is_ticker_tracked(company.cik):
        removed_offerings, removed_alerts = await dilution_engine.purge_dilution_data(company.cik)
        logger.info(
            f"Purged dilution alert state for {ticker} (untracked): "
            f"offerings={removed_offerings}, alerts={removed_alerts}"
        )

    return {"message": f"Removed {ticker} from portfolio"}
