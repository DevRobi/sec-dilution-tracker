import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import database, models, schemas
from ..services import adjusted_stats, scheduler, securities, sync

router = APIRouter(
    prefix="/tickers",
    tags=["tickers"]
)

logger = logging.getLogger(__name__)

# Lightweight in-memory response cache (single-worker friendly).
_response_cache: dict[str, tuple[float, Any]] = {}
_cik_cache: dict[str, tuple[float, str]] = {}

_CACHE_TTL = {
    "company": 300,
    "stats": 120,
    "history": 3600,
    "price": 300,
    "market_cap": 300,
    "offerings": 5,
    "batch_stats": 60,
    "cik": 3600,
}


def _cache_get(key: str):
    entry = _response_cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _response_cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: int):
    _response_cache[key] = (time.monotonic() + ttl_seconds, value)


def _invalidate_ticker_cache(ticker: str):
    prefix = f"{ticker.upper()}:"
    keys = [k for k in _response_cache if k.startswith(prefix)]
    for key in keys:
        _response_cache.pop(key, None)


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip().replace(" ", "T")
    if len(normalized) == 10:
        normalized = f"{normalized}T00:00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _serialize_company(company: models.Company) -> dict:
    return {"cik": company.cik, "ticker": company.ticker, "title": company.title}


def _serialize_sec_company(company: dict) -> dict:
    return {
        "cik": str(company.get("cik_str", "")).zfill(10),
        "ticker": str(company.get("ticker", "")).upper(),
        "title": company.get("title") or str(company.get("ticker", "")).upper(),
    }


async def _find_sec_company(ticker: str) -> Optional[dict]:
    ticker = ticker.upper()
    mapping = await securities.get_tickers()
    if not mapping:
        logger.error("SEC Ticker mapping is empty!")
        return None

    for company in mapping:
        if str(company.get("ticker", "")).upper() == ticker:
            return _serialize_sec_company(company)
    return None


async def _ensure_company_shell(ticker: str, db: AsyncSession) -> Optional[dict]:
    """
    Return a company row for a ticker, creating a lightweight DB shell from SEC
    metadata when the daily worker has not populated the ticker yet.
    """
    ticker = ticker.upper()
    result = await db.execute(select(models.Company).where(models.Company.ticker == ticker))
    company = result.scalars().first()
    if company:
        return _serialize_company(company)

    sec_company = await _find_sec_company(ticker)
    if not sec_company:
        return None

    result = await db.execute(select(models.Company).where(models.Company.cik == sec_company["cik"]))
    existing_cik_company = result.scalars().first()
    if existing_cik_company:
        # Some SEC tickers share a CIK (units/rights/classes). Preserve the
        # stored primary row, but let this request use the ticker the user chose.
        return sec_company

    new_company = models.Company(
        cik=sec_company["cik"],
        ticker=sec_company["ticker"],
        title=sec_company["title"],
    )
    db.add(new_company)
    try:
        await db.commit()
    except Exception as exc:  # e.g. read-only DB or a concurrent insert
        await db.rollback()
        logger.warning(f"Could not persist company shell for {ticker}: {exc!r}")
    return sec_company


def _serialize_stats(stats: models.Stats) -> dict:
    return {
        "cik": stats.cik,
        "latest_shares": stats.latest_shares,
        "change_pct_1q": stats.change_pct_1q,
        "change_pct_1y": stats.change_pct_1y,
        "change_pct_5y": stats.change_pct_5y,
        "dilution_summary": stats.dilution_summary,
    }


def _serialize_history(rows: List[models.History]) -> List[dict]:
    return [
        {
            "date": row.date,
            "shares": row.shares,
            "source": row.source,
            "source_url": row.source_url,
        }
        for row in rows
    ]


def _serialize_prices(rows: List[models.Price]) -> List[dict]:
    return [{"date": row.date, "price": row.price} for row in rows]


def _serialize_offerings(rows: List[models.Offering]) -> List[dict]:
    def display_filing(raw: str) -> str:
        if not raw:
            return raw
        # Internally we may store "<FORM>:<ACCESSION>" for uniqueness.
        return raw.split(":", 1)[0]

    return [
        {
            "date": row.date,
            "category": row.category,
            "text": row.text,
            "source_filing": display_filing(row.source_filing),
            "source_url": row.source_url,
            "amount": row.amount,
            "amount_text": row.amount_text,
            "status": row.status,
            "description": row.description,
        }
        for row in rows
    ]


