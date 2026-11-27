"""
Data-quality checks.

Each check is a small function ``check_x(ctx) -> list[Finding]`` registered in
CHECKS. They all work from plain SQL against a DB-API connection (SQLite in
practice, opened read-only), pulling the small tables (history, offerings,
stats, companies) into memory once and doing the logic in Python so the rules
are easy to read and unit-test.

Severity convention
-------------------
* error   - the number is wrong or the feed is broken; do not trust downstream
            output that depends on it (rankings, dilution totals).
* warning - probably wrong / needs a human look (likely duplicate, unexplained
            jump, stale ticker).
* info    - coverage notes (thin history, shells without data).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

SEVERITIES = ("error", "warning", "info")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class AuditConfig:
    as_of: date = field(default_factory=date.today)
    # A ticker is "stale" when its newest share-count point is older than this.
    # 10-Q/10-K cadence is ~90 days, so 200 days = at least two missed filings.
    stale_days: int = 200
    # The daily sync is considered late when the newest stats refresh is older.
    refresh_days: int = 7
    # Per-ticker price feed lag (vs. the newest price in the table).
    price_lag_days: int = 10
    # Consecutive share counts whose ratio is >= jump_ratio (or <= 1/jump_ratio)
    # are flagged as implausible unless explained.
    jump_ratio: float = 1.5
    # Relative tolerance for "this ratio is a clean split ratio".
    split_tolerance: float = 0.02
    # Relative tolerance for near-duplicate offering quantities.
    near_dup_tolerance: float = 0.01
    # Shelf registrations stay usable for 3 years (Rule 415(a)(5)).
    shelf_life_days: int = 3 * 365
    # Tickers with fewer history points than this get an info finding.
    min_history_points: int = 4
    # Skip the (large) prices table scan.
    skip_prices: bool = False


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    cik: str | None = None
    ticker: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditResult:
    findings: list[Finding]
    meta: dict

    def counts_by_check(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, (severity, description, _) in CHECKS.items():
            out[name] = {"severity": severity, "description": description, "count": 0}
        for f in self.findings:
            out.setdefault(f.check, {"severity": f.severity, "description": "", "count": 0})
            out[f.check]["count"] += 1
        return out

    def counts_by_severity(self) -> dict[str, int]:
        c = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def summary(self) -> dict:
        return {
            "meta": self.meta,
            "by_severity": self.counts_by_severity(),
            "by_check": self.counts_by_check(),
            "total_findings": len(self.findings),
        }


# ---------------------------------------------------------------------------
# Parsing helpers (pure, unit-tested)
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
_SHARES_TEXT_RE = re.compile(r"^\s*([\d,]+)\s+shares", re.IGNORECASE)
_DOLLAR_TEXT_RE = re.compile(r"^\s*\$\s*([\d.]+)\s*([KMB])?\s*$", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$([\d.]+)/sh")
_DESC_SHARES_RE = re.compile(r"([\d,]{5,})\s+shares")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_shares_text(text: str | None) -> int | None:
    """'2,230,000 shares' -> 2230000."""
    if not text:
        return None
    m = _SHARES_TEXT_RE.match(text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_dollar_text(text: str | None) -> float | None:
    """'$13.7M' -> 13_700_000.0 ; '$21K' -> 21_000.0."""
    if not text:
        return None
    m = _DOLLAR_TEXT_RE.match(text)
    if not m:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1.0)
    return float(m.group(1)) * mult


def offering_shares(row: dict) -> int | None:
    """Share quantity of an offering row, from amount_text or the description."""
    s = parse_shares_text(row.get("amount_text"))
    if s:
        return s
    m = _DESC_SHARES_RE.search(row.get("description") or "")
    return int(m.group(1).replace(",", "")) if m else None


def offering_price(row: dict) -> float | None:
    m = _PRICE_RE.search(row.get("description") or "")
    return float(m.group(1)) if m else None


def split_source_filing(source_filing: str | None) -> tuple[str, str | None]:
    """'424B5:0001493152-25-029223:d9f18a61' -> ('424B5', '0001493152-25-029223')."""
    if not source_filing:
        return "", None
    parts = source_filing.split(":")
    form = parts[0]
    accession = parts[1] if len(parts) > 1 and _ACCESSION_RE.match(parts[1]) else None
    return form, accession


def numbers_in_text(text: str | None) -> set[int]:
    if not text:
        return set()
    return {int(n.replace(",", "")) for n in _NUM_RE.findall(text)}


# Shared with the split-adjustment service so the audit and the API agree on
# what a split-like or units-error jump is.
from app.services.splits import classify_ratio  # noqa: E402


# ---------------------------------------------------------------------------
# Context: load everything once
# ---------------------------------------------------------------------------
class AuditContext:
    def __init__(self, conn: sqlite3.Connection, cfg: AuditConfig):
        self.conn = conn
        self.cfg = cfg
        self.conn.row_factory = sqlite3.Row
        t0 = time.time()
        self.companies = {r["cik"]: dict(r) for r in self._q("SELECT cik, ticker, title FROM companies")}
        self.history = [dict(r) for r in self._q(
            "SELECT id, cik, date, shares, source FROM history ORDER BY cik, date, id"
        )]
        self.offerings = [dict(r) for r in self._q(
            "SELECT id, cik, date, category, text, source_filing, amount, amount_text, "
            "status, description FROM offerings ORDER BY cik, date, id"
        )]
        self.stats = {r["cik"]: dict(r) for r in self._q("SELECT * FROM stats")}
        self.history_by_cik: dict[str, list[dict]] = defaultdict(list)
        for h in self.history:
            self.history_by_cik[h["cik"]].append(h)
        logger.info(
            "loaded companies=%d history=%d offerings=%d stats=%d in %.1fs",
            len(self.companies), len(self.history), len(self.offerings), len(self.stats),
            time.time() - t0,
        )

    def _q(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def ticker(self, cik: str | None) -> str | None:
        c = self.companies.get(cik or "")
        return c["ticker"] if c else None

    def finding(self, check: str, message: str, cik: str | None = None, **details) -> Finding:
        severity = CHECKS[check][0]
        return Finding(check=check, severity=severity, message=message, cik=cik,
                       ticker=self.ticker(cik), details=details)


# ---------------------------------------------------------------------------
# Offering checks
# ---------------------------------------------------------------------------
def check_offering_exact_duplicates(ctx: AuditContext) -> list[Finding]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for o in ctx.offerings:
        groups[(o["cik"], o["date"], o["category"], o["source_filing"])].append(o)
    return [
        ctx.finding("offering_exact_duplicate",
                    f"{len(rows)} identical offering rows ({k[2]} {k[1]} {k[3]})",
                    k[0], ids=[r["id"] for r in rows])
        for k, rows in groups.items() if len(rows) > 1
    ]


def _instrument_groups(ctx: AuditContext) -> dict[tuple, list[dict]]:
    """Offerings grouped by (cik, accession, category)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for o in ctx.offerings:
        _, acc = split_source_filing(o["source_filing"])
        groups[(o["cik"], acc or o["source_filing"], o["category"])].append(o)
    return groups


