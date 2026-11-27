# SEC Dilution Tracker

> **Live snapshot:** [https://devrobi.github.io/demo/dilution/](https://devrobi.github.io/demo/dilution/) (the real frontend on frozen data, no backend needed) · **All projects:** [https://devrobi.github.io](https://devrobi.github.io)

**Pitch:** I pull share counts and offering documents straight from SEC EDGAR, use an LLM to extract dilution (ATMs, shelves, warrants, direct offerings), and check the data before I trust a ranking.

## What it does
- Tracks shares outstanding over time for 3,400+ U.S. companies from 10-K/10-Q/20-F XBRL facts, with 1Y-10Y buyback/dilution rankings and market-cap filters.
- For a single ticker, parses 424B/S-3/8-K/6-K filings and lists active and potential dilution, each with a link to the source filing and the sentence the number came from.
- Runs a data-quality audit (`app/data_quality`) with 30 checks: duplicates, implausible share-count jumps, splits that were never adjusted, stale tickers, late feeds, shelf/ATM capacity that doesn't reconcile, and stats that don't match their own inputs.

## How I use it
I screen for steady buybacks and avoid serial diluters. Before buying a small cap I check its remaining shelf, ATM and warrant capacity, then the audit page, since a "−60% shares" ranking is usually a reverse split.

## Architecture
```
SEC EDGAR ─┬─ XBRL companyfacts ─────────────► share-count history ─┐
           └─ filing text (Docling → chunks) ─► rules + LLM extract ─┤
                                                                     ▼
                  validate (arithmetic, quote grounding, cap-table guards)
                  → reconcile (merge the same instrument across filings)
                  → SQLite/Postgres → audit job → FastAPI → Next.js
```

## Run it (read-only against the local snapshot)
```bash
cd backend
ENABLE_AI_EXTRACTION=false \
DATABASE_URL="sqlite+aiosqlite:///file:$PWD/tracker.db?mode=ro&uri=true" \
venv/bin/uvicorn app.main:app --port 8200

cd frontend && BACKEND_ORIGIN=http://127.0.0.1:8200 npx next dev -p 3200

cd backend && venv/bin/python -m app.data_quality audit    # HTML/CSV/JSON in backend/reports/data_quality
cd backend && venv/bin/python -m app.data_quality dedupe   # dry-run merge plan, never writes
backend/venv/bin/python -m pytest                          # from repo root: 45 tests
```

## Best screenshots
- http://localhost:3200/dashboard/data-quality (audit summary)
- http://localhost:3200/dashboard/ticker/SIDU (AI dilution analysis + share history)
- http://localhost:3200/dashboard/rankings
- http://localhost:8200/data-quality/report (static HTML report)
- http://localhost:8200/docs (API)

## Stack
Python 3.14, FastAPI, SQLAlchemy (async), SQLite/Postgres, pandas, Docling, Ollama/DeepSeek, pytest · Next.js 16, React 19, TypeScript, Tailwind, Recharts · Docker, Render, Vercel.
