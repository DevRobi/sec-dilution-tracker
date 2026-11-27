#!/usr/bin/env python3
"""Record a frozen API snapshot for the static demo build (NEXT_DEMO=1).

Drives the running frontend (default http://localhost:3200, which proxies
/api/* to the FastAPI backend) with Playwright, intercepts every /api/
response and writes its body to demo-snapshot/data/<file>.json, where <file>
is the same deterministic name the browser shim in src/lib/demo.ts computes
from path + query. Rerunnable: the output directory is rebuilt from scratch.

Never recorded: /auth/*, /portfolio/*, /users/* (accounts and personal
portfolios). The demo build fakes a read-only "demo user" instead.

Usage:
    python scripts/record_demo.py [--frontend URL] [--out DIR] [--tickers A,B,C]
                                  [--quick]   # skip the full rankings grid
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "demo-snapshot" / "data"

# SIDU has the full offerings/AI-extraction record; the rest are large
# buyback names and well-known diluters taken from the rankings.
DEFAULT_TICKERS = [
    "SIDU",
    # buybacks
    "AAPL", "ORLY", "NVR", "MGM", "SYF", "GM", "EBAY", "CHTR",
    # dilution
    "ASTS", "MSTR", "RIOT", "HOOD", "IONQ", "GOVX",
]

DATA_VERSION = "split-adj-1"  # matches DATA_VERSION in src/lib/api.ts
YEARS = [1, 2, 3, 4, 5]
CAPS = ["any", "50000000", "300000000", "1000000000", "2000000000",
        "10000000000", "50000000000", "200000000000"]
GRID_LIMIT = 50            # default "Rows" value; 10/20 are sliced from it by the shim
ANY_CAP_LIMITS = [100, 200]  # larger row counts recorded for the unfiltered view only

EXCLUDE = re.compile(r"^/(auth|portfolio|users?)(/|$)")


# ---- must stay in sync with canonicalKey()/fileNameFor() in src/lib/demo.ts ----
def canonical_key(path: str, query: str) -> str:
    if path.startswith("/api/"):
        path = path[4:]
    path = "/" + path.strip("/")
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "v"]
    params.sort()
    return path + ("?" + "&".join(f"{k}={v}" for k, v in params) if params else "")


def fnv1a32(s: str) -> str:
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def file_name_for(key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_")[:80]
    return f"{slug}-{fnv1a32(key)}.json"
# ---------------------------------------------------------------------------------


class Recorder:
    def __init__(self, out: Path):
        self.out = out
        self.index: dict[str, str] = {}
        self.pending: set[asyncio.Task] = set()
        self.skipped: list[str] = []
        self.failed: list[str] = []

    def on_response(self, response) -> None:
        parts = urlsplit(response.url)
        if not parts.path.startswith("/api/"):
            return
        api_path = parts.path[4:]
        if api_path == "/data-quality/report":  # saved separately as HTML
            return
        if EXCLUDE.match(api_path):
            self.skipped.append(api_path)
            return
        if response.request.method != "GET":
            self.skipped.append(f"{response.request.method} {api_path}")
            return
        task = asyncio.ensure_future(self._save(response, parts.path, parts.query))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _save(self, response, path: str, query: str) -> None:
        key = canonical_key(path, query)
        if response.status != 200:
            # Not saved: the demo shows these as "not in this snapshot", which
            # is also what the live app shows for a 404.
            self.failed.append(f"{response.status} {key}")
            return
        try:
            body = await response.body()
        except Exception as e:  # response evicted / navigation raced it
            self.failed.append(f"body-error {key}: {e}")
            return
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype:
            self.failed.append(f"non-json {key} ({ctype})")
            return
        name = file_name_for(key)
        (self.out / name).write_bytes(body)
        self.index[key] = name

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)


async def in_page_get(page, url: str) -> int:
    """GET through the page's own fetch so the response listener records it."""
    return await page.evaluate("u => fetch(u).then(r => r.status)", url)