def check_offering_same_instrument(ctx: AuditContext) -> list[Finding]:
    """Same filing + category + identical quantity + price, but stored as separate rows."""
    out = []
    for (cik, acc, cat), rows in _instrument_groups(ctx).items():
        by_qty: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            qty = offering_shares(r) or (round(r["amount"]) if r["amount"] else None)
            if qty:
                by_qty[(qty, offering_price(r))].append(r)
        for (qty, price), dup in by_qty.items():
            if len(dup) > 1:
                out.append(ctx.finding(
                    "offering_same_instrument_duplicate",
                    f"{len(dup)} rows for the same {cat} ({qty:,} @ {price}) in filing {acc}",
                    cik, ids=[r["id"] for r in dup], accession=acc, quantity=qty))
    return out


def check_offering_near_duplicates(ctx: AuditContext) -> list[Finding]:
    """Quantities within 1% in the same filing/category: usually an LLM digit misread."""
    tol = ctx.cfg.near_dup_tolerance
    out = []
    for (cik, acc, cat), rows in _instrument_groups(ctx).items():
        qty_rows = [(offering_shares(r), "shares", r) for r in rows if offering_shares(r)]
        qty_rows += [(r["amount"], "usd", r) for r in rows if not offering_shares(r) and r["amount"]]
        for i in range(len(qty_rows)):
            for j in range(i + 1, len(qty_rows)):
                a, ua, ra = qty_rows[i]
                b, ub, rb = qty_rows[j]
                if ua != ub or a == b:
                    continue
                if abs(a - b) / max(a, b) <= tol:
                    out.append(ctx.finding(
                        "offering_near_duplicate",
                        f"{cat} in {acc}: {a:,.0f} vs {b:,.0f} {ua} (within {tol:.0%})",
                        cik, ids=[ra["id"], rb["id"]], accession=acc, values=[a, b]))
    return out


