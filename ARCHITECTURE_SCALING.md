# Dilution Terminal — Scaling Architecture

> Target: run reliably for **thousands of concurrent users** instead of the current
> single-machine, single-process, 2-tester setup.
>
> This document is the system-architecture plan: what the app is today, where it
> breaks under load, and the concrete changes (database, services, frontend,
> infra, security, observability) to get there. Organized as an honest **as-is**
> assessment followed by a **target architecture** and a **phased roadmap** so the
> work can ship incrementally instead of as a big-bang rewrite.

---

## 1. Current architecture (as-is)

```
                ┌─────────────────────────────────────────┐
   Browser ───► │  Next.js (dev server, port 3000)         │
   (JWT in      │  - rewrites /api/* -> localhost:8000     │
    localStorage)└───────────────┬─────────────────────────┘
                                 │
                ┌────────────────▼─────────────────────────┐
                │  FastAPI (single uvicorn process, :8000)  │
                │  - JWT auth (python-jose, bcrypt)         │
                │  - in-process BackgroundTasks for sync    │
                │  - in-RAM ticker cache (company_tickers)  │
                │  - SQLAlchemy async engine                │
                └───┬──────────────┬───────────────┬────────┘
                    │              │               │
            ┌───────▼──┐   ┌───────▼───────┐  ┌────▼─────────────┐
            │ SQLite / │   │ Ollama (local │  │ External APIs:   │
            │ Postgres │   │ LLM, same box)│  │ SEC EDGAR,       │
            │ (1 file/ │   │ dilution      │  │ Polygon,         │
            │  1 node) │   │ extraction    │  │ yfinance/Finnhub │
            └──────────┘   └───────────────┘  └──────────────────┘

   Side processes (manual): market_sync.py (bulk), portfolio_scanner.py (daemon),
   scheduler.py (daily, currently disabled).
```

**Key properties today**
- One uvicorn process; no horizontal scaling, no load balancer.
- Background work (`sync_company_offerings`, etc.) runs **inside the API process** via FastAPI `BackgroundTasks` — competes with request handling for CPU/event-loop time.
- LLM extraction (Ollama) runs **on the same machine** as the API — CPU/GPU-bound and bursty.
- Data store is SQLite (or a single local Postgres) — fine for a few users, a hard ceiling for thousands.
- Ticker universe cached **in process RAM** (lost on restart, not shared across processes).
- Auth token stored in **`localStorage`** (XSS-exfiltratable) and verified per-request.
- Secrets live in plaintext `.env`; no rotation, no secret manager.
- Ingestion (`market_sync.py`) is a manual script with naive concurrency (10) and **no awareness of Polygon rate-limit tiers** — fails/throttles at scale.

---

## 2. Why it breaks at thousands of users

| Bottleneck | Symptom under load | Root cause |
|---|---|---|
| **SQLite / single DB node** | Write lock contention, read latency, no failover | Single-writer DB, no replicas, no connection pooler |
| **Background tasks in API process** | API latency spikes during syncs; one bad filing parse stalls requests | Work not isolated from request path |
| **LLM on API box** | CPU saturation, request timeouts when extraction runs | No separate inference tier, no queue |
| **In-RAM ticker cache** | Cold start latency; inconsistent across processes | Not a shared cache |
| **Single process / no LB** | No redundancy; deploy = downtime; can't use >1 core well | No orchestration / autoscaling |
| **External API rate limits** | Bulk sync throttled or banned (SEC 10 req/s, Polygon tier limits) | No centralized rate limiter / token bucket |
| **JWT in localStorage** | XSS = account takeover | Token reachable from JS |
| **No caching layer** | Same `/rankings`, `/tickers/{x}` recomputed per request | No Redis/CDN read-through cache |
| **No observability** | Can't tell why it's slow or what failed | No metrics/traces/structured logs |

---

## 3. Target architecture (to-be)

