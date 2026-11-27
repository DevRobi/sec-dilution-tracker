"""FastAPI smoke tests against a throwaway SQLite DB (no startup hooks, no network)."""
import pytest
from fastapi.testclient import TestClient

from conftest import API_DB_PATH, DQ_REPORT_DIR
from dq_fixtures import build_fixture_db


@pytest.fixture(scope="module")
def client():
    if not API_DB_PATH.exists():
        build_fixture_db(API_DB_PATH)
    from app.main import app
    return TestClient(app)  # not used as a context manager -> startup (SEC fetch) is skipped


def test_root(client):
    assert client.get("/").json()["status"] == "ok"


def test_ticker_detail(client):
    r = client.get("/tickers/GOOD")
    assert r.status_code == 200 and r.json()["cik"] == "1"
    hist = client.get("/tickers/GOOD/history").json()
    assert len(hist) == 6
    stats = client.get("/tickers/GOOD/stats").json()
    assert stats["latest_shares"] == 192_000_000


def test_offerings(client):
    rows = client.get("/tickers/BAD/offerings").json()
    assert len(rows) == 8


def test_rankings(client):
    r = client.get("/rankings", params={"type": "dilution", "years": 1, "limit": 10})
    assert r.status_code == 200
    assert {x["ticker"] for x in r.json()} >= {"DIL"}


def test_data_quality_endpoints(client):
    from datetime import date
    from app.data_quality import AuditConfig, run_audit
    from app.data_quality.checks import open_readonly
    from app.data_quality.report import write_all

    assert client.get("/data-quality/summary").status_code == 404  # before any audit
    conn = open_readonly(str(API_DB_PATH))
    write_all(run_audit(conn, AuditConfig(as_of=date(2026, 4, 15))), DQ_REPORT_DIR)
    conn.close()

    s = client.get("/data-quality/summary").json()
    assert s["total_findings"] > 0 and "by_check" in s
    f = client.get("/data-quality/findings", params={"check": "shelf_overdrawn"}).json()
    assert f["total"] == 1 and f["items"][0]["ticker"] == "DIL"
    assert client.get("/data-quality/report").status_code == 200