def check_offering_cross_filing_repeat(ctx: AuditContext) -> list[Finding]:
    """
    The same share quantity extracted from several different filings. Typical
    causes: a later prospectus recaps an earlier raise ("sale of 7,143,000
    shares in July 2025") or re-lists the same outstanding warrants/plan
    reserve. Summing these rows double-counts dilution.
    """
    by_qty: dict[tuple, list[dict]] = defaultdict(list)
    for o in ctx.offerings:
        qty = offering_shares(o)
        if qty and qty >= 10_000:
            by_qty[(o["cik"], qty)].append(o)
    out = []
    for (cik, qty), rows in by_qty.items():
        accs = {split_source_filing(r["source_filing"])[1] for r in rows}
        if len(accs) > 1:
            out.append(ctx.finding(
                "offering_cross_filing_repeat",
                f"{qty:,} shares appears in {len(accs)} different filings "
                f"({', '.join(sorted({r['category'] for r in rows}))})",
                cik, ids=[r["id"] for r in rows], quantity=qty,
                dates=sorted({r["date"] for r in rows})))
    return out


def check_offering_ungrounded_number(ctx: AuditContext) -> list[Finding]:
    """Extracted share count does not appear verbatim in the stored source quote."""
    out = []
    for o in ctx.offerings:
        qty = offering_shares(o)
        nums = numbers_in_text(o["text"])
        big_nums = {n for n in nums if n >= 1_000}
        if not qty or not big_nums or qty in nums:
            continue
        closest = min(big_nums, key=lambda n: abs(n - qty))
        out.append(ctx.finding(
            "offering_ungrounded_number",
            f"extracted {qty:,} shares not in source quote (closest number in quote: {closest:,})",
            o["cik"], ids=[o["id"]], extracted=qty, closest_in_quote=closest))
    return out


def check_offering_values(ctx: AuditContext) -> list[Finding]:
    out = []
    valid_status = {"Active", "Potential"}
    for o in ctx.offerings:
        oid = [o["id"]]
        if o["amount"] is not None and o["amount"] <= 0:
            out.append(ctx.finding("offering_invalid_amount",
                                   f"amount={o['amount']} ({o['category']} {o['date']})", o["cik"], ids=oid))
        if o["amount"] is None and not o["amount_text"]:
            out.append(ctx.finding("offering_incomplete",
                                   f"no amount and no quantity ({o['category']} {o['date']})", o["cik"], ids=oid))
        if not o["date"] or not parse_date(o["date"]) or not o["category"] or o["status"] not in valid_status:
            out.append(ctx.finding("offering_incomplete",
                                   f"missing/invalid date, category or status "
                                   f"(date={o['date']!r}, status={o['status']!r})", o["cik"], ids=oid))
        dollars = parse_dollar_text(o["amount_text"])
        if dollars and o["amount"] and abs(dollars - o["amount"]) / o["amount"] > 0.05:
            out.append(ctx.finding("offering_amount_text_mismatch",
                                   f"amount {o['amount']:,.0f} vs amount_text {o['amount_text']}",
                                   o["cik"], ids=oid))
        cat = (o["category"] or "").lower()
        if ("shelf" in cat or "warrant" in cat) and o["status"] == "Active":
            out.append(ctx.finding("offering_status_mismatch",
                                   f"{o['category']} marked Active (should be Potential)", o["cik"], ids=oid))
    return out