```
                          ┌──────────────┐
                 Users ──►│   CDN (edge)  │  static assets, cached GET responses
                          └──────┬───────┘
                                 │
                          ┌──────▼────────────┐
                          │  Load balancer /   │  TLS, WAF, rate limiting
                          │  API gateway       │
                          └──────┬─────────────┘
                 ┌───────────────┼───────────────────┐
                 │                                    │
        ┌────────▼─────────┐               ┌──────────▼──────────┐
        │ Next.js (SSR)    │  N replicas   │ FastAPI API tier    │  N replicas
        │ stateless        │               │ stateless, read-path│
        └──────────────────┘               └───┬─────────┬───────┘
                                               │         │
                          ┌────────────────────▼──┐   ┌──▼───────────────┐
                          │ Redis (cache + rate    │   │ Message queue    │
                          │ limit + session/refresh│   │ (SQS/RabbitMQ/   │
                          │ token store)           │   │  Redis Streams)  │
                          └───────────┬────────────┘   └──┬───────────────┘
                                      │                   │
              ┌───────────────────────▼───┐    ┌──────────▼─────────────────┐
              │ Postgres (primary)         │    │ Worker tier (Celery/Arq)   │
              │ + read replicas            │◄───┤ - ingestion (SEC/Polygon)  │
              │ + PgBouncer pooler         │    │ - stats recompute          │
              │ (TimescaleDB for series)   │    │ - calls LLM service        │
              └────────────────────────────┘   └──────────┬─────────────────┘
                                                          │
                                              ┌────────────▼──────────────┐
                                              │ LLM inference service      │
                                              │ (separate GPU pool / vLLM  │
                                              │  or hosted API w/ fallback)│
                                              └────────────────────────────┘

   Object storage (S3): raw filings, extraction artifacts, backups.
   Secrets manager: API keys, JWT keys (rotated).
   Observability: Prometheus/Grafana, OpenTelemetry traces, centralized logs, Sentry.
```

The guiding principle: **separate the read path (serve users fast) from the write path (ingest & enrich data slowly).** Users almost never need live data — they need recently-computed data served instantly. So make the API a thin cache-backed read layer, and push all the slow SEC/Polygon/LLM work into an asynchronous pipeline.

---

## 4. Database

**Move off SQLite immediately. Standardize on PostgreSQL.** It's already the default `DATABASE_URL` in the code, so the ORM is compatible.

- **Primary + read replicas.** Rankings, ticker detail, and search are read-heavy. Point the read endpoints at replicas, writes (sync) at the primary.
- **Connection pooler (PgBouncer).** The async engine already uses `pool_size=20, max_overflow=10`; per-process pools don't scale across many API replicas. PgBouncer in transaction mode lets hundreds of API workers share a bounded set of real Postgres connections.
- **Time-series tables (`History`, `Price`) → TimescaleDB hypertables** (Postgres extension). These are append-only, date-indexed series — exactly Timescale's sweet spot. Gives automatic partitioning by time, compression of old chunks, and fast range queries. Alternatively native Postgres declarative partitioning by year.
- **Indexing pass.** Ensure indexes back the actual query patterns:
  - `Stats(change_pct_1y)`, `Stats(change_pct_Ny)` partial indexes for the ranking sorts.
  - `History(cik, date)`, `Price(cik, date)` composite.
  - `Company(ticker)` unique, `Offering(cik, date)`.
- **Materialized views / precomputed ranking tables.** `/rankings` currently sorts + joins live per request. At scale, precompute the ranked lists per (period × market-cap bucket) on a schedule and serve them from a flat table or Redis. Refresh after each ingestion cycle, not per request.
- **Migrations: adopt Alembic.** The app currently does `create_all()` on startup ("Could not create tables" path). That's not safe for production schema evolution. Alembic gives versioned, reviewable migrations and zero-downtime column adds.
- **Backups + PITR.** Automated daily snapshots + WAL archiving to object storage. Test restores.

---

## 5. Service decomposition

Don't over-split into microservices on day one. Start with a **modular monolith split into three deployables** along the read/write seam:

