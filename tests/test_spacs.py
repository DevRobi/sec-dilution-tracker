"""SPAC detection: SIC 6770 first, name pattern only as a fallback; rankings exclude SPACs by default."""
import json
from types import SimpleNamespace

import pytest

from app.services import adjusted_stats, spacs


@pytest.fixture(autouse=True)
def sic_cache(tmp_path, monkeypatch):
    path = tmp_path / "sic.json"
    path.write_text(json.dumps({
        "1": {"sic": "6770", "desc": "Blank Checks", "name": "Plain Name Holdings"},
        "2": {"sic": "3562", "desc": "Ball & Roller Bearings", "name": "Bearings Acquisition Corp"},
    }))
    monkeypatch.setattr(spacs, "CACHE_PATH", path)
    spacs.reset_cache()
    yield path
    spacs.reset_cache()


def test_sic_6770_is_a_spac_even_with_an_ordinary_name():
    assert spacs.is_spac("0000000001", "Plain Name Holdings")


def test_sic_code_overrides_a_spac_looking_name():
    # An operating company whose name contains "Acquisition Corp" is not a SPAC.
    assert not spacs.is_spac("2", "Bearings Acquisition Corp")


def test_name_fallback_only_without_a_sic_code():
    assert spacs.is_spac("999", "Bayview Acquisition Corp")
    assert spacs.is_spac("998", "Newbridge Acquisition Ltd")
    assert not spacs.is_spac("997", "Middleby Corp")
    assert not spacs.is_spac("996", "Acquired Sales Corp")


def test_fetch_sic_caches_results_and_skips_known(sic_cache):
    calls = []

    def fake(cik):
        calls.append(cik)
        return {"sic": "6770", "sicDescription": "Blank Checks", "name": "X"}

    out = spacs.fetch_sic(["0000000001", "5", "6"], "Test test@example.com", fetcher=fake)
    assert calls == ["5", "6"]            # CIK 1 already cached
    assert out["fetched"] == 2
    assert json.loads(sic_cache.read_text())["5"]["sic"] == "6770"


def test_rankings_exclude_spacs_by_default():
    from app.routers.rankings import rank_rows

    rows = [("1", "2024-12-31", 20_000_000, "10-K"), ("1", "2025-12-31", 12_000_000, "10-K"),   # SPAC redemptions
            ("B", "2024-12-31", 50_000_000, "10-K"), ("B", "2025-12-31", 40_000_000, "10-K")]   # real buyback
    uni = adjusted_stats.build_universe(rows, {"1": "SPCA", "B": "BBB"})

    def stat(cik, title):
        return (SimpleNamespace(cik=cik, company=SimpleNamespace(ticker=title[:4].upper(), title=title),
                                last_updated=None, change_pct_1y=None), 10.0)

    db_rows = [stat("1", "Plain Name Holdings"), stat("B", "Bearing Maker Inc")]
    default = rank_rows(db_rows, uni, "buyback", 1, 50)
    assert [r["cik"] for r in default] == ["B"] and default[0]["spac"] is False
    included = rank_rows(db_rows, uni, "buyback", 1, 50, include_spacs=True)
    assert {r["cik"] for r in included} == {"1", "B"}
    assert next(r for r in included if r["cik"] == "1")["spac"] is True