@router.get("/search")
async def search_tickers_endpoint(
    q: str,
    response: Response,
    db: AsyncSession = Depends(database.get_db),
):
    """
    Autocomplete search over the preloaded database universe.

    Keeping this DB-only avoids suggesting tickers that would require slow
    request-time SEC/Polygon ingestion during the test-user rollout.
    """
    # Mark user as active to pause background sync
    scheduler.mark_user_active()
    
    # Add cache headers - search results can be cached for 5 minutes
    response.headers["Cache-Control"] = "public, max-age=300"
    
    query = q.strip().upper()
    if len(query) < 2:
        return []

    stmt = (
        select(models.Company)
        .where(
            (models.Company.ticker.ilike(f"{query}%"))
            | (models.Company.title.ilike(f"%{query}%"))
        )
        .order_by(models.Company.ticker)
        .limit(5)
    )
    result = await db.execute(stmt)
    db_results = [_serialize_company(company) for company in result.scalars().all()]
    if db_results:
        return db_results

    # Fallback for fresh/sparse deployments: autocomplete directly against the
    # SEC ticker cache without triggering a slow stats/history sync.
    sec_results = await securities.search_tickers(query, 5)
    return [_serialize_sec_company(company) for company in sec_results]

# --- Helper to get CIK ---
async def get_cik_for_ticker(ticker: str, session: AsyncSession) -> Optional[str]:
    ticker = ticker.upper()

    cik_cache_entry = _cik_cache.get(ticker)
    if cik_cache_entry and time.monotonic() < cik_cache_entry[0]:
        return cik_cache_entry[1]

    # Try DB first
    result = await session.execute(select(models.Company.cik).where(models.Company.ticker == ticker))
    cik = result.scalar_one_or_none()
    if cik:
        _cik_cache[ticker] = (time.monotonic() + _CACHE_TTL["cik"], cik)
        return cik
    
    # Try External
    mapping = await securities.get_tickers()
    if not mapping:
        logger.error("SEC Ticker mapping is empty!")
    
    for m in mapping:
        if m.get('ticker') == ticker:
             cik = str(m['cik_str']).zfill(10)
             _cik_cache[ticker] = (time.monotonic() + _CACHE_TTL["cik"], cik)
             return cik
    
    logger.warning(f"Ticker {ticker} not found in SEC mapping (len={len(mapping)}).")
    return None

# sync_company_data and sync_company_offerings moved to services.sync


@router.post("/{ticker}/sync")
async def sync_ticker(ticker: str, db: AsyncSession = Depends(database.get_db)):
    # Mark user as active to pause background scheduler
    scheduler.mark_user_active()

    if os.getenv("ALLOW_MANUAL_TICKER_SYNC", "false").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(
            status_code=403,
            detail="On-demand ticker sync is disabled. Use the scheduled non-AI data worker.",
        )
    
    ticker = ticker.upper()
    cik = await get_cik_for_ticker(ticker, db)
    if not cik:
        raise HTTPException(status_code=404, detail="Ticker not found in SEC database")
    
    # Manual/admin escape hatch: only run non-AI data sync. Dilution/AI extraction
    # belongs in a separate worker and is disabled for the current cloud rollout.
    success = await sync.sync_company_data(ticker, cik)
    
    if success:
        _invalidate_ticker_cache(ticker)
        return {"status": "complete", "message": f"Non-AI sync complete for {ticker}"}
    else:
        raise HTTPException(status_code=500, detail="Basic synchronization failed")

@router.get("/{ticker}", response_model=schemas.CompanyResponse)
async def get_ticker(ticker: str, response: Response, db: AsyncSession = Depends(database.get_db)):
    ticker = ticker.upper()
    cache_key = f"{ticker}:company"
    company_data = _cache_get(cache_key)
    if not company_data:
        company_data = await _ensure_company_shell(ticker, db)
        if not company_data:
             raise HTTPException(status_code=404, detail="Ticker not found in SEC database")
        _cache_set(cache_key, company_data, _CACHE_TTL["company"])
    # Cache for 5 minutes
    response.headers["Cache-Control"] = "public, max-age=300"
    return company_data

