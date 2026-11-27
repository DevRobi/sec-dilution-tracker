"""
Daily AI dilution extraction for the preload ticker universe.

Uses smart_dilution_extractor + Claude (claude-code CLI or Anthropic API), or Ollama (opt-in).
Writes offerings + dilution summaries to Postgres.

Local:
  ./scripts/run_ai_dilution_sync.sh --ticker POET

Cloud: Render cron with ENABLE_AI_EXTRACTION=true, DILUTION_LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY.
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
from app.services import dilution_engine, securities, sync
from app.services.dilution_engine import _ai_extraction_enabled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app.jobs.ai_dilution_daily_sync")


def _delay_seconds() -> float:
    raw = os.getenv("AI_DILUTION_DELAY_SECONDS", "15")
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid AI_DILUTION_DELAY_SECONDS=%r; using 15.", raw)
        return 15.0


def _max_filings() -> int:
    raw = os.getenv("AI_DILUTION_MAX_FILINGS", "12")
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


async def _ensure_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _resolve_ticker(ticker: str) -> tuple[str, str] | None:
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


async def _ensure_company_shell(ticker: str, cik: str, title: str) -> None:
    """Create a company row if non-AI cron has not populated it yet."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(models.Company).where(models.Company.cik == cik)
        )
        if result.scalars().first():
            return
        session.add(models.Company(cik=cik, ticker=ticker, title=title))
        await session.commit()


async def run_ai_dilution_sync(tickers: list[str] | None = None) -> int:
    if not _ai_extraction_enabled():
        logger.error(
            "ENABLE_AI_EXTRACTION is disabled. Set ENABLE_AI_EXTRACTION=true "
            "in .env for local AI runs or on the AI cron service in cloud."
        )
        return 1

    try:
        from smart_dilution_extractor.llm_providers import get_active_provider, llm_available

        if not llm_available():
            logger.error(
                "LLM provider is not configured. Default is Claude: install and log in to "
                "the Claude Code CLI (DILUTION_LLM_PROVIDER=claude-code), or set "
                "ANTHROPIC_API_KEY (DILUTION_LLM_PROVIDER=anthropic). For local Ollama set "
                "DILUTION_LLM_PROVIDER=ollama."
            )
            return 1
        from smart_dilution_extractor import config as _llm_config
        logger.info("AI dilution provider: %s model=%s", get_active_provider(), _llm_config.CLAUDE_MODEL)
    except Exception:
        logger.exception("Could not initialize dilution LLM provider.")
        return 1

    tickers = resolve_job_tickers(tickers)
    if not tickers:
        logger.error("No tickers to sync (set PRELOAD_TICKERS or pass --ticker).")
        return 1

    log_database_target()
    delay_seconds = _delay_seconds()
    max_filings = _max_filings()
    started_at = datetime.utcnow()

    logger.info(
        "Starting AI dilution sync for %d tickers (max_filings=%d): %s",
        len(tickers),
        max_filings,
        ", ".join(tickers),
    )
    await _ensure_tables()

    success_count = 0
    failure_count = 0
    for index, ticker in enumerate(tickers, start=1):
        ticker = ticker.upper()
        logger.info("[%d/%d] AI dilution for %s...", index, len(tickers), ticker)
        try:
            resolved = await _resolve_ticker(ticker)
            if not resolved:
                logger.error("[%s] Ticker not found in SEC mapping.", ticker)
                failure_count += 1
                continue

            cik, title = resolved
            await _ensure_company_shell(ticker, cik, title)

            async with AsyncSessionLocal() as session:
                has_stats = await session.execute(
                    select(models.Stats.cik).where(models.Stats.cik == cik)
                )
                if not has_stats.scalar_one_or_none():
                    logger.info("[%s] No stats row yet; running non-AI sync first.", ticker)
                    await sync.sync_company_data(ticker, cik)

            await dilution_engine.sync_ticker_dilution(
                ticker,
                cik,
                max_filings=max_filings,
                use_llm=True,
                fast_pass=False,
                require_tracked=False,
            )
            success_count += 1
            logger.info("[%s] AI dilution sync complete.", ticker)
        except Exception:
            failure_count += 1
            logger.exception("[%s] AI dilution sync crashed.", ticker)

        if index < len(tickers) and delay_seconds:
            await asyncio.sleep(delay_seconds)

    elapsed = (datetime.utcnow() - started_at).total_seconds()
    logger.info(
        "Finished AI dilution sync: success=%d failure=%d elapsed=%.1fs",
        success_count,
        failure_count,
        elapsed,
    )
    return 0 if failure_count == 0 else 1


def main() -> None:
    cli_tickers = parse_ticker_args(sys.argv[1:])
    raise SystemExit(asyncio.run(run_ai_dilution_sync(cli_tickers)))


if __name__ == "__main__":
    main()
