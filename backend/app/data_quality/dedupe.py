"""
Dry-run de-duplication plan for the offerings table.

Never writes to the database. Produces a list of rows that *would* be removed,
which row each one would be merged into, and why, so a human can review it
before anything destructive happens:

    python -m app.data_quality dedupe --db tracker.db --out reports/data_quality

Rules (most to least confident):
  1. same_instrument   same filing + category + quantity + price stored twice
                       -> keep the most complete row (has $ amount), then lowest id.
  2. near_duplicate    same filing + category, quantities within 1%, and exactly
                       one of them matches a number in its own source quote
                       -> keep the grounded row, drop the misread one.
  3. cross_filing      the same share quantity re-extracted from a LATER filing
                       (a prospectus recapping an earlier raise or re-listing
                       the same warrants) -> keep the earliest row. Marked
                       'review' because a genuinely new, identically-sized
                       tranche is possible.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .checks import (numbers_in_text, offering_price, offering_shares,
                     split_source_filing)


@dataclass
class MergeAction:
    drop_id: int
    keep_id: int
    cik: str
    rule: str
    confidence: str  # "safe" | "review"
    reason: str


def _completeness(row: dict) -> tuple:
    return (row.get("amount") is not None and row["amount"] > 0, bool(row.get("amount_text")), -row["id"])


def _grounded(row: dict) -> bool:
    qty = offering_shares(row)
    return bool(qty and qty in numbers_in_text(row.get("text")))


def plan_dedupe(offerings: list[dict], near_tol: float = 0.01) -> list[MergeAction]:
    actions: list[MergeAction] = []
    dropped: set[int] = set()

    def drop(row, keep, rule, conf, reason):
        if row["id"] in dropped or keep["id"] in dropped or row["id"] == keep["id"]:
            return
        dropped.add(row["id"])
        actions.append(MergeAction(row["id"], keep["id"], row["cik"], rule, conf, reason))

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for o in offerings:
        _, acc = split_source_filing(o.get("source_filing"))
        groups[(o["cik"], acc or o.get("source_filing"), o.get("category"))].append(o)

    # 1. exact same instrument
    for (cik, acc, cat), rows in groups.items():
        by_key: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            qty = offering_shares(r) or (round(r["amount"]) if r.get("amount") else None)
            if qty:
                by_key[(qty, offering_price(r))].append(r)
        for (qty, _), dup in by_key.items():
            if len(dup) > 1:
                keep = max(dup, key=_completeness)
                for r in dup:
                    drop(r, keep, "same_instrument", "safe", f"{cat} {qty:,} duplicated in {acc}")

    # 2. near duplicates resolved by quote grounding
    for (cik, acc, cat), rows in groups.items():
        live = [r for r in rows if r["id"] not in dropped and offering_shares(r)]
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                a, b = live[i], live[j]
                qa, qb = offering_shares(a), offering_shares(b)
                if qa == qb or abs(qa - qb) / max(qa, qb) > near_tol:
                    continue
                ga, gb = _grounded(a), _grounded(b)
                if ga != gb:
                    keep, bad = (a, b) if ga else (b, a)
                    drop(bad, keep, "near_duplicate", "safe",
                         f"{offering_shares(bad):,} not in its quote; {offering_shares(keep):,} is")

    # 3. cross-filing repeats
    by_qty: dict[tuple, list[dict]] = defaultdict(list)
    for o in offerings:
        qty = offering_shares(o)
        if qty and qty >= 10_000 and o["id"] not in dropped:
            by_qty[(o["cik"], qty)].append(o)
    for (cik, qty), rows in by_qty.items():
        accs = {split_source_filing(r.get("source_filing"))[1] for r in rows}
        if len(accs) < 2:
            continue
        rows = sorted(rows, key=lambda r: (r.get("date") or "", r["id"]))
        keep = rows[0]
        for r in rows[1:]:
            if split_source_filing(r.get("source_filing"))[1] != split_source_filing(keep.get("source_filing"))[1]:
                drop(r, keep, "cross_filing", "review",
                     f"{qty:,} shares first seen {keep.get('date')} ({keep.get('category')}), "
                     f"repeated {r.get('date')} ({r.get('category')})")
    return actions


def write_plan(actions: list[MergeAction], path: Path, tickers: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tickers = tickers or {}
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["drop_id", "keep_id", "ticker", "cik", "rule", "confidence", "reason"])
        for a in actions:
            w.writerow([a.drop_id, a.keep_id, tickers.get(a.cik, ""), a.cik, a.rule, a.confidence, a.reason])
    return path