@router.get("/{ticker}/stats", response_model=schemas.StatsResponse)
async def get_ticker_stats(
    ticker: str,
    response: Response,
    db: AsyncSession = Depends(database.get_db)
):
    ticker = ticker.upper()
    cache_key = f"{ticker}:stats"
    stats_data = _cache_get(cache_key)
    if not stats_data:
        stmt = (
            select(models.Stats)
            .join(models.Company, models.Company.cik == models.Stats.cik)
            .where(models.Company.ticker == ticker)
        )
        result = await db.execute(stmt)
        stats = result.scalars().first()
        if not stats:
            raise HTTPException(status_code=404, detail="Stats unavailable")
        stats_data = _serialize_stats(stats)
        stats_data.update({
            "raw_change_pct_1q": stats.change_pct_1q,
            "raw_change_pct_1y": stats.change_pct_1y,
            "raw_change_pct_5y": stats.change_pct_5y,
            "last_date": stats.last_date,
            "date_1q": stats.date_1q,
            "date_1y": stats.date_1y,
            "date_5y": stats.date_5y,
        })
        # Replace the stored (unadjusted) changes with split-adjusted ones.
        adj = await adjusted_stats.get_ticker_series(ticker, stats.cik, db)
        c = adj.changes
        if c:
            for k in ("latest_shares", "change_pct_1q", "change_pct_1y", "change_pct_5y",
                      "last_date", "date_1q", "date_1y", "date_5y"):
                stats_data[k] = c.get(k)
        stats_data["split_source"] = adj.event_source
        stats_data["splits"] = [s.__dict__ | {"label": s.label()} for s in adj.series.splits]
        stats_data["data_issues"] = adj.series.issues
        _cache_set(cache_key, stats_data, _CACHE_TTL["stats"])

    response.headers["Cache-Control"] = "public, max-age=300"
    return stats_data

@router.get("/{ticker}/history", response_model=List[schemas.HistoryPoint])
async def get_ticker_history(ticker: str, response: Response, db: AsyncSession = Depends(database.get_db)):
    """Full share-count history, split-adjusted to the latest basis (newest first)."""
    ticker = ticker.upper()
    cache_key = f"{ticker}:history"
    cached = _cache_get(cache_key)
    if cached is not None:
        response.headers["Cache-Control"] = "public, max-age=300"
        return cached

    cik = await get_cik_for_ticker(ticker, db)
    if not cik:
        raise HTTPException(status_code=404, detail="Ticker not found in SEC database")

    adj = await adjusted_stats.get_ticker_series(ticker, cik, db)
    history_data = [p.__dict__.copy() for p in reversed(adj.series.points)]
    _cache_set(cache_key, history_data, _CACHE_TTL["history"])
    response.headers["Cache-Control"] = "public, max-age=300"
    return history_data

@router.get("/{ticker}/price", response_model=List[schemas.PricePoint])
async def get_ticker_prices(ticker: str, response: Response, db: AsyncSession = Depends(database.get_db)):
    ticker = ticker.upper()
    cache_key = f"{ticker}:price"
    cached = _cache_get(cache_key)
    if cached is not None:
        response.headers["Cache-Control"] = "public, max-age=300"
        return cached

    cik = await get_cik_for_ticker(ticker, db)
    if not cik:
        raise HTTPException(status_code=404, detail="Ticker not found in SEC database")

    cutoff = (datetime.utcnow() - timedelta(days=365*5)).strftime("%Y-%m-%d")
    
    stmt = select(models.Price).where(
        models.Price.cik == cik,
        models.Price.date >= cutoff
    ).order_by(models.Price.date.desc())
    
    result = await db.execute(stmt)
    price_data = _serialize_prices(result.scalars().all())
    _cache_set(cache_key, price_data, _CACHE_TTL["price"])
    # Cache for 5 minutes (price data updates more frequently)
    response.headers["Cache-Control"] = "public, max-age=300"
    return price_data

