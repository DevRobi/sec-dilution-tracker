"""Identify SPACs (blank-check companies) so their trust redemptions don't pose as buybacks.

Primary signal: SEC Standard Industrial Classification code 6770 ("Blank Checks"), read from
https://data.sec.gov/submissions/CIK##########.json and cached on disk (cache/sic.json).
Fallback, only when no SIC code is cached: the company name ("... Acquisition Corp", "SPAC").

A SPAC's share count falls when holders redeem shares from the trust, and jumps at the
de-SPAC merger; neither is a buyback or ordinary dilution.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BLANK_CHECK_SIC = "6770"
CACHE_PATH = Path(os.getenv("SIC_CACHE_PATH", str(Path(__file__).resolve().parents[2] / "cache" / "sic.json")))

_NAME = re.compile(
    r"\b(acquisition|acquisitions)\s+(corp(oration)?|co|company|ltd|limited|inc|holdings|i{1,3}|iv|v|vi{0,3})\b"
    r"|\bspac\b|\bblank[- ]check\b",
    re.IGNORECASE,
)

_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None


def sic_for(cik: str) -> dict | None:
    return _load().get(str(cik).lstrip("0") or "0")


def looks_like_spac_name(title: str | None) -> bool:
    return bool(title and _NAME.search(title))


def is_spac(cik: str, title: str | None = None) -> bool:
    """True for SIC 6770; the name pattern decides only when the SIC code is unknown."""
    record = sic_for(cik)
    if record and record.get("sic"):
        return record["sic"] == BLANK_CHECK_SIC
    return looks_like_spac_name(title)


def fetch_sic(ciks, user_agent: str, rate_per_sec: float = 8.0, fetcher=None, save_every: int = 100) -> dict:
    """Fill the SIC cache for the given CIKs (skips cached ones). Returns counts."""
    import urllib.request

    cache = _load()
    pending = [str(c).lstrip("0") for c in ciks if str(c).lstrip("0") and str(c).lstrip("0") not in cache]

    def default_fetch(cik):
        req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                                     headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    fetch = fetcher or default_fetch
    done = failed = 0
    for i, cik in enumerate(pending, 1):
        try:
            data = fetch(cik)
            cache[cik] = {"sic": str(data.get("sic") or ""), "desc": data.get("sicDescription") or "",
                          "name": data.get("name") or ""}
            done += 1
        except Exception as exc:  # network/404: leave uncached so the name fallback applies
            failed += 1
            logger.warning("SIC fetch failed for CIK %s: %s", cik, exc)
        if i % save_every == 0:
            _save(cache)
        if fetcher is None:
            time.sleep(1.0 / rate_per_sec)
    _save(cache)
    return {"fetched": done, "failed": failed, "cached_total": len(cache)}


def _save(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True))
    tmp.replace(CACHE_PATH)
