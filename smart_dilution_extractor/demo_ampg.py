"""
End-to-end test/demo on AMPG (Amplify Energy Corp.).
Run from the share_buyback_tracker directory:
    backend/venv/bin/python -m smart_dilution_extractor.demo_ampg
"""
from __future__ import annotations

import argparse
import json

from .config import PROSPECTUS_FORMS
from .pipeline import run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AMPG")
    ap.add_argument("--max-filings", type=int, default=10)
    ap.add_argument("--no-llm", action="store_true", help="skip Ollama/DeepSeek, rules+XBRL only")
    ap.add_argument("--prospectus-only", action="store_true",
                    help="only process 424B/FWP offering docs (fastest, richest)")
    args = ap.parse_args()

    forms = PROSPECTUS_FORMS if args.prospectus_only else None

    result = run(
        args.ticker,
        max_filings=args.max_filings,
        use_llm=not args.no_llm,
        forms_filter=forms,
    )

    print("\n" + "=" * 78)
    print(f" DILUTION REPORT — {result.company} ({result.ticker}, CIK {result.cik})")
    print("=" * 78)

    print(f"\nXBRL facts pulled: {len(result.xbrl_facts)}")
    taxes = sorted({f["taxonomy"] for f in result.xbrl_facts})
    print(f"Taxonomies present: {taxes}")

    active = [e for e in result.events if e.dilution_category == "active"]
    potential = [e for e in result.events if e.dilution_category == "potential"]
    act_sum = sum(e.gross_proceeds_usd or 0 for e in active)
    pot_sum = sum(e.gross_proceeds_usd or 0 for e in potential)
    print(f"\nCanonical dilution events: {len(result.events)}  "
          f"(ACTIVE={len(active)}  POTENTIAL={len(potential)})")
    print(f"  Active dilution  $ (sum of proceeds):   ${act_sum:,.0f}")
    print(f"  Potential dilution $ (shelf/converts):  ${pot_sum:,.0f}\n")

    for i, ev in enumerate(result.events, 1):
        cat = ev.dilution_category.upper()
        shares = f"{ev.shares:,.0f}" if ev.shares else "—"
        price = f"${ev.price_per_share:,.4f}" if ev.price_per_share else "—"
        gross = f"${ev.gross_proceeds_usd:,.0f}" if ev.gross_proceeds_usd else "—"
        print(f"[{i:02d}] [{cat:9}] {ev.offering_type.value:18} status={ev.status.value:10} "
              f"conf={ev.confidence:.2f}  via={ev.extraction_method}")
        print(f"     shares={shares}  price={price}  gross={gross}  "
              f"date={ev.event_date or ev.filing_date}")
        print(f"     src={ev.source_form} {ev.source_accession}")
        print(f"     flags={ev.validation_flags}")
        if ev.source_quote:
            q = ev.source_quote[:160].replace("\n", " ")
            print(f"     quote: \"{q}\"")
        print()

    # Persist machine-readable output for inspection.
    out = {
        "ticker": result.ticker,
        "cik": result.cik,
        "company": result.company,
        "taxonomies": taxes,
        "n_xbrl_facts": len(result.xbrl_facts),
        "events": [
            {**json.loads(ev.model_dump_json()), "dilution_category": ev.dilution_category}
            for ev in result.events
        ],
    }
    from .config import CACHE_DIR
    path = CACHE_DIR / f"report_{result.ticker}.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Full JSON report written to: {path}")


if __name__ == "__main__":
    main()