def check_capacity_reconciliation(ctx: AuditContext) -> list[Finding]:
    """
    Shelf / ATM capacity vs. what has been drawn.

    For each CIK, at every shelf-registration date S, the capacity still in its
    3-year life is compared with the dollars raised via Active prospectus
    takedowns (424B*) since the oldest live shelf. Used > authorized (i.e.
    negative remaining capacity) means either a shelf is missing from the DB or
    a takedown was mis-extracted/double-counted. ATM rows get the same test
    against Active rows in the ATM category.
    """
    life = timedelta(days=ctx.cfg.shelf_life_days)
    by_cik: dict[str, list[dict]] = defaultdict(list)
    for o in ctx.offerings:
        by_cik[o["cik"]].append(o)
    out = []
    for cik, rows in by_cik.items():
        for kind, cap_pred, use_pred in (
            ("shelf",
             lambda r: "shelf" in (r["category"] or "").lower(),
             lambda r: r["status"] == "Active" and split_source_filing(r["source_filing"])[0].startswith("424B")),
            ("atm",
             lambda r: "atm" in (r["category"] or "").lower() and r["status"] == "Potential",
             lambda r: "atm" in (r["category"] or "").lower() and r["status"] == "Active"),
        ):
            caps = [(parse_date(r["date"]), r["amount"]) for r in rows if cap_pred(r) and r["amount"] and r["amount"] > 0]
            caps = [(d, a) for d, a in caps if d]
            if not caps:
                continue
            for d0, _ in caps:
                live = [(d, a) for d, a in caps if d <= d0 + life and d + life >= d0]
                start = min(d for d, _ in live)
                authorized = sum(a for _, a in live)
                used_rows = [r for r in rows if use_pred(r) and r["amount"] and r["amount"] > 0
                             and parse_date(r["date"]) and start <= parse_date(r["date"]) <= d0 + life]
                used = sum(r["amount"] for r in used_rows)
                if used > authorized * 1.0001:
                    out.append(ctx.finding(
                        f"{kind}_overdrawn",
                        f"{kind.upper()} capacity ${authorized:,.0f} but ${used:,.0f} drawn "
                        f"(remaining {authorized - used:,.0f})",
                        cik, authorized=authorized, used=used, remaining=authorized - used,
                        window_start=str(start), ids=[r["id"] for r in used_rows]))
                    break
    return out


# ---------------------------------------------------------------------------
# Share-count history checks
# ---------------------------------------------------------------------------
def check_history_duplicates(ctx: AuditContext) -> list[Finding]:
    out = []
    for cik, rows in ctx.history_by_cik.items():
        by_date: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_date[r["date"]].append(r)
        for d, same in by_date.items():
            if len(same) < 2:
                continue
            values = sorted({r["shares"] for r in same if r["shares"] is not None})
            kind = "exact duplicate" if len(values) <= 1 else "conflicting values"
            out.append(ctx.finding("history_duplicate_date", f"{len(same)} rows on {d} ({kind}: {values})",
                                   cik, ids=[r["id"] for r in same], date=d, values=values))
    return out


def check_history_values(ctx: AuditContext) -> list[Finding]:
    out = []
    for r in ctx.history:
        d = parse_date(r["date"])
        if r["shares"] is None or r["shares"] <= 0:
            out.append(ctx.finding("history_invalid_shares", f"shares={r['shares']} on {r['date']} ({r['source']})",
                                   r["cik"], ids=[r["id"]]))
        if d is None:
            out.append(ctx.finding("history_future_or_bad_date", f"unparseable date {r['date']!r}",
                                   r["cik"], ids=[r["id"]]))
        elif d > ctx.cfg.as_of:
            out.append(ctx.finding("history_future_or_bad_date",
                                   f"date {r['date']} is after as-of {ctx.cfg.as_of} ({r['source']})",
                                   r["cik"], ids=[r["id"]], shares=r["shares"]))
    return out


def _valid_series(rows: list[dict]) -> list[dict]:
    """One point per date (last id wins), positive shares, parseable date."""
    by_date: dict[str, dict] = {}
    for r in rows:
        if r["shares"] and r["shares"] > 0 and parse_date(r["date"]):
            by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date)]