@router.get("/{ticker}/market_cap")
async def get_ticker_market_cap(ticker: str, response: Response, db: AsyncSession = Depends(database.get_db)):
    """
    Calculates historical market cap (Price * Shares) for the last 5 years.
    Returns a list of {date, market_cap}.
    """
    ticker = ticker.upper()
    cache_key = f"{ticker}:market_cap"
    cached = _cache_get(cache_key)
    if cached is not None:
        response.headers["Cache-Control"] = "public, max-age=300"
        return cached
    
    # Fetch Prices and History (pass response for cache headers)
    prices = await get_ticker_prices(ticker, response, db)
    history = await get_ticker_history(ticker, response, db)
    
    if not prices or not history:
        return []

    share_map: dict[datetime, int] = {}
    for point in history:
        if point.get("status") == "excluded":
            continue
        dt = _parse_timestamp(point.get("date"))
        shares = point.get("shares")
        if dt and shares and shares > 0:
            share_map[dt] = int(shares)

    if not share_map:
        return []

    share_points = sorted(share_map.items(), key=lambda x: x[0])
    first_shares = share_points[0][1]

    price_points = []
    for point in prices:
        dt = _parse_timestamp(point.get("date"))
        price = point.get("price")
        if dt and price is not None:
            price_points.append((dt, float(price)))
    price_points.sort(key=lambda x: x[0])

    if not price_points:
        return []

    results = []
    share_idx = -1
    for price_dt, price in price_points:
        while share_idx + 1 < len(share_points) and share_points[share_idx + 1][0] <= price_dt:
            share_idx += 1
        shares = share_points[share_idx][1] if share_idx >= 0 else first_shares
        if shares <= 0:
            continue
        results.append(
            {
                "date": price_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "market_cap": float(price * shares),
                "price": float(price),
                "shares": int(shares),
            }
        )

    results.sort(key=lambda x: x["date"], reverse=True)
    _cache_set(cache_key, results, _CACHE_TTL["market_cap"])
    # Cache for 5 minutes
    response.headers["Cache-Control"] = "public, max-age=300"
    return results

@router.get("/{ticker}/offerings", response_model=List[schemas.OfferingResponse])
async def get_ticker_offerings(ticker: str, response: Response, db: AsyncSession = Depends(database.get_db)):
    ticker = ticker.upper()
    cache_key = f"{ticker}:offerings"
    cached = _cache_get(cache_key)
    if cached is not None:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return cached

    cik = await get_cik_for_ticker(ticker, db)
    if not cik:
        raise HTTPException(status_code=404, detail="Ticker not found in SEC database")

    # Only surface dilution (active or potential) from the last 2 years — older
    # offerings add noise and rarely reflect a company's current dilution stance.
    # Dates are stored as ISO "YYYY-MM-DD" strings, so a lexical >= comparison is
    # a correct chronological filter.
    cutoff_date = (datetime.utcnow() - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    stmt = (
        select(models.Offering)
        .where(models.Offering.cik == cik)
        .where(models.Offering.date >= cutoff_date)
        .order_by(models.Offering.date.desc())
    )
    result = await db.execute(stmt)
    offerings_data = _serialize_offerings(result.scalars().all())
    _cache_set(cache_key, offerings_data, _CACHE_TTL["offerings"])
    # No cache for offerings to ensure immediate updates after background sync
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return offerings_data

@router.post("/batch/stats")
async def get_batch_stats(tickers: List[str], db: AsyncSession = Depends(database.get_db)):
    """
    Fetch stats for multiple tickers in ONE database call.
    Eliminates N+1 problem for portfolio loading.
    """
    tickers = sorted(t.upper() for t in tickers)
    cache_key = f"{':'.join(tickers)}:batch_stats"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    
    stmt = (
        select(models.Company.ticker, models.Stats)
        .join(models.Stats, models.Stats.cik == models.Company.cik)
        .where(models.Company.ticker.in_(tickers))
    )
    result = await db.execute(stmt)
    rows = result.all()
    
    universe = adjusted_stats.peek_universe() or {}
    stats_map = {}
    for ticker, stats in rows:
        stats_map[ticker] = adjusted_stats.adjusted_stats_dict(universe.get(stats.cik), {
            "latest_shares": stats.latest_shares,
            "change_pct_1q": stats.change_pct_1q,
            "change_pct_1y": stats.change_pct_1y,
            "change_pct_5y": stats.change_pct_5y,
            "last_date": stats.last_date,
        })
    
    _cache_set(cache_key, stats_map, _CACHE_TTL["batch_stats"])
    return stats_map
