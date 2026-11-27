"""
Central configuration: SEC endpoints, headers, the curated XBRL tag map
(covering BOTH us-gaap and IFRS taxonomies), dilution keyword weights,
and the set of filing forms that actually carry dilution information.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# HTTP / SEC
# ---------------------------------------------------------------------------
# SEC requires a descriptive User-Agent with contact info. Override via env.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "smart-dilution-extractor research@example.com"
)
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SEC_RATE_LIMIT_PER_SEC = 8          # stay under SEC's 10 req/s ceiling

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_CONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"
)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# LLM extraction - Claude by default (see llm_providers.py)
#   DILUTION_LLM_PROVIDER = claude-code | anthropic | ollama
#   (unset -> "anthropic" if ANTHROPIC_API_KEY is set, else "claude-code")
# ---------------------------------------------------------------------------
DILUTION_LLM_PROVIDER = os.getenv("DILUTION_LLM_PROVIDER", "").strip().lower()

# Claude Haiku 5.5: cheapest current Claude model; extraction runs at low effort.
CLAUDE_MODEL = os.getenv("DILUTION_LLM_MODEL", "claude-haiku-5-5")
CLAUDE_EFFORT = os.getenv("DILUTION_LLM_EFFORT", "low")
CLAUDE_MAX_TOKENS = int(os.getenv("DILUTION_LLM_MAX_TOKENS", "16000"))
CLAUDE_CODE_BIN = os.getenv("CLAUDE_CODE_BIN", os.path.expanduser("~/.local/bin/claude"))
LLM_TIMEOUT_SECONDS = float(os.getenv("DILUTION_LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = int(os.getenv("DILUTION_LLM_MAX_RETRIES", "2"))

# Local Ollama (explicit opt-in: DILUTION_LLM_PROVIDER=ollama)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:8b")
OLLAMA_NUM_CTX = 8192

# ---------------------------------------------------------------------------
# Filing forms scanned by the AI dilution extractor
# ---------------------------------------------------------------------------
# Keep this allowlist intentionally narrow. Periodic reports (10-K/10-Q and
# variants) belong to the non-AI outstanding-share history path.
PROSPECTUS_FORMS = {
    "424B3", "424B4", "424B5", "FWP",
}
REGISTRATION_FORMS = {
    "S-1", "S-3", "F-1", "F-3",
}
CURRENT_REPORT_FORMS = {"8-K", "6-K"}
PROXY_FORMS = {"PRE 14A", "DEF 14A"}
EFFECTIVENESS_FORMS = {"EFFECT"}

DILUTION_FORMS = (
    PROSPECTUS_FORMS
    | REGISTRATION_FORMS
    | CURRENT_REPORT_FORMS
    | PROXY_FORMS
    | EFFECTIVENESS_FORMS
)

# Source authority for reconciliation (higher = more authoritative final numbers).
FORM_PRIORITY = {
    **{f: 100 for f in PROSPECTUS_FORMS},
    **{f: 80 for f in CURRENT_REPORT_FORMS},
    **{f: 60 for f in PROXY_FORMS},
    **{f: 40 for f in REGISTRATION_FORMS},
    **{f: 20 for f in EFFECTIVENESS_FORMS},
}

# ---------------------------------------------------------------------------
# Curated XBRL dilution tags — keyed by taxonomy.
# This is the deterministic "free truth" layer. Foreign private issuers
# (e.g. POET) report under ifrs-full, NOT us-gaap, so we cover both.
# ---------------------------------------------------------------------------
XBRL_TAGS = {
    "us-gaap": {
        "ProceedsFromIssuanceOfCommonStock": "gross_proceeds",
        "ProceedsFromIssuanceInitialPublicOffering": "gross_proceeds",
        "ProceedsFromIssuanceOfPrivatePlacement": "gross_proceeds",
        "StockIssuedDuringPeriodSharesNewIssues": "shares",
        "StockIssuedDuringPeriodValueNewIssues": "value",
        "SaleOfStockNumberOfSharesIssuedInTransaction": "shares",
        "SaleOfStockPricePerShare": "price",
        "SaleOfStockConsiderationReceivedOnTransaction": "gross_proceeds",
        "CommonStockSharesOutstanding": "shares_outstanding",
        "CommonStockSharesIssued": "shares_outstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding": "wavg_diluted",
        "WeightedAverageNumberOfSharesOutstandingBasic": "wavg_basic",
        "DebtConversionConvertedInstrumentSharesIssued1": "shares",
        "ClassOfWarrantOrRightOutstanding": "warrants_outstanding",
    },
    "ifrs-full": {
        "ProceedsFromIssueOfOrdinaryShares": "gross_proceeds",
        "ProceedsFromIssuingShares": "gross_proceeds",
        "IncreaseDecreaseThroughExerciseOfWarrantsEquity": "value",
        "NumberOfSharesOutstanding": "shares_outstanding",
        "NumberOfSharesIssued": "shares_outstanding",
        "WeightedAverageShares": "wavg_basic",
        "WeightedAverageNumberOfOrdinarySharesOutstandingDiluted": "wavg_diluted",
    },
    "dei": {
        "EntityCommonStockSharesOutstanding": "shares_outstanding",
    },
}

# ---------------------------------------------------------------------------
# Dilution-relevance keyword weights for chunk scoring.
# Higher weight = stronger signal that a section describes a capital raise.
# ---------------------------------------------------------------------------
KEYWORD_WEIGHTS = {
    "at-the-market": 6, "at the market": 6, "atm program": 6, "atm facility": 6,
    "gross proceeds": 5, "net proceeds": 5, "aggregate offering price": 5,
    "public offering price": 5, "per share": 3, "shares of common stock": 3,
    "common shares": 2, "registered direct": 5, "private placement": 5,
    "warrant": 3, "pre-funded warrant": 5, "convertible": 4, "prospectus supplement": 4,
    "we are offering": 6, "are offering": 4, "sold an aggregate": 5,
    "equity line": 5, "purchase agreement": 3, "underwriting discount": 4,
    "dilution": 4, "use of proceeds": 4, "placement agent": 4, "ordinary shares": 2,
    "shelf registration": 3, "issuance of": 2, "units consisting": 4,
}

# Sections to ignore even if keyword-rich (boilerplate / risk language).
NEGATIVE_SECTION_HINTS = [
    "risk factors", "forward-looking statements", "incorporation by reference",
    "where you can find", "legal matters", "experts", "table of contents",
]

MAX_CHUNK_CHARS = 1000000          # ~250k tokens; feed entire document to DeepSeek
TOP_K_CHUNKS = 2                   # With 1M chars, most docs fit in 1-2 chunks
ARITHMETIC_TOLERANCE = 0.03        # shares*price vs proceeds, 3%