def find_jumps(series: list[dict], cfg: AuditConfig) -> list[dict]:
    """
    Consecutive-point jumps beyond cfg.jump_ratio, classified as
    'spike' (A -> B -> ~A: one bad data point), 'units_error', 'split',
    or 'unexplained'.
    """
    jumps = []
    i = 1
    while i < len(series):
        prev, cur = series[i - 1], series[i]
        ratio = cur["shares"] / prev["shares"]
        if ratio >= cfg.jump_ratio or ratio <= 1 / cfg.jump_ratio:
            kind = None
            if i + 1 < len(series):
                nxt = series[i + 1]
                if abs(nxt["shares"] / prev["shares"] - 1) <= 0.05:
                    kind = "spike"
            if kind is None:
                kind = classify_ratio(ratio, cfg.split_tolerance) or "unexplained"
            jumps.append({"from_date": prev["date"], "to_date": cur["date"],
                          "from_shares": prev["shares"], "to_shares": cur["shares"],
                          "ratio": round(ratio, 4), "kind": kind,
                          "ids": [prev["id"], cur["id"]], "source": cur["source"]})
            if kind == "spike":
                i += 1  # don't double-report the reversion
        i += 1
    return jumps


_JUMP_CHECK = {
    "spike": "history_spike",
    "units_error": "history_units_error",
    "split": "history_unadjusted_split",
    "unexplained": "history_unexplained_jump",
}


def check_history_jumps(ctx: AuditContext) -> list[Finding]:
    out = []
    ctx.jumps_by_cik = {}
    for cik, rows in ctx.history_by_cik.items():
        jumps = find_jumps(_valid_series(rows), ctx.cfg)
        ctx.jumps_by_cik[cik] = jumps
        for j in jumps:
            out.append(ctx.finding(
                _JUMP_CHECK[j["kind"]],
                f"{j['from_shares']:,} -> {j['to_shares']:,} (x{j['ratio']}) "
                f"between {j['from_date']} and {j['to_date']} [{j['source']}]",
                cik, **j))
    return out


def check_missing_history(ctx: AuditContext) -> list[Finding]:
    out = []
    no_hist = [c for c in ctx.companies if c not in ctx.history_by_cik]
    for cik in no_hist:
        if cik in ctx.stats:
            out.append(ctx.finding("missing_history", "has stats row but no share-count history", cik))
    shells = [c for c in no_hist if c not in ctx.stats]
    if shells:
        out.append(ctx.finding(
            "coverage_shells",
            f"{len(shells)} companies in the universe have no share-count history or stats (never synced)",
            None, count=len(shells), sample=[ctx.ticker(c) for c in shells[:25]]))
    for cik, rows in ctx.history_by_cik.items():
        n = len(_valid_series(rows))
        if 0 < n < ctx.cfg.min_history_points:
            out.append(ctx.finding("thin_history", f"only {n} share-count point(s)", cik, points=n))
    for cik in ctx.history_by_cik:
        if cik not in ctx.companies:
            out.append(ctx.finding("orphan_rows", "history rows for a CIK missing from companies", cik))
    for cik in {o["cik"] for o in ctx.offerings}:
        if cik not in ctx.companies:
            out.append(ctx.finding("orphan_rows", "offering rows for a CIK missing from companies", cik))
    return out


def check_stale_filings(ctx: AuditContext) -> list[Finding]:
    cutoff = ctx.cfg.as_of - timedelta(days=ctx.cfg.stale_days)
    out = []
    for cik in ctx.stats:
        series = _valid_series(ctx.history_by_cik.get(cik, []))
        if not series:
            continue
        last = parse_date(series[-1]["date"])
        if last and last < cutoff:
            out.append(ctx.finding(
                "stale_ticker",
                f"newest share count is {series[-1]['date']} ({(ctx.cfg.as_of - last).days} days old)",
                cik, last_date=series[-1]["date"], age_days=(ctx.cfg.as_of - last).days))
    return out


# ---------------------------------------------------------------------------
# Stats (what the rankings read) checks
# ---------------------------------------------------------------------------
def check_stats_consistency(ctx: AuditContext) -> list[Finding]:
    out = []
    for cik, s in ctx.stats.items():
        series = _valid_series(ctx.history_by_cik.get(cik, []))
        if series and s["latest_shares"] is not None and s["latest_shares"] != series[-1]["shares"]:
            out.append(ctx.finding("stats_latest_mismatch",
                                   f"stats.latest_shares={s['latest_shares']:,} but newest history="
                                   f"{series[-1]['shares']:,} ({series[-1]['date']})", cik))
        latest, prev, pct = s.get("latest_shares"), s.get("prev_shares_1y"), s.get("change_pct_1y")
        if latest and prev and pct is not None:
            # prev_shares_1y is stored as int(), so allow its rounding error.
            recomputed = (latest / prev - 1) * 100
            if abs(recomputed - pct) > max(0.01, 100 / prev):
                out.append(ctx.finding("stats_arithmetic",
                                       f"change_pct_1y={pct:.3f}% but latest/prev implies {recomputed:.3f}%",
                                       cik, stored=pct, recomputed=recomputed))
        last = parse_date(s.get("last_date"))
        d1y = parse_date(s.get("date_1y"))
        if last and d1y:
            gap = (last - d1y).days
            if abs(gap - 365) > 120:
                out.append(ctx.finding(
                    "stats_window_mismatch",
                    f"'1Y' change compares {s['date_1y']} -> {s['last_date']} ({gap} days apart)",
                    cik, gap_days=gap, change_pct_1y=s.get("change_pct_1y")))
    return out


