"""
Shared test setup.

Environment is pinned BEFORE any backend import so tests never touch the real
Postgres/SQLite database, the LLM pipeline, or the network:
  * DATABASE_URL -> a throwaway SQLite file in a temp dir
  * ENABLE_AI_EXTRACTION=false
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

_TMP = Path(tempfile.mkdtemp(prefix="dilution_tests_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'api_test.db'}"
os.environ["ENABLE_AI_EXTRACTION"] = "false"
os.environ.setdefault("JWT_SECRET_KEY", "test-only-not-a-secret")
os.environ["DATA_QUALITY_REPORT_DIR"] = str(_TMP / "dq_reports")
os.environ["SPLITS_YFINANCE"] = "false"          # no network in tests
os.environ["SPLITS_CACHE_DIR"] = str(_TMP / "splits_cache")

# backend/ must win over the repo root: the root holds the legacy Streamlit
# app.py, which would otherwise shadow the backend `app` package.
for p in (str(ROOT), str(BACKEND)):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

API_DB_PATH = _TMP / "api_test.db"
DQ_REPORT_DIR = _TMP / "dq_reports"
