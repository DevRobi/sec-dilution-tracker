# Smart Dilution Extractor

Extracts dilution events (share counts **and** dollar proceeds) from SEC filings
using a **hybrid, laptop-friendly** pipeline. No GPU, no paid API — everything
runs on a single machine.

The design principle: **let deterministic sources do the easy 80%, and spend the
local LLM only on the hard parts.** That keeps it fast, cheap, and auditable.

---

## Why this approach (top to bottom)

```
ticker
  │
  ├─(1) EDGAR acquisition        edgar_client.py
  │      ticker→CIK, pull submissions, keep only dilution-bearing forms,
  │      download + cache primary docs (rate-limited to <10 req/s)
  │
  ├─(2) XBRL structured layer    xbrl_layer.py          ← "free truth"
  │      Deterministic facts from data.sec.gov XBRL API.
  │      Covers us-gaap AND ifrs-full (foreign issuers like POET file IFRS!).
  │      Zero compute, exact numbers, tied to accession numbers.
  │
  ├─(3) Docling layout parsing   docling_parser.py      ← the cutting-edge bit
  │      HTML/PDF → markdown with TABLES PRESERVED. SEC dilution figures live
  │      in offering-terms / capitalization tables; naive HTML-stripping shreds
  │      them and the LLM then swaps columns. Docling keeps them intact.
  │      CPU-only for HTML, ~7s for a 1 MB prospectus. Results cached.
  │
  ├─(4) Relevance routing        chunker.py
  │      Split into sections, score for dilution relevance (keyword weights +
  │      $/share numeric density + table rows, boilerplate penalized), keep
  │      only top-K chunks. This is what makes the 8B model fast on a laptop.
  │
  ├─(5) Deterministic regex      rules.py
  │      Stereotyped offering language ("aggregate offering price of $X",
  │      "N shares at $Y per share"). Catches standard raises with zero LLM
  │      cost. Distinguishes option/warrant OVERHANG from real cash raises.
  │
  ├─(6) LLM extraction           llm_extractor.py / llm_providers.py
  │      Claude Haiku 5.5 with structured outputs (JSON schema), via the
  │      Claude Code CLI or the Anthropic API; local Ollama as an opt-in.
  │      Runs only on the top-K chunks. Few-shot teaches the authorized-vs-
  │      issued and IFRS "ordinary shares" traps. Handles the messy cases regex
  │      can't (e.g. linking "20,689,656 shares" to "$150,000,000 proceeds").
  │
  ├─(7) Validation               validators.py
  │      Arithmetic coherence (shares×price ≈ proceeds, ±3%), magnitude sanity,
  │      and XBRL cross-check → 0..1 confidence + human-readable flags. This is
  │      the net that catches small-model hallucinations.
  │
  └─(8) Reconciliation           reconcile.py
         Same offering appears in F-3 → 424B5 → 6-K → 20-F. Collapse to one
         canonical record using the highest-authority form's numbers, attach all
         supporting accessions, flag >5% cross-filing discrepancies.
```

### What makes it "cutting edge" but light

| Lever | Effect |
|---|---|
| **XBRL-first** | Most numbers come for free, deterministically — minimizes LLM calls |
| **Docling table-aware parsing** | The single biggest accuracy win vs. naive text stripping |
| **JSON-schema-constrained decoding** | Small (8B) model becomes reliable for structured output |
| **Relevance routing** | LLM sees ~2-5 pages, not 200 — laptop-fast |
| **Arithmetic + XBRL cross-validation** | Catches the errors an 8B model will make |
| **Multi-taxonomy** | Works for US (us-gaap) *and* foreign (ifrs-full) issuers |

---

## Install & run

```bash
# from the share_buyback_tracker directory
backend/venv/bin/pip install -r smart_dilution_extractor/requirements.txt
# LLM: default is Claude via the Claude Code CLI (log in once with `claude` -> /login),
# or export ANTHROPIC_API_KEY and DILUTION_LLM_PROVIDER=anthropic.

export SEC_USER_AGENT="your-app you@example.com"   # SEC requires a contact UA

# Full hybrid run (XBRL + Docling + rules + LLM)
backend/venv/bin/python -m smart_dilution_extractor.demo_poet

# Fast deterministic-only (XBRL + rules, no LLM) — good for CI / smoke test
backend/venv/bin/python -m smart_dilution_extractor.demo_poet --no-llm

# Only the richest offering docs (424B/FWP)
backend/venv/bin/python -m smart_dilution_extractor.demo_poet --prospectus-only
```

Programmatic use:

```python
from smart_dilution_extractor import run
result = run("POET", max_filings=8, use_llm=True)
for ev in result.events:
    print(ev.offering_type, ev.shares, ev.gross_proceeds_usd, ev.confidence)
```

A machine-readable report is written to `cache/report_<TICKER>.json`.

---

## Test case: POET Technologies (CIK 0001437424)

POET is a deliberately hard case — a Canadian **foreign private issuer**:

- Files **6-K / 20-F / 424B5**, not 10-Q/10-K/8-K.
- Reports under **IFRS** (`ifrs-full`), so a us-gaap-only extractor finds **nothing**.
  This pipeline pulls `ProceedsFromIssueOfOrdinaryShares`,
  `ProceedsFromIssuingShares`, etc. (~148 facts).
- Dilution terms live in **424B5 prospectus tables** — Docling parses these
  cleanly; the rules+LLM layer reads off shares, price, and proceeds.

Run produces a reconciled event list spanning the XBRL proceeds history (back to
2015) plus document-level offering events from the recent 424B5 shelf takedowns,
each with a confidence score and the verbatim source quote.

---

## Files

| File | Role |
|---|---|
| `config.py` | SEC endpoints, XBRL tag map, keyword weights, form sets |
| `models.py` | Pydantic schemas (also the LLM's constrained-output schema) |
| `edgar_client.py` | Acquisition + caching + rate limiting |
| `xbrl_layer.py` | Deterministic multi-taxonomy fact extraction |
| `docling_parser.py` | Layout-aware HTML/PDF → markdown |
| `chunker.py` | Dilution-relevance scoring + chunk selection |
| `rules.py` | Regex extraction + overhang detection |
| `llm_extractor.py` / `llm_providers.py` | Claude (claude-code CLI / Anthropic API) structured-output extraction; Ollama opt-in |
| `validators.py` | Arithmetic / magnitude / XBRL cross-check + confidence |
| `reconcile.py` | Cross-filing dedup / merge |
| `pipeline.py` | Orchestrator |
| `demo_poet.py` | End-to-end demo on POET |

## Production notes (not yet wired)

- A **human-review queue** for `confidence < ~0.5` events; rejections become new
  few-shot examples.
- **Incremental polling** of the EDGAR RSS feed for new filings.
- Swap the model with `DILUTION_LLM_MODEL` (default `claude-haiku-5-5`) — the
  structured-output schema is the same for every provider.
