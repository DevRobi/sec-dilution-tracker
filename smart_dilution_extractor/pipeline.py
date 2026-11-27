"""
End-to-end orchestrator.

    ticker
      -> resolve CIK
      -> XBRL structured facts  (us-gaap + ifrs, deterministic)   [free truth]
      -> list dilution filings
           for each filing:
             -> download
             -> Docling -> markdown (tables preserved)
             -> chunker -> top-K dilution-relevant chunks
             -> rules (regex) + LLM (Claude Haiku 5.5 by default) extraction
      -> validate (arithmetic + magnitude + XBRL cross-check)
      -> reconcile across filings
      -> report
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import chunker, config, docling_parser, edgar_client, llm_extractor, reconcile, rules, validators, xbrl_layer
from .models import DilutionEvent, FilingRef


@dataclass
class PipelineResult:
    ticker: str
    cik: str
    company: str
    xbrl_facts: list[dict] = field(default_factory=list)
    events: list[DilutionEvent] = field(default_factory=list)
    filings_processed: list[FilingRef] = field(default_factory=list)


def _xbrl_events(facts: list[dict]) -> list[DilutionEvent]:
    """Turn deterministic proceeds facts into baseline events."""
    out = []
    for f in xbrl_layer.proceeds_facts(facts):
        out.append(
            DilutionEvent(
                offering_type="other",
                status="completed",
                gross_proceeds_usd=float(f["value"]),
                event_date=f.get("period_end"),
                source_quote=f"XBRL {f['taxonomy']}:{f['concept']} = {f['value']} {f['unit']}",
                extraction_method="xbrl",
                source_form=f.get("form") or "",
                source_accession=f.get("accession") or "",
                filing_date=f.get("filed") or "",
                confidence=0.9,
            )
        )
    return out


def run(
    ticker: str,
    max_filings: int = 8,
    use_llm: bool = True,
    forms_filter: set[str] | None = None,
    prioritize: bool = True,
    verbose: bool = True,
) -> PipelineResult:
    def log(*a):
        if verbose:
            print(*a, flush=True)

    cik, company = edgar_client.resolve_cik(ticker)
    log(f"[CIK] {ticker} -> {cik} ({company})")

    # 1. Deterministic XBRL layer
    facts = [
        fact
        for fact in xbrl_layer.extract_xbrl_facts(cik)
        if fact.get("form") in config.DILUTION_FORMS
    ]
    log(f"[XBRL] {len(facts)} dilution-relevant facts "
        f"(taxonomies: {sorted({f['taxonomy'] for f in facts})})")
    xbrl_proceeds = [f["value"] for f in xbrl_layer.proceeds_facts(facts)]

    all_events: list[DilutionEvent] = _xbrl_events(facts)

    # 2. Document layer
    filings = edgar_client.list_dilution_filings(cik)
    if forms_filter:
        filings = [f for f in filings if f.form in forms_filter]
    if prioritize:
        # Process the most dilution-informative forms first (prospectus &
        # registration carry the richest offering/shelf tables), then by recency.
        # Otherwise a 6-K-heavy issuer like POET buries its 424B5s/F-3s.
        filings.sort(key=lambda f: (config.FORM_PRIORITY.get(f.form, 0), f.filing_date),
                     reverse=True)
    filings = filings[:max_filings]
    log(f"[FILINGS] processing {len(filings)} filings")

    processed: list[FilingRef] = []
    for ref in filings:
        try:
            path = edgar_client.download_filing(ref)
            md = docling_parser.to_markdown(path)
            scored = chunker.select_relevant(md)
            doc_events: list[DilutionEvent] = []

            # rules on the highest-scoring chunks' text
            for _s, chunk in scored:
                doc_events.extend(rules.extract_rule_based(chunk, ref.form))
            # LLM on the same chunks
            if use_llm:
                doc_events.extend(llm_extractor.extract_chunks(scored))

            for ev in doc_events:
                ev.source_form = ref.form
                ev.source_accession = ref.accession
                ev.source_url = ref.url
                ev.filing_date = ref.filing_date

            log(f"  {ref.filing_date} {ref.form:7} chunks={len(scored)} "
                f"events={len(doc_events)}  ({ref.accession})")
            all_events.extend(doc_events)
            processed.append(ref)
        except Exception as exc:  # noqa: BLE001
            log(f"  !! {ref.form} {ref.accession} failed: {exc!r}")

    # 3. Validate + 4. Reconcile
    all_events = validators.validate_all(all_events, xbrl_proceeds)
    canonical = reconcile.reconcile(all_events)
    log(f"[RECONCILE] {len(all_events)} raw -> {len(canonical)} canonical events")

    return PipelineResult(
        ticker=ticker.upper(),
        cik=cik,
        company=company,
        xbrl_facts=facts,
        events=canonical,
        filings_processed=processed,
    )
