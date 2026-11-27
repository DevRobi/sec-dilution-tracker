# Deploy — Vercel (frontend) + Render (backend + Postgres), NON-AI build

This ships the full app **except** AI dilution extraction/summaries (Active /
Potential dilution, Dilution History by Instrument). Everything else — auth,
portfolio, share-count history, prices, rankings, alerts — works.

The code is already prepared:
- `ENABLE_AI_EXTRACTION=false` makes the backend skip the Ollama/Docling pipeline
  (adding a stock still fetches share-count history + prices).
- `backend/Dockerfile` binds to the platform's `$PORT`.
- `database.py` normalizes any `postgres://` / Neon `?sslmode=require` URL to the
  async `asyncpg` driver automatically.
- `render.yaml` is a one-click blueprint for the backend + DB.

---

## Prerequisites
- The repo on GitHub (private is fine). Render/Vercel pull from it.
- Secrets ready: `SEC_USER_AGENT` (a real contact email), `POLYGON_API_KEY`,
  `FINNHUB_API_KEY`, and optionally SMTP creds for email alerts.
- **Rotate the previously-exposed secrets before going public.**

---

## Part A — Backend + DB on Render

### Option 1 (recommended): one-click via the blueprint
1. Push this repo to GitHub.
2. Render dashboard → **New +** → **Blueprint** → select the repo.
3. Render reads `render.yaml`, creates **dilution-backend** (web) + **dilution-db**
   (Postgres), and prompts for the `sync: false` secrets. Fill them in.
4. Click **Apply**. First build takes a few minutes (Docker image).
5. When live, note the URL: `https://dilution-backend.onrender.com`.
6. Health check: open `https://dilution-backend.onrender.com/` → `{"status":"ok"}`.

### Option 2: manual web service
1. Render → **New +** → **Web Service** → connect repo.
2. Runtime **Docker**, Dockerfile path `./backend/Dockerfile`, context `./backend`.
3. Add env vars (Environment tab):
   - `ENABLE_AI_EXTRACTION=false`
   - `JWT_SECRET_KEY=<long random string>`
   - `ACCESS_TOKEN_EXPIRE_MINUTES=10080`
   - `SEC_USER_AGENT`, `POLYGON_API_KEY`, `FINNHUB_API_KEY` (+ SMTP\_\* if using alerts)
   - `DATABASE_URL` — see DB step below.
4. Create a DB: **New +** → **PostgreSQL** (or use Neon, below). Copy its
   connection string into `DATABASE_URL`. (No `+asyncpg` needed — the app fixes it.)
5. Deploy.

### Using Neon instead of Render Postgres (portable, stays free)
1. neon.tech → create project → copy the connection string
   (`postgresql://...?sslmode=require`).
2. Paste it as `DATABASE_URL` on the Render service. That's it — SSL + driver
   are handled automatically.

> Tables auto-create on first boot (`Base.metadata.create_all`), so there's no
> migration step. The DB starts empty.

---

## Part B — Frontend on Vercel
1. vercel.com → **Add New** → **Project** → import the repo.
2. **Root Directory:** `frontend` (important — it's a monorepo).
3. Framework preset: **Next.js** (auto-detected). Leave build/output defaults.
4. **Environment Variables** → add:
   - `BACKEND_ORIGIN = https://dilution-backend.onrender.com`
     (your Render URL, no trailing slash). The Next rewrite proxies `/api/*` to it.
5. **Deploy.** Vercel gives you `https://<project>.vercel.app`.

---

## Part C — Verify
1. Open the Vercel URL → register a user → log in.
2. Add a ticker (e.g. `AAPL`). It should appear with share-count history + price.
   - Active/Potential dilution panels stay empty by design (AI off). ✅ expected.
3. Check rankings + alerts load.
4. Backend logs (Render → Logs): you'll see
   `AI extraction disabled (ENABLE_AI_EXTRACTION); skipping ...` on add — correct.

---

## Costs (low traffic / a few testers)
- Vercel Hobby: **$0**
- Render backend: **$0** (free, sleeps ~15 min idle, ~50s cold start) or **~$7/mo** (Starter, always-on)
- Postgres: Render free (30-day) or **Neon free** (stays free)
→ **$0–7/mo.**

## Notes / gotchas
- **Free Render backend sleeps.** The hourly cleanup micro-service only runs while
  awake (it sweeps on wake-up, so no data integrity issue). Use Starter if you
  want it always-on.
- **Deploying without pushing to GitHub:** you can use the Render CLI / Vercel CLI
  (`vercel --cwd frontend`) to deploy from local instead.
- **CORS** is already `*` on the backend, so the Vercel origin works out of the box.
- **Turning AI back on later** (e.g. a separate GPU worker): set
  `ENABLE_AI_EXTRACTION=true` only where the `smart_dilution_extractor` package +
  a model backend are actually available.
