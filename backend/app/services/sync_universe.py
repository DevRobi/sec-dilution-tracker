import os

from sqlalchemy import func, select

from app import models
from app.database import AsyncSessionLocal


# Initial test universe. Keep this intentionally small while onboarding test
# users; expand it once the daily worker and database path are stable.
DEFAULT_PRELOAD_TICKERS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "BRK-B",
    "JPM",
    "XOM",
    "UNH",
    "V",
    "MA",
    "AVGO",
    "LLY",
    "COST",
    "HD",
    "PG",
    "NKE",
    "POET",
]


def get_preload_tickers() -> list[str]:
    """
    Return the non-AI sync universe.

    PRELOAD_TICKERS lets deployment override the placeholder list without a code
    change, e.g. PRELOAD_TICKERS=AAPL,NVDA,MSFT.
    """
    raw = os.getenv("PRELOAD_TICKERS", "").strip()
    if not raw:
        return DEFAULT_PRELOAD_TICKERS

    tickers: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        ticker = part.strip().upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def should_seed_ticker_universe() -> bool:
    """
    Seed searchable ticker shells on web startup.

    This keeps autocomplete useful immediately after a fresh deploy. The daily
    cron can still fill heavier stats/history/price data separately.
    """
    return os.getenv("SEED_TICKER_UNIVERSE", "true").lower() in {"1", "true", "yes", "on"}


async def seed_company_universe(companies: list[dict], minimum_existing: int = 1000) -> int:
    """
    Ensure the DB has a searchable company universe.

    `companies` is the SEC ticker cache shape from securities.get_tickers().
    Returns the number of inserted company rows. Existing company rows are left
    alone so cron-populated data/titles are not churned on every cold start.
    """
    if not companies or not should_seed_ticker_universe():
        return 0

    async with AsyncSessionLocal() as session:
        count_result = await session.execute(select(func.count()).select_from(models.Company))
        existing_count = int(count_result.scalar_one() or 0)
        if existing_count >= minimum_existing:
            return 0

        cik_result = await session.execute(select(models.Company.cik))
        existing_ciks = {row[0] for row in cik_result.all()}

        new_rows = []
        seen_new_ciks = set(existing_ciks)
        for company in companies:
            cik = str(company.get("cik_str", "")).zfill(10)
            ticker = str(company.get("ticker", "")).upper().strip()
            title = str(company.get("title", "")).strip() or ticker
            if not cik or not ticker or cik in seen_new_ciks:
                continue
            new_rows.append(models.Company(cik=cik, ticker=ticker, title=title))
            seen_new_ciks.add(cik)

        if not new_rows:
            return 0

        session.add_all(new_rows)
        await session.commit()
        return len(new_rows)


async def get_portfolio_ciks() -> set[str]:
    """CIKs currently held in at least one user's portfolio."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(models.PortfolioItem.cik).distinct())
        return {row[0] for row in result.all() if row[0]}


async def get_retained_dilution_ciks() -> set[str]:
    """CIKs whose dilution rows must be kept (portfolio + preload universe)."""
    portfolio_ciks = await get_portfolio_ciks()
    preload_ciks = await get_preload_ciks()
    return portfolio_ciks | preload_ciks


async def get_preload_ciks() -> set[str]:
    """
    CIKs for the configured preload ticker universe.

    Used by dilution cleanup so preloaded (but not yet portfolio-added) tickers
    keep their offerings rows.
    """
    tickers = get_preload_tickers()
    if not tickers:
        return set()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(models.Company.cik).where(models.Company.ticker.in_(tickers))
        )
        return {row[0] for row in result.all() if row[0]}
