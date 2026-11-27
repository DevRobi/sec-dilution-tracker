"""
Daily non-AI sync: prices, share counts, filing links → Postgres.

Local (Postgres via docker compose):
  ./scripts/run_non_ai_sync.sh
  ./scripts/run_non_ai_sync.sh --ticker POET

Cloud: Render cron `python -m app.jobs.non_ai_daily_sync` (worker image).
"""
from app.jobs.bootstrap import configure_job_environment

configure_job_environment()

import asyncio
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import select

from app.database import Base, AsyncSessionLocal, engine
from app import models
from app.jobs.bootstrap import (
    log_database_target,
    parse_ticker_args,
    resolve_job_tickers,
)
from app.services import securities, sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app.jobs.non_ai_daily_sync")


def _sync_delay_seconds() -> float:
    raw = os.getenv("NON_AI_SYNC_DELAY_SECONDS", "3")
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid NON_AI_SYNC_DELAY_SECONDS=%r; using 3 seconds.", raw)
        return 3.0


async def _ensure_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _resolve_ticker(ticker: str) -> tuple[str, str] | None:
    """
    Return (cik, title) for a ticker from the local DB or warmed SEC ticker cache.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(models.Company.cik, models.Company.title).where(
                models.Company.ticker == ticker
            )
        )
        row = result.first()
        if row:
            return row[0], row[1] or ticker

    mapping = await securities.get_tickers()
    for company in mapping:
        if str(company.get("ticker", "")).upper() == ticker:
            cik = str(company["cik_str"]).zfill(10)
            title = company.get("title") or ticker
            return cik, title
    return None


async def run_non_ai_sync(tickers: list[str] | None = None) -> int:
    tickers = resolve_job_tickers(tickers)
    if not tickers:
        logger.error("PRELOAD_TICKERS is empty; nothing to sync.")
        return 1

    log_database_target()
    delay_seconds = _sync_delay_seconds()
    started_at = datetime.utcnow()

    logger.info(
        "Starting non-AI daily sync for %d tickers: %s",
        len(tickers),
        ", ".join(tickers),
    )
    await _ensure_tables()

    if not os.getenv("SEC_USER_AGENT", "").strip():
        logger.warning("SEC_USER_AGENT is not set; SEC requests may fail.")
    if not os.getenv("POLYGON_API_KEY", "").strip():
        logger.warning("POLYGON_API_KEY is not set; price history may be incomplete.")

    success_count = 0
    failure_count = 0
    for index, ticker in enumerate(tickers, start=1):
        ticker = ticker.upper()
        logger.info("[%d/%d] Syncing %s non-AI data...", index, len(tickers), ticker)
        try:
            resolved = await _resolve_ticker(ticker)
            if not resolved:
                logger.error("[%s] Ticker not found in SEC ticker mapping.", ticker)
                failure_count += 1
                continue

            cik, _title = resolved
            ok = await sync.sync_company_data(ticker, cik)
            if ok:
                success_count += 1
                logger.info("[%s] Non-AI sync complete.", ticker)
            else:
                failure_count += 1
                logger.error("[%s] Non-AI sync failed.", ticker)
        except Exception:
            failure_count += 1
            logger.exception("[%s] Non-AI sync crashed.", ticker)

        if index < len(tickers) and delay_seconds:
            await asyncio.sleep(delay_seconds)

    elapsed = (datetime.utcnow() - started_at).total_seconds()
    logger.info(
        "Finished non-AI daily sync: success=%d failure=%d elapsed=%.1fs",
        success_count,
        failure_count,
        elapsed,
    )
    return 0 if failure_count == 0 else 1


def main() -> None:
    cli_tickers = parse_ticker_args(sys.argv[1:])
    raise SystemExit(asyncio.run(run_non_ai_sync(cli_tickers)))


if __name__ == "__main__":
    main()
