"""
Shared environment setup for cron/CLI jobs (local Postgres + Render).

Import and call ``configure_job_environment()`` before any other ``app`` imports
so ``.env`` is loaded and ``DATABASE_URL`` is read correctly.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_CONFIGURED = False


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_dotenv_files() -> None:
    root = repo_root()
    backend = backend_dir()
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            "python-dotenv not installed; relying on shell environment only."
        )
        return

    load_dotenv(root / ".env")
    load_dotenv(backend / ".env", override=True)


def configure_job_environment() -> None:
    """Idempotent: PYTHONPATH, .env, and repo root for smart_dilution_extractor."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = repo_root()
    backend = backend_dir()

    backend_str = str(backend)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.append(root_str)

    os.environ.setdefault("REPO_ROOT", root_str)
    _load_dotenv_files()
    _CONFIGURED = True


def log_database_target() -> None:
    raw = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost/buyback_tracker",
    )
    if "sqlite" in raw.lower():
        logger.warning(
            "DATABASE_URL uses SQLite. Cron jobs and cloud deploy use Postgres; "
            "set DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/buyback_tracker "
            "(see .env.example and: docker compose up -d db)."
        )
    try:
        parts = urlsplit(raw.replace("postgresql+asyncpg://", "postgresql://", 1))
        host = parts.hostname or "(unknown)"
        db = (parts.path or "").lstrip("/") or "(unknown)"
        logger.info("Database target: host=%s db=%s", host, db)
    except Exception:
        logger.info("Database target configured (could not parse URL for log).")


def parse_ticker_args(argv: list[str] | None = None) -> list[str] | None:
    """
    Optional CLI override: --ticker POET --ticker AAPL  or  --tickers POET,AAPL.
    Returns None to use PRELOAD_TICKERS / default universe.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--tickers", default="", help="Comma-separated tickers")
    args, _unknown = parser.parse_known_args(argv)

    out: list[str] = []
    seen: set[str] = set()
    for t in args.ticker or []:
        ticker = str(t).strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            out.append(ticker)
    if args.tickers:
        for part in str(args.tickers).split(","):
            ticker = part.strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                out.append(ticker)
    return out or None


def resolve_job_tickers(cli_tickers: list[str] | None) -> list[str]:
    if cli_tickers:
        return cli_tickers
    from app.services.sync_universe import get_preload_tickers

    return get_preload_tickers()
