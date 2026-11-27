from __future__ import annotations

import unittest
from unittest.mock import patch

from smart_dilution_extractor import config, edgar_client, pipeline


EXPECTED_AI_FORMS = {
    "8-K",
    "S-1",
    "S-3",
    "F-1",
    "F-3",
    "424B3",
    "424B4",
    "424B5",
    "EFFECT",
    "PRE 14A",
    "DEF 14A",
    "6-K",
    "FWP",
}


class AiFormAllowlistTests(unittest.TestCase):
    def test_allowlist_matches_requested_forms(self):
        self.assertEqual(config.DILUTION_FORMS, EXPECTED_AI_FORMS)
        self.assertNotIn("10-K", config.DILUTION_FORMS)
        self.assertNotIn("10-Q", config.DILUTION_FORMS)

    @patch.object(edgar_client, "_get")
    def test_edgar_listing_excludes_periodic_reports(self, mock_get):
        forms = list(EXPECTED_AI_FORMS) + ["10-K", "10-Q", "20-F", "S-3/A"]
        recent = {
            "form": forms,
            "accessionNumber": [
                f"0000000001-26-{index:06d}" for index in range(len(forms))
            ],
            "primaryDocument": [f"filing-{index}.htm" for index in range(len(forms))],
            "primaryDocDescription": [""] * len(forms),
            "filingDate": ["2026-06-01"] * len(forms),
        }
        mock_get.return_value.json.return_value = {"filings": {"recent": recent}}

        filings = edgar_client.list_dilution_filings("0000000001")

        self.assertEqual({filing.form for filing in filings}, EXPECTED_AI_FORMS)

    @patch.object(pipeline.reconcile, "reconcile", side_effect=lambda events: events)
    @patch.object(pipeline.validators, "validate_all", side_effect=lambda events, _: events)
    @patch.object(pipeline.edgar_client, "list_dilution_filings", return_value=[])
    @patch.object(
        pipeline.xbrl_layer,
        "extract_xbrl_facts",
        return_value=[
            {
                "taxonomy": "us-gaap",
                "concept": "ProceedsFromIssuanceOfCommonStock",
                "role": "gross_proceeds",
                "value": 100,
                "unit": "USD",
                "period_end": "2026-03-31",
                "accession": "allowed",
                "form": "8-K",
                "filed": "2026-04-01",
                "frame": None,
            },
            {
                "taxonomy": "us-gaap",
                "concept": "ProceedsFromIssuanceOfCommonStock",
                "role": "gross_proceeds",
                "value": 200,
                "unit": "USD",
                "period_end": "2025-12-31",
                "accession": "excluded",
                "form": "10-K",
                "filed": "2026-02-01",
                "frame": None,
            },
        ],
    )
    @patch.object(
        pipeline.edgar_client,
        "resolve_cik",
        return_value=("0000000001", "Example Corp"),
    )
    def test_pipeline_excludes_periodic_xbrl_facts(
        self,
        _resolve_cik,
        _extract_xbrl,
        _list_filings,
        _validate,
        _reconcile,
    ):
        result = pipeline.run("EXMP", use_llm=False, verbose=False)

        self.assertEqual([fact["form"] for fact in result.xbrl_facts], ["8-K"])
        self.assertEqual([event.source_form for event in result.events], ["8-K"])


if __name__ == "__main__":
    unittest.main()
