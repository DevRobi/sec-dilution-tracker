from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from smart_dilution_extractor import reconcile, rules


SNIPPET = """
We are offering 2,230,000 Units at an offering price of $4.055 per Unit.
Each Unit consists of one share of common stock, one Series A right to purchase
one share of Common Stock ("Series A Right"), and one Series B right to purchase
one share of Common Stock ("Series B Right"). The exercise price of the Series
Rights shall be equal to (i) in the case of the Series A Rights, $5.00 per share;
and (ii) in the case of the Series B Rights, $6.00 per share.
"""


class SeriesRightsExtractionTests(unittest.TestCase):
    def test_unit_offering_extracts_active_sale_and_two_rights(self):
        events = rules.extract_rule_based(SNIPPET, "424B5")

        active = [event for event in events if event.offering_type == "follow_on"]
        rights = [event for event in events if event.offering_type == "warrant_exercise"]

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].shares, 2_230_000)
        self.assertEqual(active[0].price_per_share, 4.055)
        self.assertEqual(active[0].gross_proceeds_usd, 9_042_650)

        self.assertEqual(len(rights), 2)
        self.assertEqual(
            sorted((event.shares, event.price_per_share) for event in rights),
            [(2_230_000, 5.0), (2_230_000, 6.0)],
        )

    def test_reconcile_and_db_fingerprint_preserve_equal_share_right_tranches(self):
        events = [
            event
            for event in rules.extract_rule_based(SNIPPET, "424B5")
            if event.offering_type == "warrant_exercise"
        ]
        for event in events:
            event.source_form = "424B5"
            event.source_accession = "0001493152-26-003618"
            event.filing_date = "2026-01-26"

        canonical = reconcile.reconcile(events)
        self.assertEqual(
            sorted((event.shares, event.price_per_share) for event in canonical),
            [(2_230_000, 5.0), (2_230_000, 6.0)],
        )

        backend_dir = Path(__file__).resolve().parents[1] / "backend"
        sys.path.insert(0, str(backend_dir))
        from app.services.dilution_engine import _event_to_row

        rows = [_event_to_row(event, "0001518461") for event in canonical]
        self.assertEqual(len({row["source_filing"] for row in rows}), 2)
        self.assertEqual({row["category"] for row in rows}, {"Unit Rights"})
        self.assertEqual({row["status"] for row in rows}, {"Potential"})
        self.assertEqual({row["amount_text"] for row in rows}, {"2,230,000 shares"})

    def test_unknown_ai_category_maps_to_other(self):
        backend_dir = Path(__file__).resolve().parents[1] / "backend"
        sys.path.insert(0, str(backend_dir))
        from app.services.dilution_engine import _event_to_row

        event = SimpleNamespace(
            event_date="2026-02-01",
            filing_date="2026-02-01",
            source_form="8-K",
            source_accession="0000000000-26-000001",
            source_url="https://example.com/filing",
            source_quote="Uncategorized financing instrument with active dilution.",
            offering_type="uncategorized_ai_type",
            gross_proceeds_usd=1_250_000,
            shares=None,
            price_per_share=None,
            dilution_category="active",
        )

        row = _event_to_row(event, "0001518461")
        self.assertEqual(row["category"], "Other")
        self.assertEqual(row["status"], "Active")


if __name__ == "__main__":
    unittest.main()
