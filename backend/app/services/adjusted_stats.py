"""
Split-adjusted share-count statistics, computed in memory.

The database is served read-only and its `stats` table holds changes computed
on unadjusted history, so the API restates history on the fly instead:

* ``get_ticker_series``  - one ticker, with yfinance split events (disk-cached),
  used by the ticker page (history, chart, 1Q/1Y/5Y metrics, market cap).
* ``get_universe``       - every CIK, built once from the `history` table
  (~223k rows, a few seconds) using series-based detection plus any yfinance
  events already on disk. Used by rankings, portfolio and batch stats.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from .. import database, models
from . import splits

logger = logging.getLogger(__name__)

TICKER_TTL_SECONDS = 3600
UNIVERSE_TTL_SECONDS = 6 * 3600

# Issue kinds that make a % change unreliable even after adjustment, with the
# short badge label the UI shows.
UNRELIABLE = {
    "split_unconfirmed": "Unconfirmed split",
    "split_approximate": "Approx. split",
    "unexplained_jump": "Jump",
}


@dataclass
class CikStats:
    cik: str
    ticker: str | None
    series: splits.AdjustedSeries
    changes: dict
    event_source: str  # "yfinance" | "detected"

    def change(self, years: int) -> float | None:
        return self.changes.get(f"change_pct_{years}y")

    def window_flags(self, years: int) -> list[dict]:
        start = self.changes.get(f"date_{years}y")
        end = self.changes.get("last_date")
        return [i for i in splits.issues_in_window(self.series, start, end) if i["kind"] in UNRELIABLE]

    def splits_in_window(self, years: int) -> list[splits.AppliedSplit]:
        start = self.changes.get(f"date_{years}y")
        end = self.changes.get("last_date")
        if not start or not end:
            return []
        return [s for s in self.series.splits if start < s.effective_date <= end]


# ---------------------------------------------------------------------------
# Per-ticker (ticker page)
# ---------------------------------------------------------------------------
_ticker_cache: dict[str, tuple[float, CikStats]] = {}


async def _load_rows(db, cik: str) -> list[dict]:
    res = await db.execute(
        select(models.History.date, models.History.shares, models.History.source,
               models.History.source_url, models.History.id)
        .where(models.History.cik == cik)
        .order_by(models.History.date, models.History.id))
    return [{"date": d, "shares": s, "source": src, "source_url": url} for d, s, src, url, _ in res.all()]


async def get_ticker_series(ticker: str, cik: str, db) -> CikStats:
    key = ticker.upper()
    hit = _ticker_cache.get(key)
    if hit and time.monotonic() - hit[0] < TICKER_TTL_SECONDS:
        return hit[1]
    rows = await _load_rows(db, cik)
    events = await asyncio.to_thread(splits.fetch_split_events, key)
    series = splits.adjust_series(rows, events or None)
    stats = CikStats(cik, key, series, splits.compute_changes(series.points),
                     "yfinance" if events else "detected")
    _ticker_cache[key] = (time.monotonic(), stats)
    return stats


# ---------------------------------------------------------------------------
# Whole universe (rankings / portfolio)
# ---------------------------------------------------------------------------
_universe: dict[str, CikStats] | None = None
_universe_built_at = 0.0
_universe_lock = asyncio.Lock()


def build_universe(rows: list[tuple], tickers: dict[str, str]) -> dict[str, CikStats]:
    """rows: (cik, date, shares, source) ordered by cik, date. Pure; used by tests."""
    by_cik: dict[str, list[dict]] = defaultdict(list)
    for cik, d, s, src in rows:
        by_cik[cik].append({"date": d, "shares": s, "source": src})
    out: dict[str, CikStats] = {}
    for cik, pts in by_cik.items():
        ticker = tickers.get(cik)
        events = splits.read_cached_events(ticker) if ticker else None  # disk only, no network
        series = splits.adjust_series(pts, events or None)
        out[cik] = CikStats(cik, ticker, series, splits.compute_changes(series.points),
                            "yfinance" if events else "detected")
    return out


async def get_universe(force: bool = False) -> dict[str, CikStats]:
    global _universe, _universe_built_at
    if _universe is not None and not force and time.monotonic() - _universe_built_at < UNIVERSE_TTL_SECONDS:
        return _universe
    async with _universe_lock:
        if _universe is not None and not force and time.monotonic() - _universe_built_at < UNIVERSE_TTL_SECONDS:
            return _universe
        t0 = time.time()
        async with database.AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(models.History.cik, models.History.date, models.History.shares, models.History.source)
                .order_by(models.History.cik, models.History.date, models.History.id))).all()
            tickers = dict((await db.execute(select(models.Company.cik, models.Company.ticker))).all())
        _universe = await asyncio.to_thread(build_universe, [tuple(r) for r in rows], tickers)
        _universe_built_at = time.monotonic()
        n_split = sum(1 for s in _universe.values() if s.series.splits)
        logger.info("split-adjusted universe: %d CIKs (%d with adjustments) from %d rows in %.1fs",
                    len(_universe), n_split, len(rows), time.time() - t0)
        return _universe


def peek_universe() -> dict[str, CikStats] | None:
    """The universe if already built (never blocks)."""
    return _universe


def reset_caches() -> None:
    global _universe, _universe_built_at
    _universe, _universe_built_at = None, 0.0
    _ticker_cache.clear()


def adjusted_stats_dict(s: CikStats | None, fallback: dict) -> dict:
    """Override the raw stats fields shown in lists with split-adjusted ones."""
    if s is None or not s.changes:
        return fallback
    c = s.changes
    return {
        **fallback,
        "latest_shares": c.get("latest_shares"),
        "change_pct_1q": c.get("change_pct_1q"),
        "change_pct_1y": c.get("change_pct_1y"),
        "change_pct_5y": c.get("change_pct_5y"),
        "last_date": c.get("last_date"),
        "split_adjusted": bool(s.series.splits),
    }
