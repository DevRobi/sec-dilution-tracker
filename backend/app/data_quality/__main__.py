"""
CLI:
    python -m app.data_quality audit  --db tracker.db [--out DIR] [--as-of YYYY-MM-DD]
    python -m app.data_quality dedupe --db tracker.db [--out DIR]      # dry-run only

Exit code of `audit` is 0 unless --fail-on is given and a finding at or above
that severity exists (handy as a cron / CI gate after the daily sync).
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from .checks import SEVERITIES, AuditConfig, open_readonly, run_audit
from .dedupe import plan_dedupe, write_plan
from .report import write_all

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = BACKEND_DIR / "tracker.db"
DEFAULT_OUT = BACKEND_DIR / "reports" / "data_quality"


def _audit(args) -> int:
    cfg = AuditConfig(
        as_of=date.fromisoformat(args.as_of) if args.as_of else date.today(),
        stale_days=args.stale_days,
        refresh_days=args.refresh_days,
        jump_ratio=args.jump_ratio,
        skip_prices=args.skip_prices,
    )
    conn = open_readonly(args.db)
    try:
        result = run_audit(conn, cfg, db_label=str(args.db))
    finally:
        conn.close()
    paths = write_all(result, Path(args.out))

    sev = result.counts_by_severity()
    print(f"\nData-quality audit of {args.db} (as of {cfg.as_of}) in {result.meta['duration_s']}s")
    print("  " + "  ".join(f"{k}={v:,}" for k, v in sev.items()))
    for name, info in result.counts_by_check().items():
        if info["count"]:
            print(f"  [{info['severity']:7}] {name:38} {info['count']:>7,}")
    for k, p in paths.items():
        print(f"  {k:14} {p}")

    if args.fail_on:
        threshold = SEVERITIES.index(args.fail_on)
        if any(SEVERITIES.index(f.severity) <= threshold for f in result.findings):
            return 1
    return 0


def _dedupe(args) -> int:
    conn = open_readonly(args.db)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, cik, date, category, text, source_filing, amount, amount_text, status, description "
            "FROM offerings ORDER BY cik, date, id")]
        tickers = {r["cik"]: r["ticker"] for r in conn.execute("SELECT cik, ticker FROM companies")}
    finally:
        conn.close()
    actions = plan_dedupe(rows)
    path = write_plan(actions, Path(args.out) / "dedupe_plan.csv", tickers)
    by = Counter((a.rule, a.confidence) for a in actions)
    print(f"\nDRY RUN — no rows were changed. {len(actions)} of {len(rows)} offering rows would be merged:")
    for (rule, conf), n in sorted(by.items()):
        print(f"  {rule:16} {conf:7} {n:>5}")
    print(f"  plan: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.data_quality")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="run all data-quality checks and write HTML/CSV/JSON")
    a.add_argument("--db", default=str(DEFAULT_DB))
    a.add_argument("--out", default=str(DEFAULT_OUT))
    a.add_argument("--as-of", default=None, help="evaluate freshness relative to this date (default today)")
    a.add_argument("--stale-days", type=int, default=200)
    a.add_argument("--refresh-days", type=int, default=7)
    a.add_argument("--jump-ratio", type=float, default=1.5)
    a.add_argument("--skip-prices", action="store_true", help="skip the large prices-table scan")
    a.add_argument("--fail-on", choices=SEVERITIES, default=None)
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=_audit)

    d = sub.add_parser("dedupe", help="DRY-RUN plan of offering rows to merge (never writes)")
    d.add_argument("--db", default=str(DEFAULT_DB))
    d.add_argument("--out", default=str(DEFAULT_OUT))
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=_dedupe)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
