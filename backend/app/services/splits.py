"""
Split adjustment for share-count history.

SEC filings report shares outstanding on whatever basis was true on that date,
so a 1-for-100 reverse split looks like a 99% "buyback" and a 4-for-1 split
like 300% "dilution". This module restates a company's history to the LATEST
share basis and records every adjustment so it can be traced.

Pipeline (``adjust_series``), all pure functions, no I/O:

1. Clean   - drop non-positive / unparseable points, keep one point per date.
2. Repair  - a single point ~1000x (or 1e6x) off its neighbours is a filing
             reported in thousands: scale it back, flag ``units_repaired``.
             A single point that jumps >=30% and reverts next filing is a bad
             data point: exclude it, flag ``spike_excluded``.
3. Splits  - (a) known events (yfinance ``Ticker.splits``): each event is matched
             to the jump in the series nearest its date whose size fits the
             ratio (allowing for shares issued in the same quarter). Events with
             no matching jump were already restated by the filer and are skipped.
             (b) no known events: a persistent jump that fits a common split ratio
             (strict 3% for 2-5x; for >=6x the nearest common ratio allowing up to
             35% concurrent issuance). A persistent ~1000x jump is a units change.
4. Restate - every point before a split is multiplied by that split's ratio,
             cumulatively, so the whole series is on today's basis.

``compute_changes`` then computes 1Q / 1Y..10Y changes on the adjusted series,
and ``classify_ratio`` (used by the data-quality audit) labels raw jumps.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ratio helpers
# ---------------------------------------------------------------------------
# Ratios used to *adjust* (strict on purpose: 1.5x is NOT here, so a genuine 50%
# raise is never mistaken for a 3-for-2 split).
SMALL_SPLITS = (2.0, 3.0, 4.0, 5.0)
LARGE_SPLITS = (6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 50.0,
                60.0, 75.0, 80.0, 100.0, 150.0, 200.0, 250.0, 300.0, 500.0)
SMALL_TOL = 0.03          # 2-5x must match within 3%
LARGE_MAX_ISSUANCE = 0.35  # >=6x may carry up to 35% same-quarter issuance
UNITS_SCALES = (1_000.0, 1_000_000.0)
UNITS_TOL = 0.05
SPIKE_RATIO = 1.3          # a one-off jump at least this big that reverts is a bad point
SPIKE_REVERT_TOL = 0.10
EVENT_WINDOW_BEFORE = timedelta(days=120)
EVENT_WINDOW_AFTER = timedelta(days=200)
EVENT_LOG_TOL = math.log(1.6)
# Jumps this large that are neither a split nor a units change are flagged: real
# micro-cap issuance can reach 3-5x a quarter, beyond ~8x it is usually bad data.
UNEXPLAINED_RATIO = 8.0

# Broad list used only to *label* raw jumps in the audit ("looks like a split").
_AUDIT_CANDIDATES = [1.5, 2.5] + [float(n) for n in range(2, 101)] + [150.0, 200.0, 250.0, 300.0, 500.0]


def classify_ratio(ratio: float, tolerance: float = 0.02) -> str | None:
    """Audit label for a raw jump: 'units_error', 'split' or None (unexplained)."""
    if ratio <= 0:
        return None
    big = ratio if ratio >= 1 else 1 / ratio
    for scale in UNITS_SCALES:
        if abs(big / scale - 1) <= UNITS_TOL:
            return "units_error"
    for c in _AUDIT_CANDIDATES:
        if abs(big / c - 1) <= tolerance:
            return "split"
    return None


def units_scale(ratio: float) -> float | None:
    """1000 / 1e6 (or their inverses) if ratio is a units change, else None."""
    if ratio <= 0:
        return None
    big = ratio if ratio >= 1 else 1 / ratio
    for scale in UNITS_SCALES:
        if abs(big / scale - 1) <= UNITS_TOL:
            return scale if ratio >= 1 else 1 / scale
    return None


def match_split_ratio(ratio: float) -> tuple[float, bool] | None:
    """
    Split ratio (new shares per old share) that explains a jump, or None.

    Returns (split_ratio, exact) where exact means within 3%. A forward split
    with concurrent issuance looks like r = C * (1 + g); a reverse split like
    r = (1 / C) * (1 + g), with g >= 0 the shares issued in the same period.
    """
    if ratio <= 0:
        return None
    forward = ratio >= 1
    big = ratio if forward else 1 / ratio
    for c in SMALL_SPLITS:
        if abs(big / c - 1) <= SMALL_TOL:
            return (c if forward else 1 / c, True)
    if big < 5.8:
        return None
    if forward:
        # largest candidate C <= r (allowing 3% slack); implied issuance g = r/C - 1
        fits = [c for c in LARGE_SPLITS if c <= big * (1 + SMALL_TOL)]
        if not fits:
            return None
        c = max(fits)
        g = big / c - 1
    else:
        # smallest candidate C >= 1/r (allowing 3% slack); implied issuance g = C*r - 1
        fits = [c for c in LARGE_SPLITS if c >= big * (1 - SMALL_TOL)]
        if not fits:
            return None
        c = min(fits)
        g = c / big - 1
    if -SMALL_TOL <= g <= LARGE_MAX_ISSUANCE:
        return (c if forward else 1 / c, abs(g) <= SMALL_TOL)
    return None


def _parse(d) -> date | None:
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class SplitEvent:
    date: date
    ratio: float          # new shares per old share; reverse split < 1
    source: str = "yfinance"


@dataclass
class AppliedSplit:
    effective_date: str   # date of the first point on the new basis
    ratio: float
    source: str           # "yfinance" | "detected" | "units"
    exact: bool
    event_date: str | None = None
    observed_ratio: float | None = None

    def label(self) -> str:
        if self.source == "units":
            return "units change"
        r = self.ratio
        return f"{r:g}-for-1 split" if r >= 1 else f"1-for-{1 / r:g} reverse split"


@dataclass
class AdjustedPoint:
    date: str
    shares: int              # on the latest share basis
    raw_shares: int
    factor: float            # shares = round(raw (repaired) * factor)
    status: str = "ok"       # ok | repaired | excluded
    note: str | None = None
    source: str | None = None
    source_url: str | None = None


@dataclass
class AdjustedSeries:
    points: list[AdjustedPoint]
    splits: list[AppliedSplit] = field(default_factory=list)
    skipped_events: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)  # {date, kind, detail}

    @property
    def kept(self) -> list[AdjustedPoint]:
        return [p for p in self.points if p.status != "excluded"]

    def to_dict(self) -> dict:
        return {
            "points": [asdict(p) for p in self.points],
            "splits": [asdict(s) | {"label": s.label()} for s in self.splits],
            "skipped_events": self.skipped_events,
            "issues": self.issues,
        }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def _clean(rows: Iterable[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for r in rows:
        d = _parse(r.get("date"))
        s = r.get("shares")
        if d is None or s is None or s <= 0:
            continue
        by_date[d.isoformat()] = {**r, "date": d.isoformat(), "_d": d, "_s": float(s)}
    return [by_date[k] for k in sorted(by_date)]


def _repair_outliers(pts: list[dict], issues: list[dict]) -> None:
    """Mark one-off units errors (repair) and spikes (exclude) in place."""
    prev = None
    spike_ratios: list[float] = []
    for i, p in enumerate(pts):
        p["_status"], p["_val"], p["_note"] = "ok", p["_s"], None
        if prev is None:
            prev = p
            continue
        if i + 1 >= len(pts):
            # Latest point: no later filing to confirm a revert. If it repeats the
            # ratio of earlier excluded spikes (e.g. two share classes reported in
            # alternate filings), it is the same bad pattern.
            r = p["_s"] / prev["_val"]
            if max(r, 1 / r) >= SPIKE_RATIO and any(abs(math.log(r / sr)) <= math.log(1.05) for sr in spike_ratios):
                p["_status"] = "excluded"
                p["_note"] = f"x{r:.3g} jump repeating an earlier bad-point pattern"
                issues.append({"date": p["date"], "kind": "spike_excluded", "detail": f"x{r:.3g} (recurring)"})
            continue
        r = p["_s"] / prev["_val"]
        nxt = pts[i + 1]["_s"]
        reverts = abs(nxt / prev["_val"] - 1) <= SPIKE_REVERT_TOL
        if max(r, 1 / r) >= SPIKE_RATIO and reverts:
            scale = units_scale(r)
            if scale:
                p["_status"], p["_val"] = "repaired", p["_s"] / scale
                p["_note"] = f"reported in {'thousands' if abs(abs(math.log10(scale)) - 3) < 0.1 else 'millions'}; scaled"
                issues.append({"date": p["date"], "kind": "units_repaired", "detail": f"x{r:.4g} one-off"})
            else:
                p["_status"] = "excluded"
                spike_ratios.append(r)
                p["_note"] = f"one-off x{r:.3g} jump that reverts next filing"
                issues.append({"date": p["date"], "kind": "spike_excluded", "detail": f"x{r:.3g}"})
                continue  # prev stays on the last good point
        prev = p


def adjust_series(rows: Iterable[dict], events: list[SplitEvent] | None = None) -> AdjustedSeries:
    """
    rows: dicts with date, shares (+ optional source, source_url).
    events: known split events (e.g. yfinance). None or [] -> detect from the series.
    """
    issues: list[dict] = []
    pts = _clean(rows)
    if not pts:
        return AdjustedSeries(points=[])
    _repair_outliers(pts, issues)
    kept = [p for p in pts if p["_status"] != "excluded"]

    # jumps[k] = ratio between kept[k-1] and kept[k]
    jumps = {k: kept[k]["_val"] / kept[k - 1]["_val"] for k in range(1, len(kept))}
    split_at: dict[int, AppliedSplit] = {}
    skipped: list[dict] = []

    if events:
        for ev in sorted(events, key=lambda e: e.date):
            if ev.ratio <= 0 or ev.ratio == 1:
                continue
            best, best_dist = None, None
            for k, r in jumps.items():
                if k in split_at:
                    continue
                d = kept[k]["_d"]
                if not (ev.date - EVENT_WINDOW_BEFORE <= d <= ev.date + EVENT_WINDOW_AFTER):
                    continue
                dist = abs(math.log(r / ev.ratio))
                if dist <= EVENT_LOG_TOL and (best_dist is None or dist < best_dist):
                    best, best_dist = k, dist
            if best is None:
                skipped.append({"event_date": ev.date.isoformat(), "ratio": ev.ratio,
                                "reason": "no matching jump; data already on post-split basis or outside history"})
                continue
            split_at[best] = AppliedSplit(
                effective_date=kept[best]["date"], ratio=ev.ratio, source=ev.source,
                exact=abs(math.log(jumps[best] / ev.ratio)) <= math.log(1 + SMALL_TOL),
                event_date=ev.date.isoformat(), observed_ratio=round(jumps[best], 6))

    for k, r in jumps.items():
        if k in split_at:
            continue
        # Persistent = the next filing stays nearer the new level than the old one
        # (micro-caps often keep issuing after a reverse split, so "flat" is too strict).
        persists = k + 1 >= len(kept) or (
            abs(math.log(kept[k + 1]["_val"] / kept[k]["_val"]))
            < abs(math.log(kept[k + 1]["_val"] / kept[k - 1]["_val"])))
        scale = units_scale(r)
        if scale and persists:
            split_at[k] = AppliedSplit(kept[k]["date"], scale, "units", True, observed_ratio=round(r, 6))
            continue
        if events:  # known events are authoritative for real splits
            if max(r, 1 / r) >= UNEXPLAINED_RATIO:
                issues.append({"date": kept[k]["date"], "kind": "unexplained_jump", "detail": f"x{r:.3g}"})
            continue
        m = match_split_ratio(r)
        if m and persists:
            ratio, exact = m
            split_at[k] = AppliedSplit(kept[k]["date"], ratio, "detected", exact, observed_ratio=round(r, 6))
            if k + 1 >= len(kept):
                issues.append({"date": kept[k]["date"], "kind": "split_unconfirmed",
                               "detail": "jump at the latest filing; no later point confirms it"})
            elif not exact:
                issues.append({"date": kept[k]["date"], "kind": "split_approximate",
                               "detail": f"x{r:.4g} read as {AppliedSplit(kept[k]['date'], ratio, 'detected', exact).label()}"})
        elif m and not persists:
            issues.append({"date": kept[k]["date"], "kind": "split_unconfirmed",
                           "detail": f"x{r:.4g} looks like a split but the next filing moves back; not adjusted"})
        elif max(r, 1 / r) >= UNEXPLAINED_RATIO:
            issues.append({"date": kept[k]["date"], "kind": "unexplained_jump", "detail": f"x{r:.3g}"})

    # Cumulative restatement: factor for a point = product of ratios of all later splits.
    factors: dict[str, float] = {}
    running = 1.0
    for k in range(len(kept) - 1, -1, -1):
        factors[kept[k]["date"]] = running
        if k in split_at:
            running *= split_at[k].ratio

    out: list[AdjustedPoint] = []
    for p in pts:
        if p["_status"] == "excluded":
            # factor of the next kept point, so the table can still show it on today's basis
            later = [kp for kp in kept if kp["date"] > p["date"]]
            f = factors[later[0]["date"]] if later else 1.0
        else:
            f = factors[p["date"]]
        out.append(AdjustedPoint(
            date=p["date"], shares=int(round(p["_val"] * f)), raw_shares=int(p["_s"]),
            factor=round(f, 10), status=p["_status"], note=p["_note"],
            source=p.get("source"), source_url=p.get("source_url")))
    splits = [split_at[k] for k in sorted(split_at)]
    return AdjustedSeries(points=out, splits=splits, skipped_events=skipped, issues=issues)


# ---------------------------------------------------------------------------
# Changes over time (same rules as securities.calculate_changes, on adjusted data)
# ---------------------------------------------------------------------------
def compute_changes(points: list[AdjustedPoint]) -> dict:
    kept = [p for p in points if p.status != "excluded" and p.shares > 0]
    if not kept:
        return {}
    kept.sort(key=lambda p: p.date)
    latest = kept[-1]
    last_d = _parse(latest.date)
    out: dict = {"latest_shares": latest.shares, "last_date": latest.date,
                 "change_pct_1q": None, "date_1q": None, "prev_shares_1y": None}
    dated = [(_parse(p.date), p) for p in kept]

    def nearest(months: int):
        target = last_d - timedelta(days=30 * months)
        d, p = min(dated, key=lambda dp: abs((dp[0] - target).days))
        if abs((d - target).days) > 90:
            return None
        return p

    p = nearest(3)
    if p and p.shares:
        out["change_pct_1q"] = (latest.shares / p.shares - 1) * 100
        out["date_1q"] = p.date
    for y in range(1, 11):
        out[f"change_pct_{y}y"] = None
        out[f"date_{y}y"] = None
        p = nearest(12 * y)
        if p and p.shares:
            out[f"change_pct_{y}y"] = (latest.shares / p.shares - 1) * 100
            out[f"date_{y}y"] = p.date
            if y == 1:
                out["prev_shares_1y"] = p.shares
    return out


def issues_in_window(series: AdjustedSeries, start: str | None, end: str | None) -> list[dict]:
    """Issues and inexact/unconfirmed adjustments dated in (start, end]."""
    if not start or not end:
        return []
    return [i for i in series.issues if start < i["date"] <= end]


# ---------------------------------------------------------------------------
# yfinance split events, cached on disk
# ---------------------------------------------------------------------------
CACHE_DIR = Path(os.getenv("SPLITS_CACHE_DIR", str(Path(__file__).resolve().parents[2] / "cache" / "splits")))
CACHE_TTL_SECONDS = int(os.getenv("SPLITS_CACHE_TTL_SECONDS", str(7 * 86400)))


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper().replace('/', '_')}.json"


def read_cached_events(ticker: str, max_age: float | None = None) -> list[SplitEvent] | None:
    path = _cache_path(ticker)
    try:
        if not path.exists():
            return None
        if max_age is not None and time.time() - path.stat().st_mtime > max_age:
            return None
        data = json.loads(path.read_text())
        return [SplitEvent(date.fromisoformat(e["date"]), float(e["ratio"]), "yfinance") for e in data["events"]]
    except Exception:  # noqa: BLE001 - a corrupt cache file is just a miss
        return None


def fetch_split_events(ticker: str) -> list[SplitEvent] | None:
    """
    Split events for a ticker from yfinance, cached for CACHE_TTL_SECONDS.
    Returns None when yfinance is unavailable (caller falls back to detection).
    """
    cached = read_cached_events(ticker, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    if os.getenv("SPLITS_YFINANCE", "true").lower() in {"0", "false", "no", "off"}:
        return None  # offline (tests / air-gapped): series-based detection only
    try:
        import yfinance as yf

        raw = yf.Ticker(ticker).splits
        events = []
        if raw is not None:
            for ts, ratio in raw.items():
                if ratio and float(ratio) > 0:
                    events.append(SplitEvent(ts.date() if hasattr(ts, "date") else _parse(ts), float(ratio)))
    except Exception as exc:  # noqa: BLE001 - network / parsing problems must not break the page
        logger.warning("yfinance splits failed for %s: %r", ticker, exc)
        return read_cached_events(ticker)  # stale cache is better than nothing
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(ticker).write_text(json.dumps(
            {"ticker": ticker.upper(), "fetched_at": time.time(),
             "events": [{"date": e.date.isoformat(), "ratio": e.ratio} for e in events]}))
    except OSError as exc:
        logger.warning("could not cache splits for %s: %r", ticker, exc)
    return events
