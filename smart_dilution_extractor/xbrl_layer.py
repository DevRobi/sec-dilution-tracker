"""
Structured ("free truth") layer.

Pulls deterministic dilution facts from SEC's XBRL companyfacts API across
BOTH us-gaap and ifrs-full taxonomies (foreign private issuers like POET file
IFRS, not us-gaap). No LLM, no compute — these numbers are exact and are later
used to cross-validate the LLM/regex extractions.
"""
from __future__ import annotations

import json

import requests

from . import config
from .edgar_client import _get


def fetch_company_facts(cik: str) -> dict:
    cache = config.CACHE_DIR / f"facts_{cik}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    try:
        data = _get(config.COMPANY_FACTS_URL.format(cik=cik)).json()
    except requests.HTTPError:
        data = {"facts": {}}
    cache.write_text(json.dumps(data))
    return data


def extract_xbrl_facts(cik: str) -> list[dict]:
    """
    Return a flat list of dilution-relevant facts:
      {taxonomy, concept, role, value, unit, period_end, accession, form, filed, frame}
    Each fact is tied to an accession number so it can be reconciled against
    document-level extractions.
    """
    facts_doc = fetch_company_facts(cik)
    facts = facts_doc.get("facts", {})
    out: list[dict] = []

    for taxonomy, concept_map in config.XBRL_TAGS.items():
        tax_facts = facts.get(taxonomy, {})
        for concept, role in concept_map.items():
            node = tax_facts.get(concept)
            if not node:
                continue
            for unit, points in node.get("units", {}).items():
                for p in points:
                    out.append(
                        {
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "role": role,
                            "value": p.get("val"),
                            "unit": unit,
                            "period_end": p.get("end"),
                            "period_start": p.get("start"),
                            "accession": p.get("accn"),
                            "form": p.get("form"),
                            "filed": p.get("filed"),
                            "frame": p.get("frame"),
                        }
                    )
    # newest first
    out.sort(key=lambda r: r.get("filed") or "", reverse=True)
    return out


def proceeds_facts(facts: list[dict]) -> list[dict]:
    """Just the cash-raise facts, handy for cross-validation."""
    return [f for f in facts if f["role"] == "gross_proceeds" and f["value"]]


def shares_outstanding_timeline(facts: list[dict]) -> list[dict]:
    """Chronological shares-outstanding series for computing % dilution."""
    so = [f for f in facts if f["role"] == "shares_outstanding" and f["value"]]
    so.sort(key=lambda r: r.get("period_end") or r.get("filed") or "")
    return so