1. **API service (stateless, scales horizontally).**
   - Serves all user-facing reads: `/rankings`, `/tickers/*`, `/portfolio/*`, `/auth/*`, `/tickers/search`.
   - Holds **no** background work and **no** in-process LLM calls.
   - Read-through cache (Redis) in front of expensive queries.
   - Run with multiple uvicorn/gunicorn workers per pod, multiple pods behind the LB.

2. **Worker service (the write/ingestion path).**
   - Consumes jobs from the queue: `sync_company_stats`, `sync_company_offerings`, daily refresh, bulk universe sync.
   - This is where `market_sync.py`, `portfolio_scanner.py`, and `scheduler.py` logic belongs — as **queued, idempotent, retryable jobs** instead of ad-hoc scripts and in-API background tasks.
   - Use **Celery** or **Arq** (Arq is asyncio-native, pairs well with the existing async code) with Redis/RabbitMQ as broker.
   - Scheduling via Celery Beat / cron-style triggers for the daily refresh.

3. **LLM inference service (the dilution extractor).**
   - Pull Ollama off the API box. Either:
     - a dedicated **GPU worker pool** running vLLM/Ollama behind an internal HTTP queue, or
     - a **hosted LLM API** (with a local fallback) if data-sensitivity allows.
   - Workers call this service; it never sits in the request path.
   - Batch extraction requests; cap concurrency to protect the GPU.

**Shared core library.** Keep `app.models`, `app.services` as an importable package used by both API and worker so logic isn't duplicated.

---

## 6. Data ingestion pipeline (the hard part at scale)

Syncing the whole US universe (~5,000+ tickers) against rate-limited external APIs is the real scaling challenge. Design it as a proper pipeline:

- **Centralized rate limiting / token bucket per provider.** SEC EDGAR allows ~10 req/s with a declared User-Agent; Polygon limits depend on tier (free = 5 req/min). Put a shared limiter in Redis so *all* workers collectively respect the cap — not per-process semaphores (the current `Semaphore(10)` in `market_sync.py` is per-process and provider-blind).
- **Tiered freshness, not "sync everything always."**
  - **Hot tier** (in any user portfolio, or recently viewed): refresh daily.
  - **Warm tier** (rest of liquid universe): refresh weekly.
  - **Cold tier** (illiquid/micro/nano): refresh monthly or on-demand.
  This slashes API call volume and respects rate limits. `should_skip_sync`'s 24h check is the seed of this — generalize it into tiers.
- **Idempotent, retryable jobs with backoff.** Each ticker sync is a job; on failure, exponential backoff + dead-letter queue for inspection. No more "best effort, mostly fails."
- **Incremental ingestion.** Only fetch filings newer than the last seen accession number per CIK, rather than re-scanning the last N filings every time.
- **Decouple fetch from parse.** Stage 1: fetch raw filing → object storage (S3). Stage 2: parse/extract from the stored artifact. Lets you re-run extraction (e.g., after improving the LLM prompt) without re-hitting SEC.
- **On-demand sync path for cache misses.** When a user searches an un-synced ticker, enqueue a high-priority job and stream status to the UI (the frontend already polls for offerings — formalize this with job IDs + a status endpoint, ideally WebSocket/SSE instead of fixed-interval polling).

---

## 7. Caching

- **Redis as the central cache + rate-limit store + refresh-token store.**
  - Read-through cache for `/rankings` (per period+bucket), `/tickers/{symbol}` detail, `/tickers/search` prefixes. TTLs aligned with ingestion cadence (e.g., rankings cached 1–6h).
  - Replace the **in-RAM ticker universe cache** with Redis so it's shared across API replicas and survives restarts.
- **CDN at the edge** for static frontend assets and cacheable GET API responses (rankings/search are public, cache-friendly). Cuts origin load dramatically.
- **HTTP cache headers** (`Cache-Control`, `ETag`) on public read endpoints so the CDN and browsers cooperate.

---

## 8. Frontend

