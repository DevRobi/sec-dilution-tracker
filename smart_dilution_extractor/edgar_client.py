"""
Acquisition layer.

Resolves ticker -> CIK, pulls the EDGAR submissions index, filters to the
forms that actually carry dilution information, and downloads + caches the
primary documents. A simple token-bucket keeps us under SEC's rate limit.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests

from . import config
from .models import FilingRef


class _RateLimiter:
    """Minimal thread-safe spacing between requests."""

    def __init__(self, per_sec: int):
        self._min_interval = 1.0 / per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


_limiter = _RateLimiter(config.SEC_RATE_LIMIT_PER_SEC)


def _get(url: str) -> requests.Response:
    _limiter.wait()
    resp = requests.get(url, headers=config.SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


def resolve_cik(ticker: str) -> tuple[str, str]:
    """Return (10-digit CIK, company title) for a ticker symbol."""
    cache = config.CACHE_DIR / "company_tickers.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        data = _get(config.TICKERS_URL).json()
        cache.write_text(json.dumps(data))
    ticker = ticker.upper()
    for row in data.values():
        if row["ticker"].upper() == ticker:
            return str(row["cik_str"]).zfill(10), row["title"]
    raise ValueError(f"Ticker {ticker} not found in SEC company_tickers.json")


def list_dilution_filings(cik: str, limit: int | None = None) -> list[FilingRef]:
    """
    Return AI-approved dilution filings (most recent first).

    Periodic reports such as 10-K and 10-Q are deliberately excluded; the
    non-AI share-history service handles their outstanding-share XBRL facts.
    `limit` caps how many recent submissions to scan (None = the recent block).
    """
    url = config.SUBMISSIONS_URL.format(cik=cik)
    recent = _get(url).json()["filings"]["recent"]
    n = len(recent["form"])
    cik_int = int(cik)
    out: list[FilingRef] = []
    for i in range(n):
        form = recent["form"][i]
        if form not in config.DILUTION_FORMS:
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        if not doc:
            continue
        acc_nodash = acc.replace("-", "")
        url_doc = config.ARCHIVE_DOC_URL.format(
            cik_int=cik_int, acc_nodash=acc_nodash, doc=doc
        )
        out.append(
            FilingRef(
                accession=acc,
                form=form,
                filing_date=recent["filingDate"][i],
                primary_doc=doc,
                description=recent["primaryDocDescription"][i] or "",
                url=url_doc,
                cik=cik,
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def download_filing(ref: FilingRef) -> Path:
    """Download a filing's primary document to cache; return local path."""
    safe = f"{ref.cik}_{ref.accession.replace('-', '')}_{ref.primary_doc.replace('/', '_')}"
    dest = config.CACHE_DIR / safe
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    resp = _get(ref.url)
    dest.write_bytes(resp.content)
    return dest
