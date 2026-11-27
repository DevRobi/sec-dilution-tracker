"""
Cross-filing reconciliation / dedup.

The same offering shows up in the F-3 shelf, the 424B5 supplement, the 6-K/8-K
closing announcement, and the next periodic report. We collapse those into one
canonical record, keeping the figures from the highest-authority form and
attaching every supporting accession. Disagreements >5% are flagged.
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .models import DilutionEvent

# Relative tolerance for collapsing near-identical figures that the coarse,
# ABSOLUTE bucket key in _key() misses. Two same-type events for the same date
# whose primary magnitude (proceeds, else shares) agree within this fraction are
# treated as ONE instrument double-extracted — e.g. the rules regex and the LLM
# grabbing slightly different counts from the same filing, or a restated figure.
# A sub-1% difference on a 37M-share warrant spans many 10k buckets, so it
# survived as a duplicate (the POET case: 37,569,438 vs 37,364,938 in one
# 424B5). Keep this very tight: AMPG has genuinely distinct Series Rights with
# 2,230,000 and 2,268,204 shares, only ~1.7% apart.
_NEAR_DUP_REL_TOL = 0.01


def _primary_magnitude(ev: DilutionEvent):
    """(kind, value) used to compare two same-type/date events for near-dup."""
    if ev.gross_proceeds_usd:
        return ("g", ev.gross_proceeds_usd)
    if ev.shares:
        return ("s", ev.shares)
    return (None, None)


def _price_bucket(ev: DilutionEvent):
    """
    Distinguish equal-share potential tranches with different exercise prices.

    Example: one unit contains a Series A Right for 2.23M shares at $5 and a
    Series B Right for 2.23M shares at $6. Grouping only on share count collapses
    one tranche and loses data before the DB write.
    """
    offering_type = ev.offering_type.value if hasattr(ev.offering_type, "value") else str(ev.offering_type)
    if ev.shares and ev.price_per_share and offering_type in {
        "warrant_exercise",
        "convertible",
        "rights_offering",
    }:
        return round(ev.price_per_share, 4)
    return None


def _disp_date(ev: DilutionEvent) -> str:
    return (ev.event_date or ev.filing_date or "")[:10]


def _rank(e: DilutionEvent):
    completeness = sum(
        x is not None for x in (e.shares, e.price_per_share, e.gross_proceeds_usd)
    )
    return (config.FORM_PRIORITY.get(e.source_form, 0), e.confidence, completeness)


def _merge_near_duplicates(events: list[DilutionEvent]) -> list[DilutionEvent]:
    """
    Second-stage dedup run AFTER the coarse bucket reconcile. Collapses
    same-type, same-date events whose primary magnitude agrees within
    _NEAR_DUP_REL_TOL — the case the absolute bucket key can't catch for large
    figures. Events with neither proceeds nor shares pass through untouched.
    """
    groups: dict[tuple, list[DilutionEvent]] = defaultdict(list)
    passthrough: list[DilutionEvent] = []
    for ev in events:
        kind, val = _primary_magnitude(ev)
        if val is None:
            passthrough.append(ev)
            continue
        groups[(ev.offering_type, _disp_date(ev), kind, _price_bucket(ev))].append(ev)

    out: list[DilutionEvent] = list(passthrough)
    for members in groups.values():
        # Most authoritative/complete first so it becomes the kept representative.
        members.sort(key=_rank, reverse=True)
        reps: list[DilutionEvent] = []
        for ev in members:
            _, val = _primary_magnitude(ev)
            for rep in reps:
                _, rep_val = _primary_magnitude(rep)
                hi = max(val, rep_val)
                if hi > 0 and abs(val - rep_val) / hi <= _NEAR_DUP_REL_TOL:
                    # Same instrument: fold ev into rep (backfill blanks, keep
                    # the higher confidence) and drop ev.
                    for fld in ("shares", "price_per_share", "gross_proceeds_usd",
                                "net_proceeds_usd", "event_date"):
                        if getattr(rep, fld) is None and getattr(ev, fld) is not None:
                            setattr(rep, fld, getattr(ev, fld))
                    rep.confidence = max(rep.confidence, ev.confidence)
                    break
            else:
                reps.append(ev)
        out.extend(reps)
    return out


def _key(ev: DilutionEvent):
    """
    Coarse grouping key for dedup. Prefer a proceeds bucket (~$100k); for
    potential instruments with no dollar amount (warrants/options/converts),
    bucket by share count (~10k) so distinct tranches don't wrongly collapse;
    a capless open shelf collapses to a single record per type.
    """
    if ev.gross_proceeds_usd:
        return (ev.offering_type, "g", round(ev.gross_proceeds_usd / 1e5))
    if ev.shares:
        return (ev.offering_type, "s", round(ev.shares / 1e4), _price_bucket(ev))
    return (ev.offering_type, "none", 0)


def reconcile(events: list[DilutionEvent]) -> list[DilutionEvent]:
    groups: dict[tuple, list[DilutionEvent]] = {}
    for ev in events:
        # Events with no proceeds AND no shares are too thin to dedup reliably;
        # keep them grouped only by type so we don't over-merge.
        groups.setdefault(_key(ev), []).append(ev)

    canonical: list[DilutionEvent] = []
    for _key_, members in groups.items():
        # Pick the most authoritative + most complete member as the base.
        members.sort(key=_rank, reverse=True)
        base = members[0].model_copy(deep=True)

        supporting = []
        proceeds_vals = []
        for m in members:
            if m.source_accession:
                supporting.append(f"{m.source_form}:{m.source_accession}")
            if m.gross_proceeds_usd:
                proceeds_vals.append(m.gross_proceeds_usd)
            # Backfill missing fields from other members.
            for fld in ("shares", "price_per_share", "gross_proceeds_usd",
                        "net_proceeds_usd", "event_date"):
                if getattr(base, fld) is None and getattr(m, fld) is not None:
                    setattr(base, fld, getattr(m, fld))

        if len(proceeds_vals) > 1:
            lo, hi = min(proceeds_vals), max(proceeds_vals)
            if hi > 0 and (hi - lo) / hi > 0.05:
                base.validation_flags.append(
                    f"cross_filing_discrepancy({lo:,.0f}-{hi:,.0f})"
                )

        base.validation_flags.append(f"supported_by={len(set(supporting))}_filings")
        base.confidence = max(m.confidence for m in members)
        canonical.append(base)

    # Second-stage relative-tolerance dedup catches near-identical figures that
    # the coarse absolute bucket above couldn't (large warrant/proceeds counts
    # extracted slightly differently by rules vs LLM within the same filing).
    canonical = _merge_near_duplicates(canonical)

    canonical.sort(key=lambda e: (e.filing_date or "", e.gross_proceeds_usd or 0), reverse=True)
    return canonical
