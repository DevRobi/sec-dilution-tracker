import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import engine, Base, READ_ONLY
from .routers import tickers, auth, portfolio, rankings, admin, data_quality
from .services import adjusted_stats, scheduler, securities, cleanup
from .services.sync_universe import seed_company_universe

# --- Logging ---------------------------------------------------------------
# Without this, app loggers (e.g. logging.getLogger("app.services.dilution_engine"))
# propagate to the unconfigured root logger, which defaults to WARNING, so all
# our INFO progress lines — including the dilution-extraction START/FAST/FULL
# logs — are silently dropped. Configure a console handler at INFO for the
# "app" namespace only, leaving noisy third-party libs (docling, httpx, …) at
# WARNING. These lines appear in the SAME terminal as uvicorn's access logs.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("app").setLevel(logging.INFO)

app = FastAPI(title="Share Buyback Tracker API", version="2.0")

# CORS Setup - Allow all origins with preflight caching
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # Must be False when using allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,  # Cache preflight for 24 hours - prevents OPTIONS spam
)

app.include_router(auth.router)
app.include_router(portfolio.router)
app.include_router(tickers.router)
app.include_router(rankings.router)
app.include_router(admin.router)
app.include_router(data_quality.router)

import asyncio

@app.on_event("startup")
async def startup():
    import time
    start = time.time()
    print("=" * 50)
    print("🚀 Starting Share Buyback Tracker API...")
    print("=" * 50)
    
    if READ_ONLY:
        print("🔒 READ-ONLY database mode: skipping create_all, seeding and cleanup.")

    # Only for dev: Create tables if they don't exist
    # Retry logic in case DB is starting up
    retries = 0 if READ_ONLY else 5
    for i in range(retries):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            print("✅ Database tables created successfully.")
            break
        except Exception as e:
            print(f"⏳ Database connection failed ({i+1}/{retries}): {e}")
            if i < retries - 1:
                await asyncio.sleep(5)
            else:
                print("❌ Could not create tables. App might fail.")
    
    # Warm up ticker cache BEFORE starting scheduler
    print("📥 Warming up ticker cache...")
    cache_start = time.time()
    tickers = await securities.get_tickers()
    cache_time = time.time() - cache_start
    if tickers:
        print(f"✅ Ticker cache ready: {len(tickers)} companies ({cache_time:.2f}s)")
        seed_start = time.time()
        inserted = 0 if READ_ONLY else await seed_company_universe(tickers)
        if inserted:
            print(f"✅ Seeded searchable ticker universe: {inserted} companies ({time.time() - seed_start:.2f}s)")
    else:
        print("⚠️ Ticker cache failed to load - searches will be slow!")
    
    # ✅ Start Scheduler (Disabled temporarily for priority sync)
    # await scheduler.start_scheduler()
    print("✅ Background scheduler paused for priority sync.")

    # 🧹 Start the alert-state cleanup micro-service. It only reconciles per-user
    # alert rows; shared AI extraction output stays available for search/ticker
    # pages even when no portfolio currently tracks that ticker.
    if not READ_ONLY:
        cleanup.start_cleanup_service()
        print("🧹 Dilution alert cleanup service started.")
    
    # Build the split-adjusted share-count universe (rankings/portfolio) in the
    # background; the first rankings request waits for it if it isn't ready.
    asyncio.create_task(adjusted_stats.get_universe())
    print("Building split-adjusted share-count universe in the background.")

    total_time = time.time() - start
    print("=" * 50)
    print(f"🎉 API Ready! Startup took {total_time:.2f}s")
    print("=" * 50)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Share Buyback Tracker API is running"}
