import hashlib
import sqlite3
from datetime import date

import pytest

from app.data_quality import AuditConfig, run_audit
from app.data_quality.__main__ import main as dq_main
from app.data_quality.checks import (classify_ratio, find_jumps, numbers_in_text, open_readonly,
                                     parse_dollar_text, parse_shares_text, split_source_filing)
from app.data_quality.dedupe import plan_dedupe
from app.data_quality.report import write_all

from dq_fixtures import build_fixture_db

AS_OF = date(2026, 4, 15)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    return build_fixture_db(tmp_path_factory.mktemp("dq") / "fixture.db")


@pytest.fixture(scope="module")
def result(db_path):
    conn = open_readonly(str(db_path))
    try:
        return run_audit(conn, AuditConfig(as_of=AS_OF), db_label="fixture")
    finally:
        conn.close()


def _by(result, check):
    return [f for f in result.findings if f.check == check]


def _tickers(result, check):
    return {f.ticker for f in _by(result, check)}


# --- pure helpers -----------------------------------------------------------
@pytest.mark.parametrize("ratio,expected", [
    (2.0, "split"), (0.5, "split"), (1 / 10, "split"), (6.953, "split"), (1.5, "split"),
    (1000.2, "units_error"), (0.001, "units_error"), (1_003_012.0, "units_error"),
    (1.8, None), (1.23, None),
])
def test_classify_ratio(ratio, expected):
    assert classify_ratio(ratio) == expected


def test_parsers():
    assert parse_shares_text("2,230,000 shares") == 2_230_000
    assert parse_shares_text("$7.5M") is None
    assert parse_dollar_text("$13.7M") == pytest.approx(13_700_000)
    assert parse_dollar_text("$21K") == 21_000
    assert parse_dollar_text("765,796 shares") is None
    assert split_source_filing("424B5:0001493152-25-029223:d9f18a61") == ("424B5", "0001493152-25-029223")
    assert split_source_filing("SEC:abcd1234") == ("SEC", None)
    assert 2_349_675 in numbers_in_text("2,349,675 shares at $2.36")


def test_find_jumps_spike_is_reported_once():
    s = [{"id": i, "date": f"2025-0{i}-01", "shares": v, "source": "10-Q"}
         for i, v in enumerate([100, 100_000, 101], start=1)]
    jumps = find_jumps(s, AuditConfig())
    assert [j["kind"] for j in jumps] == ["spike"]


# --- end-to-end on the fixture DB ---------------------------------------------
def test_clean_company_only_flags_its_split(result):
    good = [f for f in result.findings if f.ticker == "GOOD"]
    assert {f.check for f in good} == {"history_unadjusted_split", "stats_split_contaminated"}


def test_genuine_dilution_is_unexplained_not_split(result):
    assert "DIL" in _tickers(result, "history_unexplained_jump")
    assert "DIL" not in _tickers(result, "history_unadjusted_split")
    assert "DIL" not in _tickers(result, "stats_split_contaminated")


def test_history_defects(result):
    assert "BAD" in _tickers(result, "history_spike")
    assert "BAD" in _tickers(result, "history_invalid_shares")
    assert "BAD" in _tickers(result, "history_future_or_bad_date")
    dup = _by(result, "history_duplicate_date")
    assert len(dup) == 1 and dup[0].details["values"] == [51_000_000, 52_000_000]


def test_stats_defects(result):
    assert "BAD" in _tickers(result, "stats_latest_mismatch")
    assert _tickers(result, "stats_arithmetic") == {"OLD"}  # 10.2M/10M is +2%, stored +5%
    assert "BAD" in _tickers(result, "stats_window_mismatch")
    assert "GOOD" not in _tickers(result, "stats_arithmetic")
    assert "NOHIST" in _tickers(result, "missing_history")


def test_staleness_and_coverage(result):
    assert _tickers(result, "stale_ticker") == {"OLD"}
    shells = _by(result, "coverage_shells")
    assert shells and "SHELL" in shells[0].details["sample"]
    assert "BAD" in _tickers(result, "price_lagging")
    assert {"OLD", "NOHIST"} <= _tickers(result, "price_missing")


def test_feed_late_only_when_old(db_path):
    conn = open_readonly(str(db_path))
    try:
        fresh = run_audit(conn, AuditConfig(as_of=date(2026, 4, 3)))
        late = run_audit(conn, AuditConfig(as_of=date(2026, 6, 1)))
    finally:
        conn.close()
    assert not [f for f in fresh.findings if f.check == "feed_late"]
    assert len([f for f in late.findings if f.check == "feed_late"]) == 2


def test_offering_defects(result):
    same = _by(result, "offering_same_instrument_duplicate")
    assert len(same) == 1 and sorted(same[0].details["ids"]) == [1, 2]
    near = _by(result, "offering_near_duplicate")
    assert len(near) == 1 and sorted(near[0].details["ids"]) == [3, 4]
    ungrounded = _by(result, "offering_ungrounded_number")
    assert [f.details["ids"] for f in ungrounded] == [[4]]
    repeat = _by(result, "offering_cross_filing_repeat")
    assert len(repeat) == 1 and repeat[0].details["quantity"] == 7_143_000
    assert len(_by(result, "offering_invalid_amount")) == 1
    assert {f.details["ids"][0] for f in _by(result, "offering_incomplete")} == {8}
    assert [f.details["ids"] for f in _by(result, "offering_amount_text_mismatch")] == [[11]]
    assert [f.details["ids"] for f in _by(result, "offering_status_mismatch")] == [[12]]


def test_shelf_reconciliation(result):
    over = _by(result, "shelf_overdrawn")
    assert len(over) == 1 and over[0].ticker == "DIL"
    assert over[0].details["remaining"] < 0


def test_summary_counts_add_up(result):
    s = result.summary()
    assert s["total_findings"] == len(result.findings)
    assert sum(s["by_severity"].values()) == len(result.findings)
    assert sum(c["count"] for c in s["by_check"].values()) == len(result.findings)


# --- read-only guarantees -------------------------------------------------------
def test_audit_never_writes(db_path, tmp_path):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert dq_main(["audit", "--db", str(db_path), "--out", str(tmp_path), "--as-of", "2026-04-15"]) == 0
    assert dq_main(["dedupe", "--db", str(db_path), "--out", str(tmp_path)]) == 0
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    conn = open_readonly(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM offerings")
    conn.close()


def test_fail_on_exit_code(db_path, tmp_path):
    assert dq_main(["audit", "--db", str(db_path), "--out", str(tmp_path), "--fail-on", "error"]) == 1


def test_open_readonly_does_not_create_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        open_readonly(str(tmp_path / "nope.db")).execute("SELECT 1")
    assert not (tmp_path / "nope.db").exists()


def test_reports_written(result, tmp_path):
    paths = write_all(result, tmp_path)
    html = paths["html"].read_text()
    assert "Data Quality Audit" in html and "offering_near_duplicate" in html
    lines = paths["csv"].read_text().splitlines()
    assert lines[0].startswith("severity,check,ticker")
    assert len(lines) == len(result.findings) + 1


# --- dry-run dedupe plan --------------------------------------------------------
def test_dedupe_plan(db_path):
    conn = open_readonly(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM offerings")]
    conn.close()
    actions = {a.drop_id: a for a in plan_dedupe(rows)}
    assert actions[2].keep_id == 1 and actions[2].rule == "same_instrument"
    assert actions[4].keep_id == 3 and actions[4].rule == "near_duplicate"
    assert actions[6].keep_id == 5 and actions[6].confidence == "review"
    assert 3 not in actions and 5 not in actions