def check_stats_split_contamination(ctx: AuditContext) -> list[Finding]:
    """A 1Y change that spans an unadjusted split / units error / spike."""
    jumps_by_cik = getattr(ctx, "jumps_by_cik", None)
    if jumps_by_cik is None:
        check_history_jumps(ctx)
        jumps_by_cik = ctx.jumps_by_cik
    out = []
    for cik, s in ctx.stats.items():
        d1y, last = s.get("date_1y"), s.get("last_date")
        if not d1y or not last or s.get("change_pct_1y") is None:
            continue
        bad = [j for j in jumps_by_cik.get(cik, [])
               if j["kind"] in ("split", "units_error", "spike") and d1y < j["to_date"] <= last]
        if bad:
            out.append(ctx.finding(
                "stats_split_contaminated",
                f"1Y change {s['change_pct_1y']:+.1f}% spans a {bad[0]['kind']} "
                f"(x{bad[0]['ratio']} on {bad[0]['to_date']}); ranking is not a real buyback/dilution",
                cik, change_pct_1y=s["change_pct_1y"], jump=bad[0]))
    return out


# ---------------------------------------------------------------------------
# Feed freshness (late / missing feed)
# ---------------------------------------------------------------------------
def check_feed_freshness(ctx: AuditContext) -> list[Finding]:
    out = []
    refreshed = [parse_date(s.get("last_updated")) for s in ctx.stats.values()]
    refreshed = [d for d in refreshed if d]
    if refreshed:
        newest = max(refreshed)
        age = (ctx.cfg.as_of - newest).days
        stale = sum(1 for d in refreshed if (ctx.cfg.as_of - d).days > ctx.cfg.refresh_days)
        if age > ctx.cfg.refresh_days:
            out.append(ctx.finding(
                "feed_late",
                f"stats feed last refreshed {newest} ({age} days before {ctx.cfg.as_of}); "
                f"{stale}/{len(refreshed)} rows older than {ctx.cfg.refresh_days} days",
                None, newest_refresh=str(newest), age_days=age, stale_rows=stale))
    if ctx.cfg.skip_prices:
        return out
    rows = ctx._q("SELECT cik, MAX(date) AS max_date FROM prices GROUP BY cik")
    latest = {r["cik"]: parse_date(r["max_date"]) for r in rows}
    if latest:
        newest = max(d for d in latest.values() if d)
        age = (ctx.cfg.as_of - newest).days
        if age > ctx.cfg.refresh_days:
            out.append(ctx.finding("feed_late",
                                   f"price feed newest bar is {newest} ({age} days before {ctx.cfg.as_of})",
                                   None, newest_price=str(newest), age_days=age))
        for cik, d in latest.items():
            if d and (newest - d).days > ctx.cfg.price_lag_days:
                out.append(ctx.finding("price_lagging",
                                       f"latest price {d} is {(newest - d).days} days behind the feed ({newest})",
                                       cik, last_price_date=str(d)))
        for cik in ctx.stats:
            if cik not in latest:
                out.append(ctx.finding("price_missing", "no price history (market cap filters will drop it)", cik))
    return out


