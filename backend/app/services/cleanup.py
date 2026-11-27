"""
Alert-state cleanup micro-service.

A lightweight background loop that periodically reconciles per-user dilution
alert state. Offering rows and cached summaries are shared ticker data populated
by AI sync jobs, so they are intentionally retained even when no user currently
tracks the ticker.

It is the safety net behind the synchronous per-ticker alert cleanup that
portfolio removal already performs (routers/portfolio.py ->
dilution_engine.purge_dilution_data). The heavy lifting is one set-based pass in
dilution_engine.sweep_untracked_dilution(); this module only schedules it.

Runs as an asyncio task inside the FastAPI process (started from main.startup),
so there is no separate daemon to deploy — it lives and dies with the API.
"""
from __future__ import annotations

import asyncio
import logging

from . import dilution_engine

logger = logging.getLogger(__name__)

# Sweep cadence. Stale alert state is non-urgent, so an hourly reconcile is
# plenty and keeps DB load negligible.
_CLEANUP_INTERVAL_SECONDS = 60 * 60
# Small initial delay so the first sweep doesn't contend with startup warm-up
# (ticker cache, table creation) and any in-flight portfolio sync.
_INITIAL_DELAY_SECONDS = 120


async def run_cleanup_loop() -> None:
    """Infinite loop: sweep stale dilution alert state every _CLEANUP_INTERVAL_SECONDS."""
    logger.info(
        f"🧹 Dilution alert cleanup service started; first sweep in "
        f"{_INITIAL_DELAY_SECONDS}s, then every {_CLEANUP_INTERVAL_SECONDS // 60} min."
    )
    await asyncio.sleep(_INITIAL_DELAY_SECONDS)
    while True:
        try:
            summary = await dilution_engine.sweep_untracked_dilution()
            if summary["offerings"] or summary["alerts"] or summary["summaries"]:
                logger.info(
                    f"🧹 cleanup sweep purged alert state: alerts={summary['alerts']} "
                    f"across {summary['ciks']} untracked CIK(s)."
                )
            else:
                logger.debug("🧹 cleanup sweep: nothing stale.")
        except Exception as exc:  # noqa: BLE001
            # Never let a transient DB error kill the loop.
            logger.error(f"🧹 cleanup sweep failed: {exc!r}")
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)


def start_cleanup_service() -> None:
    """Fire-and-forget: schedule the cleanup loop on the running event loop."""
    asyncio.create_task(run_cleanup_loop())
