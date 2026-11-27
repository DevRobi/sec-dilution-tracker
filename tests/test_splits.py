"""Split adjustment: detection, restatement, repairs, yfinance events (mocked), rankings."""
import sys
import types
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import adjusted_stats, splits
from app.services.splits import SplitEvent, adjust_series, compute_changes, match_split_ratio


def series(*pairs):
    return [{"date": d, "shares": s, "source": "10-Q"} for d, s in pairs]


def adjusted(res):
    return {p.date: p.shares for p in res.points if p.status != "excluded"}


# --- ratio matching -------------------------------------------------------------
@pytest.mark.parametrize("ratio,expected", [
    (3.0, (3.0, True)), (2.04, (2.0, True)), (0.01, (0.01, True)),
    (0.01195, (0.01, False)),        # 1-for-100 plus ~20% same-quarter issuance (SIDU)
    (10.4, (10.0, False)),
    (1.5, None), (1.8, None), (2.3, None), (0.6, None),
])
def test_match_split_ratio(ratio, expected):
    m = match_split_ratio(ratio)
    if expected is None:
        assert m is None
    else:
        assert m[0] == pytest.approx(expected[0]) and m[1] == expected[1]


# --- detection from the series --------------------------------------------------------
def test_reverse_split_1_for_100():
    res = adjust_series(series(("2023-03-31", 50_000_000), ("2023-06-30", 51_000_000),
                               ("2023-09-30", 52_000_000), ("2023-12-31", 520_000),
                               ("2024-03-31", 530_000)))
    assert [(s.ratio, s.source) for s in res.splits] == [(0.01, "detected")]
    a = adjusted(res)
    assert a["2023-03-31"] == 500_000 and a["2024-03-31"] == 530_000
    assert compute_changes(res.points)["change_pct_1y"] == pytest.approx(530_000 / 500_000 * 100 - 100)


def test_forward_split_3_for_1():
    res = adjust_series(series(("2024-03-31", 10_000_000), ("2024-06-30", 10_000_000),
                               ("2024-09-30", 30_000_000), ("2024-12-31", 29_700_000),
                               ("2025-03-31", 29_400_000)))
    assert [s.ratio for s in res.splits] == [3.0]
    a = adjusted(res)
    assert a["2024-03-31"] == 30_000_000
    ch = compute_changes(res.points)
    assert ch["change_pct_1y"] == pytest.approx(-2.0)  # a real 2% buyback, not +194%


def test_two_splits_in_sequence_are_cumulative():
    res = adjust_series(series(("2020-12-31", 1_000_000), ("2021-03-31", 1_000_000),
                               ("2021-06-30", 2_000_000),            # 2-for-1
                               ("2021-09-30", 2_000_000), ("2022-03-31", 2_000_000),
                               ("2022-06-30", 200_000),              # 1-for-10
                               ("2022-09-30", 200_000)))
    assert [s.ratio for s in res.splits] == [2.0, 0.1]
    pts = {p.date: p for p in res.points}
    assert pts["2020-12-31"].factor == pytest.approx(0.2) and pts["2020-12-31"].shares == 200_000
    assert pts["2021-09-30"].factor == pytest.approx(0.1)
    assert pts["2022-09-30"].factor == 1.0
    assert len(set(adjusted(res).values())) == 1  # flat once restated


def test_units_error_one_off_is_repaired():
    res = adjust_series(series(("2024-03-31", 25_000_000), ("2024-06-30", 25_100),
                               ("2024-09-30", 25_200_000), ("2024-12-31", 25_300_000)))
    pts = {p.date: p for p in res.points}
    assert pts["2024-06-30"].status == "repaired" and pts["2024-06-30"].shares == 25_100_000
    assert pts["2024-06-30"].raw_shares == 25_100
    assert res.splits == []
    assert [i["kind"] for i in res.issues] == ["units_repaired"]


def test_persistent_units_change_restates_earlier_points():
    res = adjust_series(series(("2011-03-31", 23_440), ("2011-06-30", 23_445_000), ("2011-09-30", 23_500_000)))
    assert [(s.source, s.ratio) for s in res.splits] == [("units", 1000.0)]
    assert adjusted(res)["2011-03-31"] == 23_440_000


def test_spike_that_reverts_is_excluded_not_plotted():
    res = adjust_series(series(("2024-03-31", 330_000_000), ("2024-06-30", 605_000_000),
                               ("2024-09-30", 331_000_000), ("2024-12-31", 332_000_000)))
    pts = {p.date: p for p in res.points}
    assert pts["2024-06-30"].status == "excluded"
    assert "2024-06-30" not in adjusted(res) and res.splits == []
    assert compute_changes(res.points)["latest_shares"] == 332_000_000


def test_genuine_50pct_dilution_is_not_a_split():
    res = adjust_series(series(("2024-03-31", 10_000_000), ("2024-06-30", 10_100_000),
                               ("2024-09-30", 15_150_000), ("2024-12-31", 15_300_000),
                               ("2025-03-31", 15_400_000)))
    assert res.splits == [] and res.issues == []
    assert compute_changes(res.points)["change_pct_1y"] == pytest.approx(54.0)


def test_genuine_4x_issuance_is_kept():
    # 4.39x in a quarter (no clean ratio) must stay dilution, as SIDU did in 2024.
    res = adjust_series(series(("2023-12-31", 619_986), ("2024-03-31", 2_719_812), ("2024-06-30", 3_450_577)))
    assert res.splits == []