- **Deploy Next.js properly** — `next build` + `next start` (or Vercel/static export), not the dev server (`npm run dev`) currently used. Dev mode is slow and not production-safe.
- **Stateless SSR replicas** behind the LB; or host on Vercel/Cloudflare Pages and point `/api/*` at the API tier.
- **Server-side rendering / streaming** for the rankings and ticker pages improves first paint and SEO (these are public, indexable pages).
- **Use the rewrite/proxy in prod too**, or split to a real API subdomain (`api.dilutionterminal.com`) with CORS — pick one and make auth cookies match the domain strategy (see security).
- **Move polling to push.** The ticker/search pages poll every 2–5s for offerings/sync status. Replace with **SSE or WebSockets** driven by job completion events — fewer wasted requests, snappier UX.
- **Client data layer.** Adopt React Query/SWR for caching, dedup, and background refetch instead of bespoke `requestVersionRef` race-guarding scattered across pages.

---

## 9. Auth & security

- **Stop storing JWT in `localStorage`.** Use **httpOnly, Secure, SameSite cookies** for the session/access token so XSS can't exfiltrate it.
- **Short-lived access token + refresh token** (refresh stored server-side in Redis, revocable). Current token TTL is 10080 min (7 days) — too long for a bearer token in localStorage.
- **Secrets manager** (AWS Secrets Manager / Vault / Doppler). Pull all keys (Polygon, Finnhub, JWT signing key, SMTP) out of plaintext `.env`. **Rotate the currently-exposed keys** — they've been in plaintext and must be considered compromised.
- **Rotate JWT signing key** with key-ID support so you can roll keys without invalidating all sessions at once.
- **WAF + rate limiting at the gateway** (per-IP, per-user) to stop scraping/abuse — rankings + search are attractive scrape targets.
- **Input validation & query hardening.** Replace the deprecated FastAPI `regex=` with `pattern=` (already noted in `rankings.py`); keep all query params constrained.
- **Least-privilege DB users**: API tier gets read-mostly creds; workers get write creds.
- **Network isolation**: DB, Redis, LLM service on a private network; only the LB is public.

---

## 10. Observability & reliability

- **Metrics**: Prometheus + Grafana — request latency/throughput per endpoint, DB pool saturation, queue depth, job success/failure rates, external-API error rates, LLM latency.
- **Tracing**: OpenTelemetry across API → worker → DB/LLM to find latency sources.
- **Structured logging** shipped to a central store (Loki/ELK/CloudWatch). Replace ad-hoc `print`/`logging` with structured JSON logs carrying request IDs.
- **Error tracking**: Sentry for both frontend and backend.
- **Health checks & readiness probes** for the orchestrator; `/health` distinguishing liveness vs. DB-ready.
- **SLOs + alerting**: e.g., p95 read latency < 300ms, ingestion freshness < 24h for hot tier; page on breach.
- **Graceful degradation**: if LLM is down, still serve stats/history/prices and mark offerings "pending."

---

## 11. Infrastructure & deployment

- **Containerize everything** (API, worker, LLM, frontend). A `docker-compose.yml` already exists (db + backend) — extend it for local dev parity, but production runs on an orchestrator.
- **Orchestration**: Kubernetes (or a managed PaaS like ECS/Fly.io/Render for less ops). Gives rolling deploys, autoscaling, self-healing.
- **Autoscaling**: HPA on the API tier by CPU/req latency; scale workers by **queue depth**; LLM pool scaled by GPU utilization/queue.
- **Zero-downtime deploys**: rolling updates + readiness gates + Alembic migrations run as a pre-deploy job.
- **CI/CD**: lint + typecheck (tsc is already clean) + tests + build images + deploy on merge. Staging environment that mirrors prod.
- **IaC**: Terraform for reproducible infra.
- **Object storage (S3)** for raw filings, extraction artifacts, and DB backups.

---

## 12. External-data & cost considerations