# ---------------------------------------------------------------------------
# Registry: name -> (severity, description, function)
# Several functions emit more than one check name; they're listed once in RUNNERS.
# ---------------------------------------------------------------------------
CHECKS: dict[str, tuple[str, str, Callable | None]] = {
    "feed_late": ("error", "Daily stats/price feed has not refreshed recently", None),
    "offering_exact_duplicate": ("error", "Identical offering rows (same key)", None),
    "offering_same_instrument_duplicate": ("error", "Same filing+category+quantity+price stored twice", None),
    "offering_near_duplicate": ("warning", "Same filing+category, quantities within 1% (likely LLM digit error)", None),
    "offering_cross_filing_repeat": ("warning", "Same share quantity extracted from multiple filings (double count)", None),
    "offering_ungrounded_number": ("warning", "Extracted share count not present in the source quote", None),
    "offering_invalid_amount": ("error", "Offering amount is zero or negative", None),
    "offering_incomplete": ("warning", "Offering missing amount/quantity, date, category or status", None),
    "offering_amount_text_mismatch": ("warning", "amount and amount_text disagree by >5%", None),
    "offering_status_mismatch": ("warning", "Shelf/warrant capacity marked Active", None),
    "shelf_overdrawn": ("error", "Takedowns exceed live shelf capacity (negative remaining)", None),
    "atm_overdrawn": ("error", "ATM sales exceed ATM program size (negative remaining)", None),
    "history_duplicate_date": ("error", "Multiple share counts for one CIK+date", None),
    "history_invalid_shares": ("error", "Share count zero, negative or null", None),
    "history_future_or_bad_date": ("error", "Share-count date in the future or unparseable", None),
    "history_spike": ("error", "Single bad share-count point (jumps then reverts)", None),
    "history_units_error": ("error", "~1000x jump: a filing reported in thousands", None),
    "history_unadjusted_split": ("warning", "Jump matching a split ratio; history is not split-adjusted", None),
    "history_unexplained_jump": ("warning", "Share count jump >=50% not explained by a split", None),
    "stats_latest_mismatch": ("error", "stats.latest_shares differs from newest history point", None),
    "stats_arithmetic": ("error", "change_pct_1y does not equal latest/prev_shares_1y - 1", None),
    "stats_window_mismatch": ("warning", "'1Y' change computed over a window far from 365 days", None),
    "stats_split_contaminated": ("warning", "1Y ranking change spans a split/units error/spike", None),
    "stale_ticker": ("warning", "No new share-count data within the stale window", None),
    "missing_history": ("error", "Stats row without any share-count history", None),
    "orphan_rows": ("error", "Rows referencing a CIK not in companies", None),
    "thin_history": ("info", "Fewer than min_history_points share-count points", None),
    "coverage_shells": ("info", "Universe companies never synced", None),
    "price_lagging": ("warning", "Ticker's price feed lags the rest of the feed", None),
    "price_missing": ("info", "Ticker with stats but no prices", None),
}

RUNNERS: list[Callable[[AuditContext], list[Finding]]] = [
    check_feed_freshness,
    check_offering_exact_duplicates,
    check_offering_same_instrument,
    check_offering_near_duplicates,
    check_offering_cross_filing_repeat,
    check_offering_ungrounded_number,
    check_offering_values,
    check_capacity_reconciliation,
    check_history_duplicates,
    check_history_values,
    check_history_jumps,
    check_stats_consistency,
    check_stats_split_contamination,
    check_stale_filings,
    check_missing_history,
]


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only (fails instead of creating a file)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def run_audit(conn: sqlite3.Connection, cfg: AuditConfig | None = None, db_label: str = "") -> AuditResult:
    cfg = cfg or AuditConfig()
    t0 = time.time()
    ctx = AuditContext(conn, cfg)
    findings: list[Finding] = []
    timings = {}
    for fn in RUNNERS:
        t = time.time()
        try:
            res = fn(ctx)
        except Exception:  # a broken check must not hide the others
            logger.exception("check %s failed", fn.__name__)
            res = []
        timings[fn.__name__] = round(time.time() - t, 3)
        logger.info("%-40s %5d finding(s) in %.2fs", fn.__name__, len(res), timings[fn.__name__])
        findings.extend(res)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.check, f.ticker or "", f.message))
    meta = {
        "db": db_label,
        "as_of": cfg.as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - t0, 2),
        "row_counts": {
            "companies": len(ctx.companies), "history": len(ctx.history),
            "offerings": len(ctx.offerings), "stats": len(ctx.stats),
        },
        "config": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(cfg).items()},
        "timings_s": timings,
    }
    return AuditResult(findings=findings, meta=meta)