def test_split_at_latest_filing_is_flagged_unconfirmed():
    res = adjust_series(series(("2025-03-31", 10_000_000), ("2025-06-30", 10_000_000), ("2025-09-30", 40_000_000)))
    assert [s.ratio for s in res.splits] == [4.0]
    assert [i["kind"] for i in res.issues] == ["split_unconfirmed"]


# --- known events (yfinance) --------------------------------------------------------------
def test_event_guided_match_with_issuance_and_skip_when_already_restated():
    rows = series(("2023-06-30", 40_482_106), ("2023-09-30", 51_880_279), ("2023-12-31", 619_986),
                  ("2024-03-31", 2_719_812))
    res = adjust_series(rows, [SplitEvent(date(2023, 12, 20), 0.01), SplitEvent(date(2015, 1, 1), 2.0)])
    assert [(s.ratio, s.source, s.exact, s.effective_date) for s in res.splits] == [(0.01, "yfinance", False, "2023-12-31")]
    assert adjusted(res)["2023-09-30"] == 518_803  # restated with the exact event ratio
    assert res.skipped_events and res.skipped_events[0]["event_date"] == "2015-01-01"


def test_events_suppress_heuristic_false_positive():
    # A 2.0x jump with a known (unrelated) event elsewhere is NOT treated as a split.
    rows = series(("2024-03-31", 10_000_000), ("2024-06-30", 20_000_000), ("2024-09-30", 20_100_000))
    res = adjust_series(rows, [SplitEvent(date(2010, 1, 1), 3.0)])
    assert res.splits == []


@pytest.fixture
def fake_yf(monkeypatch, tmp_path):
    monkeypatch.setattr(splits, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("SPLITS_YFINANCE", "true")
    calls = {"n": 0}

    class Ticker:
        def __init__(self, t):
            self.t = t

        @property
        def splits(self):
            calls["n"] += 1
            if self.t == "BOOM":
                raise RuntimeError("network down")
            idx = pd.to_datetime(["2023-12-20"]).tz_localize("America/New_York")
            return pd.Series([0.01], index=idx)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    return calls


def test_fetch_split_events_caches_on_disk(fake_yf):
    ev = splits.fetch_split_events("SIDU")
    assert [(e.date, e.ratio) for e in ev] == [(date(2023, 12, 20), 0.01)]
    assert splits.fetch_split_events("SIDU") == ev and fake_yf["n"] == 1   # second call from cache
    assert splits.read_cached_events("SIDU")[0].ratio == 0.01


def test_fetch_split_events_failure_returns_none(fake_yf):
    assert splits.fetch_split_events("BOOM") is None


# --- rankings on a fixture DB ------------------------------------------------------------
def test_rankings_use_adjusted_changes(tmp_path):
    from app.routers.rankings import rank_rows

    rows = [
        ("A", "2024-12-31", 10_000_000, "10-K"), ("A", "2025-06-30", 10_000_000, "10-Q"),
        ("A", "2025-09-30", 250_000, "10-Q"), ("A", "2025-12-31", 250_000, "10-K"),     # 1-for-40: not a buyback
        ("B", "2024-12-31", 50_000_000, "10-K"), ("B", "2025-12-31", 40_000_000, "10-K"),  # real 20% buyback
        ("C", "2024-12-31", 10_000_000, "10-K"), ("C", "2025-12-31", 30_000_000, "10-K"),  # 3-for-1: not dilution
        ("D", "2024-12-31", 10_000_000, "10-K"), ("D", "2025-12-31", 15_000_000, "10-K"),  # real 50% dilution
    ]
    uni = adjusted_stats.build_universe(rows, {"A": "AAA", "B": "BBB", "C": "CCC", "D": "DDD"})

    def stat(cik, raw):
        return (SimpleNamespace(cik=cik, company=SimpleNamespace(ticker=cik * 3, title=cik), last_updated=None,
                                change_pct_1y=raw), 10.0)

    db_rows = [stat("A", -97.5), stat("B", -20.0), stat("C", 200.0), stat("D", 50.0)]
    buy = rank_rows(db_rows, uni, "buyback", 1, 50)
    dil = rank_rows(db_rows, uni, "dilution", 1, 50)
    assert [r["ticker"] for r in buy] == ["BBB"] and buy[0]["change_pct"] == pytest.approx(-20.0)
    assert [r["ticker"] for r in dil] == ["DDD"] and dil[0]["change_pct"] == pytest.approx(50.0)
    # A's and C's raw moves were split artefacts and are gone from both lists.
    assert uni["A"].changes["change_pct_1y"] == pytest.approx(0.0)
    assert uni["C"].series.splits[0].ratio == 3.0


def test_alternating_share_classes_trailing_point_excluded():
    # Two concepts reported in alternate filings (e.g. all classes vs one class).
    res = adjust_series(series(("2025-06-30", 100_000_000), ("2025-07-31", 66_000_000),
                               ("2025-09-30", 99_000_000), ("2025-10-31", 65_500_000),
                               ("2025-12-31", 99_500_000), ("2026-01-31", 66_200_000)))
    assert [p.status for p in res.points][-1] == "excluded"
    assert compute_changes(res.points)["latest_shares"] == 99_500_000
