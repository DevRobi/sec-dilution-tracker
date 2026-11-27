from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.future import select
from sqlalchemy import and_, func
from app.database import get_db
from app.models import Price, Stats
from app.services import adjusted_stats, spacs
from sqlalchemy.orm import selectinload
import time

router = APIRouter()
_rankings_cache = {}
_RANKINGS_TTL_SECONDS = 60

MARKET_CAP_BUCKETS = {
    "all": (None, None),
    "mega": (200_000_000_000, None),         # >= $200B
    "large": (10_000_000_000, 200_000_000_000),  # $10B - $200B
    "mid": (2_000_000_000, 10_000_000_000),      # $2B - $10B
    "small": (300_000_000, 2_000_000_000),       # $300M - $2B
    "micro": (50_000_000, 300_000_000),          # $50M - $300M
    "nano": (0, 50_000_000),                     # < $50M
}

# Sanity caps on the (split-adjusted) change. Kept as a last guard against
# residual bad data; adjusted rows beyond them are almost always data errors.
BUYBACK_FLOOR_PCT = -50.0
DILUTION_CAP_PCT = 500.0
MIN_SHARES = 1_000_000


def rank_rows(rows, universe, kind: str, years: int, limit: int, eff_min=None, eff_max=None,
              include_spacs: bool = False) -> list[dict]:
    """
    rows: (Stats, latest_price) pairs; universe: cik -> CikStats (split-adjusted).
    Pure function so it can be tested on a fixture DB.
    """
    out = []
    for s, latest_price in rows:
        if not s.company:
            continue
        spac = spacs.is_spac(s.cik, s.company.title)
        if spac and not include_spacs:
            continue  # trust redemptions / de-SPAC share issuance are neither buybacks nor dilution
        adj = universe.get(s.cik)
        if adj is None or not adj.changes:
            continue
        change = adj.change(years)
        latest = adj.changes.get("latest_shares")
        if change is None or not latest or latest < MIN_SHARES:
            continue
        if kind == "buyback" and not (BUYBACK_FLOOR_PCT < change < 0):
            continue
        if kind == "dilution" and not (0 < change <= DILUTION_CAP_PCT):
            continue
        market_cap = float(latest) * float(latest_price) if latest_price is not None else None
        if eff_min is not None or eff_max is not None:
            if market_cap is None:
                continue
            if eff_min is not None and market_cap < float(eff_min):
                continue
            if eff_max is not None and market_cap >= float(eff_max):
                continue
        flags = adj.window_flags(years)
        applied = adj.splits_in_window(years)
        out.append({
            "ticker": s.company.ticker,
            "title": s.company.title,
            "cik": s.cik,
            "change_pct": change,
            "raw_change_pct": getattr(s, f"change_pct_{years}y"),
            "start_date": adj.changes.get(f"date_{years}y"),
            "latest_shares": latest,
            "last_updated": s.last_updated,
            "market_cap": market_cap,
            "split_adjusted": bool(applied),
            "spac": spac,
            "adjustments": [a.label() for a in applied],
            "flags": [{"kind": f["kind"], "label": adjusted_stats.UNRELIABLE[f["kind"]],
                       "date": f["date"], "detail": f["detail"]} for f in flags],
        })
    out.sort(key=lambda r: r["change_pct"], reverse=(kind == "dilution"))
    return out[:limit]


@router.get("/rankings")
async def get_rankings(
    type: str = Query(..., pattern="^(buyback|dilution)$"),
    years: int = Query(1, ge=1, le=10),
    limit: int = Query(50, ge=1, le=200),
    market_cap_bucket: str = Query("all", pattern="^(all|mega|large|mid|small|micro|nano)$"),
    min_cap: float | None = Query(None, ge=0),  # explicit lower bound ($), None = no min
    max_cap: float | None = Query(None, ge=0),  # explicit upper bound ($), None = no max
    include_spacs: bool = Query(False, description="Include SPACs (blank-check companies, SIC 6770)"),
    db = Depends(get_db)
):
    """
    Companies ranked by SPLIT-ADJUSTED share-count change.
    type=buyback -> most negative first; type=dilution -> most positive first.

    Each row carries `raw_change_pct` (unadjusted, as stored), `adjustments`
    (splits restated inside the window) and `flags` (issues that still make the
    change unreliable after adjustment, e.g. an unconfirmed split at the latest
    filing). Market-cap filtering: explicit min_cap / max_cap (dollars) win over
    the legacy market_cap_bucket.
    """
    cache_key = f"{type}:{years}:{limit}:{market_cap_bucket}:{min_cap}:{max_cap}:{include_spacs}"
    cached = _rankings_cache.get(cache_key)
    if cached:
        expires_at, value = cached
        if time.monotonic() < expires_at:
            return value
        _rankings_cache.pop(cache_key, None)

    if not hasattr(Stats, f"change_pct_{years}y"):
        raise HTTPException(status_code=400, detail="Invalid year range")

    latest_price_date_sq = (
        select(Price.cik.label("cik"), func.max(Price.date).label("max_date"))
        .group_by(Price.cik)
        .subquery()
    )
    latest_price_sq = (
        select(Price.cik.label("cik"), Price.price.label("price"))
        .join(latest_price_date_sq, and_(
            Price.cik == latest_price_date_sq.c.cik,
            Price.date == latest_price_date_sq.c.max_date,
        ))
        .subquery()
    )
    query = (
        select(Stats, latest_price_sq.c.price)
        .options(selectinload(Stats.company))
        .outerjoin(latest_price_sq, latest_price_sq.c.cik == Stats.cik)
    )
    rows = (await db.execute(query)).all()
    universe = await adjusted_stats.get_universe()

    b_min, b_max = MARKET_CAP_BUCKETS.get(market_cap_bucket, (None, None))
    eff_min = min_cap if min_cap is not None else b_min
    eff_max = max_cap if max_cap is not None else b_max

    response = rank_rows(rows, universe, type, years, limit, eff_min, eff_max, include_spacs)
    _rankings_cache[cache_key] = (time.monotonic() + _RANKINGS_TTL_SECONDS, response)
    return response