async def settle(page, rec: Recorder, ms: int = 400) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)
    await rec.drain()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", default="http://localhost:3200")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--quick", action="store_true", help="skip the full rankings filter grid")
    args = ap.parse_args()

    base = args.frontend.rstrip("/")
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rec = Recorder(out)
    t0 = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})  # fresh: no token
        page = await ctx.new_page()
        page.set_default_timeout(120_000)
        page.on("response", rec.on_response)

        # 1. Landing + dashboard shell.
        await page.goto(f"{base}/")
        await settle(page, rec)
        await page.goto(f"{base}/dashboard")
        await settle(page, rec)

        # 2. Data-quality audit.
        await page.goto(f"{base}/dashboard/data-quality")
        await settle(page, rec)
        # The "Open HTML report" link is a plain anchor; store it as a static page.
        html = await page.evaluate("() => fetch('/api/data-quality/report').then(r => r.ok ? r.text() : '')")
        if html:
            (out / "data-quality-report.html").write_text(html, encoding="utf-8")

        # 3. Rankings page as a visitor sees it (default filters).
        await page.goto(f"{base}/dashboard/rankings")
        await settle(page, rec)

        # 4. Rankings filter grid: every period x market-cap range x SPAC toggle at
        #    the default row count, plus larger row counts for the unfiltered view.
        def rankings_url(kind: str, years: int, limit: int, lo: str, hi: str, spacs: bool) -> str:
            q = f"type={kind}&years={years}&limit={limit}"
            if lo != "any":
                q += f"&min_cap={lo}"
            if hi != "any":
                q += f"&max_cap={hi}"
            if spacs:
                q += "&include_spacs=true"
            return f"/api/rankings?{q}&v={DATA_VERSION}"

        combos = []
        for spacs in (False, True):
            for years in YEARS:
                for lim in ANY_CAP_LIMITS:
                    combos.append((years, lim, "any", "any", spacs))
                if args.quick:
                    combos.append((years, GRID_LIMIT, "any", "any", spacs))
                    continue
                for lo in CAPS:
                    for hi in CAPS:
                        # min >= max can never match; the demo shows "not in snapshot".
                        if lo != "any" and hi != "any" and int(lo) >= int(hi):
                            continue
                        combos.append((years, GRID_LIMIT, lo, hi, spacs))
        total = len(combos) * 2
        done = 0
        for years, lim, lo, hi, spacs in combos:
            for kind in ("buyback", "dilution"):
                url = rankings_url(kind, years, lim, lo, hi, spacs)
                if canonical_key(*urlsplit(url)[2:4]) in rec.index:
                    done += 1
                    continue
                status = await in_page_get(page, url)
                done += 1
                if status != 200:
                    print(f"  rankings {status}: {url}", flush=True)
            if done % 40 == 0:
                await rec.drain()
                print(f"  rankings {done}/{total}  ({time.time() - t0:.0f}s)", flush=True)
        await rec.drain()

        # 5. Ticker detail pages (each loads ticker, stats, history, price,
        #    market cap, offerings and its data-quality findings).
        titles = {}
        for t in tickers:
            await page.goto(f"{base}/dashboard/ticker/{t}")
            await settle(page, rec, 800)
            info_key = canonical_key(f"/api/tickers/{t}", "")
            if info_key in rec.index:
                titles[t] = json.loads((out / rec.index[info_key]).read_text()).get("title", "")
                print(f"  ticker {t}: ok", flush=True)
            else:
                print(f"  ticker {t}: NOT RECORDED (skipped from manifest)", flush=True)

        await rec.drain()
        await browser.close()

    manifest = {
        "asOf": dt.date.today().isoformat(),
        "recordedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "dataVersion": DATA_VERSION,
        "tickers": [{"ticker": t, "title": titles[t]} for t in tickers if t in titles],
        "files": dict(sorted(rec.index.items())),
    }
    (out / "_index.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    size = sum(f.stat().st_size for f in out.iterdir())
    print(f"\nRecorded {len(rec.index)} responses, {size / 1e6:.1f} MB, in {time.time() - t0:.0f}s -> {out}")
    if rec.skipped:
        print(f"Skipped (excluded or non-GET): {sorted(set(rec.skipped))}")
    if rec.failed:
        print(f"Not saved ({len(rec.failed)}): {rec.failed[:20]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
