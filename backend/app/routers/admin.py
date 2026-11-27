"""
Admin / maintenance endpoints.

On-demand operational levers that mirror what the background services do
automatically. Currently exposes a manual trigger for the dilution alert-state
cleanup sweep (the same set-based reconcile the hourly cleanup micro-service
runs), so an operator can force a sweep without waiting for the next tick.
"""
import logging

from fastapi import APIRouter, Depends

from ..security.admin import require_admin_key
from ..services import dilution_engine

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


@router.post("/cleanup")
async def trigger_cleanup(_: None = Depends(require_admin_key)):
    """
    Manually run one alert-state sweep. Shared offering rows and cached
    summaries are retained; only stale alert-log rows are purged.
    Safe to call repeatedly — a no-op when nothing is stale.
    """
    summary = await dilution_engine.sweep_untracked_dilution()
    logger.info(f"[admin] manual cleanup sweep: {summary}")
    return {"status": "ok", "purged": summary}
