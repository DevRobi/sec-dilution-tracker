"""
Dilution engine adapter.

Bridges the standalone `smart_dilution_extractor` package (XBRL + Docling +
local-LLM hybrid) into the buyback tracker. This REPLACES the legacy
`securities.get_offering_text` path for companies added to a portfolio.

The extractor is synchronous (requests / Docling / Ollama all block), so we run
it in a worker thread via `asyncio.to_thread` and then upsert the resulting
events into the existing `Offering` table — preserving the Active/Potential
status column the UI already understands.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.future import select

from .. import database, models
from . import sync as legacy_sync

logger = logging.getLogger(__name__)

# --- make the sibling `smart_dilution_extractor` package importable ----------
# NOTE: append (not insert-at-0). The repo root also holds the legacy Streamlit
# `app.py`; inserting at the front would shadow the backend `app` package.
def _find_repo_root() -> Path:
    env = os.getenv("REPO_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "smart_dilution_extractor").is_dir():
            return parent
    return here.parents[3]


_REPO_ROOT = _find_repo_root()
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

# Only persist dilution (active or potential) from the last 2 years; older
# offerings rarely reflect a company's current dilution stance and just add
# noise. The /offerings API enforces the same window, but filtering here keeps
# stale events out of the DB entirely (and, combined with prune=True on the full
# pass, purges any previously-stored old rows).
_DILUTION_LOOKBACK_DAYS = 365 * 2


def _within_lookback(row: dict) -> bool:
    d = (row.get("date") or "").strip()
    if not d:
        return True  # undated event: can't time-filter, keep it
    cutoff = (datetime.utcnow() - timedelta(days=_DILUTION_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    return d >= cutoff


# Imported lazily inside the thread to keep module import cheap and resilient.
_running: set[str] = set()
_semaphore = asyncio.Semaphore(2)   # heavy (Docling+LLM); limit concurrency

# Master off-switch for the AI dilution extraction (XBRL is fine; this gates the
# Docling + local-LLM pipeline). Set ENABLE_AI_EXTRACTION=false in environments
# that don't ship the `smart_dilution_extractor` package / Ollama (e.g. the
# lightweight cloud deploy). When off, sync_portfolio_dilution no-ops, so adding
# a stock still fetches share-count history / prices via the legacy sync — it
# just skips Active/Potential dilution + AI summaries. Defaults ON for local dev.
def _ai_extraction_enabled() -> bool:
    return os.getenv("ENABLE_AI_EXTRACTION", "true").strip().lower() in (
        "1", "true", "yes", "on"
    )


# ---------------------------------------------------------------------------
# Event -> Offering mapping
# ---------------------------------------------------------------------------
def _fmt_amount(amount: float | None) -> str | None:
    if not amount:
        return None
    if amount >= 1e9:
        return f"${amount / 1e9:.2f}B"
    if amount >= 1e6:
        return f"${amount / 1e6:.1f}M"
    if amount >= 1e3:
        return f"${amount / 1e3:.0f}K"
    return f"${amount:,.0f}"


def _describe(ev) -> str:
    """Short human-readable description for the Offering row."""
    t = ev.offering_type.value if hasattr(ev.offering_type, "value") else str(ev.offering_type)
    shares = f"{ev.shares:,.0f}" if ev.shares else None
    amt = _fmt_amount(ev.gross_proceeds_usd)
    price = f"${ev.price_per_share:,.4f}/sh" if ev.price_per_share else None

    if t == "shelf_registration":
        return f"Shelf registration — capacity {amt}" if amt else "Open shelf registration (indeterminate capacity)"
    if t == "warrant_exercise":
        bits = [b for b in (shares and f"{shares} shares issuable", price) if b]
        label = "Rights/warrants" if "right" in (ev.source_quote or "").lower() else "Warrants/options"
        return f"{label} — " + (", ".join(bits) if bits else "outstanding")
    if t == "convertible":
        return f"Convertible — into {shares} shares" if shares else "Convertible security outstanding"

    # active offerings
    label = {
        "follow_on": "Follow-on offering", "atm": "ATM program",
        "private_placement": "Private placement", "ipo": "IPO",
        "rights_offering": "Rights offering", "equity_line": "Equity line",
        "other": "Equity issuance",
    }.get(t, t)
    bits = [b for b in (shares and f"{shares} shares", price, amt and f"{amt} proceeds") if b]
    return f"{label}" + (" — " + ", ".join(bits) if bits else "")


# Map the extractor's OfferingType -> the human-readable category labels the
# frontend's "Dilution History by Instrument" cards filter on. Those filters use
# CASE-SENSITIVE substring matches (ATM / Shelf / Convertible / Warrant / SAFE /
# Unit Rights / Series / Direct / SBC / Other), so writing raw enum values
# like 'warrant_exercise'/'shelf_registration' leaves every instrument card empty
# even though Active/Potential (which key off `status`) populate fine. These
# labels match what the legacy sync emitted, keeping old + new rows consistent.
_CATEGORY_LABEL = {
    "atm": "ATM Offering",
    "follow_on": "Series / Direct",
    "private_placement": "Series / Direct",
    "ipo": "IPO",
    "rights_offering": "Rights Offering",
    "equity_line": "ATM Offering",
    "shelf_registration": "Shelf Offering",
    "warrant_exercise": "Warrants / SAFEs",
    "convertible": "Convertible Note",
    "other": "Other",
}


def _is_unit_right_event(ev) -> bool:
    if (ev.source_quote or "").lower().find("right") == -1:
        return False
    t = ev.offering_type.value if hasattr(ev.offering_type, "value") else str(ev.offering_type)
    return t == "warrant_exercise"


def _category_label(ev) -> str:
    if _is_unit_right_event(ev):
        return "Unit Rights"
    t = ev.offering_type.value if hasattr(ev.offering_type, "value") else str(ev.offering_type)
    return _CATEGORY_LABEL.get(t, "Other")


def _edgar_index_url(cik: str, accession: str | None) -> str | None:
    """
    Build the SEC EDGAR filing-index URL from CIK + accession.

    XBRL-derived events (10-K/10-Q/8-K and amendments) carry source_form and
    source_accession but NO source_url — only the Docling-parsed prospectus
    (424B*) events get a URL set during the pipeline. Without this the filing
    "link" renders as non-clickable plain text in the UI. The index page is
    always valid and lets the user drill into any document in the filing.

    Format: https://www.sec.gov/Archives/edgar/data/<CIK-no-zeros>/<ACC-no-dashes>/<ACC>-index.htm
    """
    if not accession:
        return None
    try:
        cik_int = str(int(cik))  # strip leading zeros for the archives path
    except (TypeError, ValueError):
        return None
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession}-index.htm"


# Matches the trailing ":xxxxxxxx" (8 hex chars) discriminator that this engine
# appends to source_filing — used to scope pruning to rows WE manage so we never
# delete legacy/other-source offerings for the same CIK.
_DISC_RE = re.compile(r":[0-9a-f]{8}$")


def _is_engine_managed(source_filing: str | None) -> bool:
    return bool(source_filing and _DISC_RE.search(source_filing))


def _instrument_discriminator(ev) -> str:
    """
    Stable 8-char fingerprint distinguishing instruments that share the SAME
    (date, category, accession) — e.g. two warrant tranches in one 424B5.

    Must be DETERMINISTIC across processes (Python's built-in hash() is salted
    per-interpreter and would break idempotency), so we use sha1.

    CRITICAL for the two-phase (rules-only -> LLM-enriched) sync: the fingerprint
    must NOT change when reconciliation backfills fields during the LLM pass,
    otherwise the enriched event would land as a NEW row and orphan the fast-pass
    row. We therefore bucket on the most stable identifier available and
    deliberately EXCLUDE price_per_share (the field reconcile backfills most):
      * a bucketed SHARE count (~10k) for share-denominated instruments, plus
        exercise price for contingent tranches when present. Price is needed so
        equal-share Series A/B rights at different strikes do not collapse.
      * else a bucketed PROCEEDS figure (~$100k) for $-denominated instruments
        (shelf capacity, XBRL proceeds)
      * else the verbatim quote (capless open shelf — no figures at all)
    These mirror reconcile._key so phase-1 and phase-2 representations of the
    same instrument hash identically.
    """
    if ev.shares:
        t = ev.offering_type.value if hasattr(ev.offering_type, "value") else str(ev.offering_type)
        price_part = ""
        if ev.price_per_share and t in {"warrant_exercise", "convertible", "rights_offering"}:
            price_part = f":p:{round(ev.price_per_share, 4)}"
        seed = f"s:{round(ev.shares / 1e4)}{price_part}"
    elif ev.gross_proceeds_usd:
        seed = f"g:{round(ev.gross_proceeds_usd / 1e5)}"
    else:
        seed = "q:" + (ev.source_quote or "")[:200]
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def _event_to_row(ev, cik: str) -> dict:
    date_str = ev.event_date or ev.filing_date or ""
    if date_str and len(date_str) > 10:
        date_str = date_str[:10]
    # source_filing layout: "FORM:ACCESSION:DISC".  The leading FORM token is
    # what the alerts UI shows (portfolio.py does source_filing.split(":",1)[0]);
    # the trailing DISC keeps same-filing instruments unique under the DB
    # UniqueConstraint(cik, date, category, source_filing).
    src_filing = ev.source_form or "SEC"
    disc = _instrument_discriminator(ev)
    if ev.source_accession:
        src_filing = f"{ev.source_form}:{ev.source_accession}:{disc}"
    else:
        src_filing = f"{src_filing}:{disc}"
    return {
        "cik": cik,
        "date": date_str,
        "category": _category_label(ev),
        "text": ev.source_quote or _describe(ev),
        "source_filing": src_filing,
        # Fall back to a constructed EDGAR index URL when the extractor didn't
        # set one (XBRL-derived 10-K/10-Q/8-K events) so the filing link is
        # always clickable.
        "source_url": ev.source_url or _edgar_index_url(cik, ev.source_accession),
        "amount": ev.gross_proceeds_usd,
        "amount_text": _fmt_amount(ev.gross_proceeds_usd)
        or (f"{ev.shares:,.0f} shares" if ev.shares else None),
        "status": "Active" if ev.dilution_category == "active" else "Potential",
        "description": _describe(ev),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _run_pipeline_sync(ticker: str, max_filings: int, use_llm: bool):
    """Blocking call executed in a worker thread."""
    from smart_dilution_extractor import run as run_pipeline  # local import
    return run_pipeline(
        ticker, max_filings=max_filings, use_llm=use_llm, verbose=False
    )


async def _upsert_offerings(
    cik: str, rows: list[dict], prune: bool = False
) -> tuple[int, int, int]:
    """
    Insert new offerings / update changed ones. Returns (added, updated, removed).

    prune=False  -> additive only (the fast rules-only pass: never deletes, so
                    partial results just appear).
    prune=True   -> authoritative (the full LLM pass): also DELETES engine-managed
                    rows for this CIK that are absent from `rows`, cleaning up
                    fast-pass events the enriched pass dropped/superseded. Scoped
                    via _is_engine_managed so legacy/other-source rows are safe.
    """
    added = updated = removed = 0
    def _key(date, category, source_filing):
        # Mirror the DB UniqueConstraint EXACTLY:
        #   UniqueConstraint(cik, date, category, source_filing)
        # source_filing carries a per-instrument discriminator (see
        # _instrument_discriminator) so distinct tranches from the same filing
        # remain separate rows AND re-runs stay idempotent (no churn, no
        # IntegrityError) instead of colliding on a coarse key.
        return (date, category, source_filing or "")

    async with database.AsyncSessionLocal() as session:
        stmt = select(models.Offering).where(models.Offering.cik == cik)
        existing = (await session.execute(stmt)).scalars().all()
        index = {
            _key(o.date, o.category, o.source_filing): o
            for o in existing
        }

        incoming_keys: set[tuple] = set()
        for row in rows:
            key = _key(row["date"], row["category"], row["source_filing"])
            incoming_keys.add(key)
            obj = index.get(key)
            if obj is None:
                obj = models.Offering(**row)
                session.add(obj)
                index[key] = obj
                added += 1
            else:
                changed = False
                for fld in ("status", "description", "amount", "amount_text", "text", "source_url"):
                    new_val = row.get(fld)
                    if new_val is not None and getattr(obj, fld) != new_val:
                        setattr(obj, fld, new_val)
                        changed = True
                if changed:
                    updated += 1

        if prune:
            for o in existing:
                if (
                    _is_engine_managed(o.source_filing)
                    and _key(o.date, o.category, o.source_filing) not in incoming_keys
                ):
                    await session.delete(o)
                    removed += 1

        if added or updated or removed:
            await session.commit()
    return added, updated, removed


async def purge_dilution_data(cik: str) -> tuple[int, int]:
    """
    Remove per-user dilution alert state for a CIK after portfolio removal.

    Offering rows and cached summaries are shared ticker data populated by the
    AI sync jobs, so they must survive even when no user currently tracks the
    ticker. Otherwise a one-off AI extraction can appear to "vanish" from the
    frontend as soon as cleanup runs. Returns (offerings_deleted, alerts_deleted)
    for the existing caller contract; offerings_deleted is always 0.
    """
    from sqlalchemy import delete

    async with database.AsyncSessionLocal() as session:
        offering_ids_sq = select(models.Offering.id).where(models.Offering.cik == cik)

        alerts_res = await session.execute(
            delete(models.AlertLog).where(models.AlertLog.offering_id.in_(offering_ids_sq))
        )
        await session.commit()

    removed_alerts = alerts_res.rowcount or 0
    logger.info(
        f"[dilution_engine] purged dilution alert state for cik={cik}: "
        f"offerings=0, alerts={removed_alerts}, summary retained"
    )
    return 0, removed_alerts


async def sweep_untracked_dilution() -> dict:
    """
    Set-based reconciliation sweep for alert state.

    AI extraction output is shared ticker data, not portfolio-owned data. Do not
    delete Offering rows or cached dilution summaries here; manual and preload
    AI syncs must remain visible through ticker/search endpoints even before a
    user adds that ticker to a portfolio.

    Deletion order respects the alert_logs -> offerings FK:
      1. orphaned AlertLog rows (offering_id points at a missing offering)
      2. AlertLog rows for offerings no current portfolio owns

    Returns a summary dict: {ciks, offerings, alerts, summaries}.
    """
    from sqlalchemy import delete, func
    from .sync_universe import get_portfolio_ciks

    portfolio_ciks = await get_portfolio_ciks()

    async with database.AsyncSessionLocal() as session:
        # 1. Orphaned alerts: their offering was already deleted.
        valid_offering_ids = select(models.Offering.id)
        orphan_res = await session.execute(
            delete(models.AlertLog).where(
                models.AlertLog.offering_id.isnot(None),
                models.AlertLog.offering_id.notin_(valid_offering_ids),
            )
        )

        untracked_alerts_count = 0
        stale_ciks = 0
        if portfolio_ciks:
            portfolio_list = list(portfolio_ciks)
            ciks_res = await session.execute(
                select(func.count(func.distinct(models.Offering.cik)))
                .join(models.AlertLog, models.AlertLog.offering_id == models.Offering.id)
                .where(models.Offering.cik.notin_(portfolio_list))
            )
            stale_ciks = ciks_res.scalar() or 0

            untracked_offering_ids = select(models.Offering.id).where(
                models.Offering.cik.notin_(portfolio_list)
            )
            untracked_alerts_res = await session.execute(
                delete(models.AlertLog).where(
                    models.AlertLog.offering_id.in_(untracked_offering_ids)
                )
            )
            untracked_alerts_count = untracked_alerts_res.rowcount or 0
        else:
            ciks_res = await session.execute(
                select(func.count(func.distinct(models.Offering.cik)))
                .join(models.AlertLog, models.AlertLog.offering_id == models.Offering.id)
            )
            stale_ciks = ciks_res.scalar() or 0
            untracked_alerts_res = await session.execute(delete(models.AlertLog))
            untracked_alerts_count = untracked_alerts_res.rowcount or 0

        await session.commit()

    summary = {
        "ciks": stale_ciks,
        "offerings": 0,
        "alerts": (orphan_res.rowcount or 0) + untracked_alerts_count,
        "summaries": 0,
    }
    if any(v for k, v in summary.items() if k != "ciks"):
        logger.info(
            f"[dilution_engine] alert-state sweep: alerts={summary['alerts']} "
            f"across {summary['ciks']} untracked CIK(s); shared offerings retained"
        )
    return summary


async def sync_ticker_dilution(
    ticker: str,
    cik: str,
    max_filings: int = 12,
    use_llm: bool = True,
    *,
    fast_pass: bool = True,
    require_tracked: bool = False,
):
    """
    Run the hybrid dilution extractor and upsert offerings + summary.

    fast_pass=True (default for interactive flows):
      Phase 1 rules/XBRL only, then phase 2 LLM enrichment.
    fast_pass=False (batch/cron):
      Single authoritative LLM pass.

    require_tracked=True aborts if the ticker is not in any portfolio.
    require_tracked=False is used by the daily AI cron for the preload universe.
    """
    if not _ai_extraction_enabled():
        logger.info(
            f"[dilution_engine] AI extraction disabled (ENABLE_AI_EXTRACTION); "
            f"skipping dilution extraction for {ticker}."
        )
        return
    if ticker in _running:
        logger.info(f"[dilution_engine] sync already running for {ticker}; skipping.")
        return
    _running.add(ticker)
    try:
        async with _semaphore:
            if require_tracked and not await legacy_sync.is_ticker_tracked(cik):
                logger.info(f"[dilution_engine] {ticker} not tracked; aborting.")
                return

            logger.info(
                f"[dilution_engine] ===== DILUTION EXTRACTION STARTED for "
                f"{ticker} (cik={cik}) | max_filings={max_filings}, use_llm={use_llm}, "
                f"fast_pass={fast_pass}, require_tracked={require_tracked} ====="
            )

            if fast_pass and use_llm:
                try:
                    fast = await asyncio.to_thread(
                        _run_pipeline_sync, ticker, max_filings, False
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[dilution_engine] fast pass failed for {ticker}: {exc!r}")
                    return

                fast_rows = [
                    r for r in (_event_to_row(ev, cik) for ev in fast.events)
                    if _within_lookback(r)
                ]
                fa, fu, _ = await _upsert_offerings(cik, fast_rows, prune=False)
                logger.info(
                    f"[dilution_engine] {ticker} FAST pass (rules-only): "
                    f"{len(fast_rows)} events; added={fa}, updated={fu}"
                )
                try:
                    await legacy_sync.generate_dilution_summary_from_db(
                        ticker,
                        cik,
                        force=True,
                        require_tracked=require_tracked,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[dilution_engine] fast-pass summary failed for {ticker}: {exc!r}"
                    )

                if require_tracked and not await legacy_sync.is_ticker_tracked(cik):
                    logger.info(
                        f"[dilution_engine] {ticker} untracked mid-run; skipping LLM pass."
                    )
                    return

            if use_llm:
                try:
                    full = await asyncio.to_thread(
                        _run_pipeline_sync, ticker, max_filings, True
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[dilution_engine] LLM pass failed for {ticker}: {exc!r}")
                    return

                rows = [
                    r for r in (_event_to_row(ev, cik) for ev in full.events)
                    if _within_lookback(r)
                ]
                active = sum(1 for r in rows if r["status"] == "Active")
                potential = len(rows) - active
                added, updated, removed = await _upsert_offerings(cik, rows, prune=True)
                logger.info(
                    f"[dilution_engine] {ticker} FULL pass: {len(rows)} events "
                    f"(active={active}, potential={potential}); "
                    f"added={added}, updated={updated}, removed={removed}"
                )
            elif not fast_pass:
                try:
                    rules_only = await asyncio.to_thread(
                        _run_pipeline_sync, ticker, max_filings, False
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        f"[dilution_engine] rules-only pass failed for {ticker}: {exc!r}"
                    )
                    return
                rows = [
                    r for r in (_event_to_row(ev, cik) for ev in rules_only.events)
                    if _within_lookback(r)
                ]
                added, updated, removed = await _upsert_offerings(cik, rows, prune=True)
                logger.info(
                    f"[dilution_engine] {ticker} RULES-ONLY pass: {len(rows)} events; "
                    f"added={added}, updated={updated}, removed={removed}"
                )

        try:
            await legacy_sync.generate_dilution_summary_from_db(
                ticker,
                cik,
                force=True,
                require_tracked=require_tracked,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dilution_engine] summary refresh failed for {ticker}: {exc!r}")
    finally:
        _running.discard(ticker)


async def sync_portfolio_dilution(
    ticker: str,
    cik: str,
    max_filings: int = 12,
    use_llm: bool = True,
):
    """Portfolio-triggered extraction (requires an active portfolio holding)."""
    await sync_ticker_dilution(
        ticker,
        cik,
        max_filings=max_filings,
        use_llm=use_llm,
        fast_pass=True,
        require_tracked=True,
    )