- **Polygon tier**: the single biggest ingestion constraint. Free tier (5 req/min) cannot sync the universe — budget for a paid tier sized to your refresh cadence, or pre-license a bulk dataset.
- **SEC EDGAR** is free but rate-limited and requires a declared User-Agent (already set). Respect it via the central limiter or risk an IP ban.
- **LLM cost**: self-hosted GPU (fixed cost, data stays local) vs. hosted API (variable cost, simpler ops). Extraction is the most expensive compute — batch aggressively and cache results in object storage so prompt improvements don't force full re-spend.
- **Egress + storage**: filings and price history accumulate; lifecycle-policy cold data to cheaper storage tiers.

---

## 13. Cost-optimized stack — the cheapest scalable path

> Goal: the **simplest, cheapest** setup that still scales to thousands of users.
> The trick is to pick **managed, serverless, scale-to-zero** services with generous
> free tiers, run a **single small VPS** for the always-on parts, and *not* pay for
> Kubernetes / GPU pools / read replicas until traffic actually forces it.
>
> **Reality check:** at thousands of users, your infra (servers, DB, cache) is
> cheap — likely **$20–60/mo**. The real money is **market data (Polygon)** and
> **LLM tokens**. Optimize those two and everything else is rounding error.

### Recommended cheap-but-scalable stack

| Layer | Pick | Free tier | Paid (when you grow) | Why this one |
|---|---|---|---|---|
| **Frontend hosting** | Cloudflare Pages (or Vercel) | Unlimited static + generous functions | ~$0–20/mo | Free CDN built in; scales globally at $0 |
| **API + worker** | One small VPS (Hetzner CX22 / Fly.io / Railway) | Fly/Railway have small free credits | €4–6/mo (Hetzner) | Cheapest reliable always-on compute; run API + arq worker on the same box at first |
| **Database** | **Neon** serverless Postgres (or Supabase) | 0.5 GB, scales to zero | ~$19/mo | Serverless Postgres, scales-to-zero, instant read replicas + branching when needed — no DB ops |
| **Cache + rate limit + queue** | **Upstash** (serverless Redis) | 10k cmd/day free | pay-per-request, ~$0–10/mo | Serverless Redis; doubles as the arq job broker and the central rate limiter |
| **Object storage** | **Cloudflare R2** | 10 GB free | ~$0.015/GB, **$0 egress** | No egress fees (vs S3) — big saver for storing raw filings/backups |
| **LLM inference** | Hosted cheap model (Groq / Gemini Flash / Claude Haiku / gpt-4o-mini) | small free credits | pay-per-token | Scales to zero, no GPU to rent; **cache extractions in R2** so you never re-spend |
| **Secrets** | Platform env vars / Doppler | free | free | No need for Vault at this size |
| **Observability** | Sentry + Grafana Cloud / Better Stack | free tiers | $0 until high volume | Errors + metrics + logs for free at small scale |

*(Pricing is approximate and changes — verify current rates. Hetzner + Neon + Upstash + Cloudflare is a well-trodden cheap-scalable combo.)*

### Estimated monthly cost by stage

| Stage | Users | Infra cost | Data + LLM cost | Notes |
|---|---|---|---|---|
| **MVP / testers** | 2–50 | **~$0–6** | ~$0 (Polygon free, Ollama local or free LLM credits) | Everything on free tiers + one tiny VPS |
| **Early growth** | 100s | **~$25–50** | **Polygon paid tier + LLM tokens dominate** | Neon paid, Upstash light usage, hosted LLM for extraction |
| **Scale** | 1,000s | **~$50–150 infra** | **Polygon + LLM are the real bill** | Add Neon read replica, bump VPS, split worker to its own box |

### The two real cost drivers (optimize these, ignore the rest)

1. **Polygon market data.** Free tier (5 req/min) can't sync the universe. A paid tier is the single biggest line item. Mitigate by:
   - **Tiered freshness** (hot daily / warm weekly / cold monthly) — slashes call volume.
   - Caching everything; never re-fetch unchanged filings (incremental by accession number).
   - Consider a **one-time bulk dataset** purchase vs. continuous API polling if cheaper at your volume.
2. **LLM extraction tokens.** Mitigate by:
   - Use a **cheap fast model** (Groq/Gemini Flash/Haiku) — extraction is structured, doesn't need a frontier model.
   - **Cache extraction results in R2** keyed by filing — improving the prompt later doesn't force a full re-spend.
   - Only run extraction on **dilution-relevant chunks** (the code already pre-filters) — don't feed whole filings.
   - Batch requests; cap concurrency.

### What to deliberately NOT pay for early (anti-over-engineering)

- ❌ **Kubernetes** — use a VPS or a PaaS (Fly/Railway/Render) until you genuinely need orchestration. K8s is an ops tax you don't need at hundreds of users.
- ❌ **GPU pool for the LLM** — a rented GPU is $200–1000+/mo idle. A pay-per-token hosted API scales to zero and is cheaper until very high volume.
- ❌ **Read replicas / PgBouncer** — Neon gives these on demand; add only when read latency proves it's needed.
- ❌ **Multi-region, dedicated message broker (RabbitMQ/Kafka)** — Upstash Redis + arq covers the queue for free-ish; revisit only at high throughput.
- ❌ **Separate microservices** — keep the modular monolith (API + worker in one codebase, maybe one box) and split only along the read/write seam when load demands.

**Bottom line:** Cloudflare Pages (frontend) + one Hetzner VPS (API + worker) + Neon (Postgres) + Upstash (Redis/queue) + Cloudflare R2 (storage) + a hosted cheap LLM = a stack that costs **near-zero at MVP**, scales to thousands of users for **tens of dollars of infra**, and where your only meaningful bill is the market data and LLM tokens you choose to consume.

---

## 14. Phased roadmap (incremental, not big-bang)

**Phase 0 — Stop the bleeding (days)**
- Migrate SQLite → Postgres; add Alembic; add the missing indexes.
- Pull background sync out of the API process into a simple worker (even a separate `arq`/Celery process on the same box).
- Move secrets to a secret manager and **rotate the exposed keys**.
- Run Next.js as a production build, not the dev server.

**Phase 1 — Make reads fast & safe (1–2 weeks)**
- Add Redis: cache rankings/ticker/search; move ticker universe cache to Redis.
- Precompute ranking tables/materialized views.
- Switch JWT to httpOnly cookies + short access token + refresh tokens.
- Add structured logging, Sentry, basic metrics.

**Phase 2 — Scale out (2–4 weeks)**
- Containerize; deploy API + worker + frontend on an orchestrator behind an LB.
- PgBouncer + read replicas; TimescaleDB for series tables.
- Centralized rate limiter; tiered freshness ingestion; idempotent retryable jobs + DLQ.
- Separate the LLM inference tier.

**Phase 3 — Harden & optimize (ongoing)**
- CDN + HTTP caching; SSR streaming; React Query on the frontend.
- Autoscaling by queue depth / latency; SLOs + alerting; OpenTelemetry tracing.
- WAF, per-user rate limits, network isolation, least-privilege DB roles.
- Incremental filing ingestion + fetch/parse decoupling via S3.

---

## 15. Summary of the key moves

1. **Postgres + replicas + PgBouncer + Timescale** — kill the SQLite ceiling.
2. **Split read API from write workers** — isolate slow ingestion/LLM from user requests.
3. **Queue-driven, rate-limited, tiered ingestion** — sync the universe without getting throttled or banned.
4. **Redis caching + precomputed rankings + CDN** — serve thousands of reads cheaply.
5. **Separate LLM inference tier** — get GPU-bound extraction off the request path.
6. **httpOnly-cookie auth + secret manager + key rotation** — close the obvious security holes.
7. **Containers + orchestration + autoscaling + observability** — operate it like a real service.

The throughline: users get **instantly-served, recently-computed data**; the expensive SEC/Polygon/LLM work happens **asynchronously, rate-limited, and tiered** in the background.
