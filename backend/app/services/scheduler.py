import asyncio
import logging
from sqlalchemy import select
from .. import database, models
from . import sync
import time

logger = logging.getLogger(__name__)

# Flag to pause scheduler when user is actively making requests
_user_active = asyncio.Event()
_last_user_activity = 0

def mark_user_active():
    """Called when user makes a request - pauses background sync."""
    global _last_user_activity
    _last_user_activity = time.time()
    _user_active.set()

async def wait_if_user_active():
    """Pauses scheduler if user made request in last 10 seconds."""
    global _last_user_activity
    while time.time() - _last_user_activity < 10:
        logger.debug("User active, pausing background sync...")
        await asyncio.sleep(5)
    _user_active.clear()

async def run_daily_sync():
    """
    Infinite loop that runs the sync job every 24 hours.
    Waits for API to warm up before first sync.
    """
    logger.info("📅 Scheduler started. First sync in 5 minutes...")
    
    # Wait 5 minutes before first sync to allow API to warm up and user requests to complete
    await asyncio.sleep(300)
    
    while True:
        try:
            logger.info("🔄 Starting Daily Sync Job...")
            async with database.AsyncSessionLocal() as session:
                # Fetch all companies
                result = await session.execute(select(models.Company))
                companies = result.scalars().all()
                logger.info(f"Found {len(companies)} companies to sync.")
                
            for i, company in enumerate(companies):
                # Check if user is active - pause if so
                await wait_if_user_active()
                
                logger.info(f"[{i+1}/{len(companies)}] Syncing {company.ticker}...")
                
                try:
                    await sync.sync_company_data(company.ticker, company.cik)
                    # Offerings sync is 'slow' scraping
                    await sync.sync_company_offerings(company.ticker, company.cik)
                except Exception as e:
                    logger.error(f"Error syncing {company.ticker}: {e}")
                    
                # Longer delay between tickers to avoid SEC rate limits
                await asyncio.sleep(15)  # Increased from 5 to 15 seconds
                
            logger.info("✅ Daily Sync Job Completed. Sleeping for 24 hours.")
            
        except Exception as e:
            logger.error(f"❌ Scheduler crashed: {e}")
            
        # 24 hours = 86400 seconds
        await asyncio.sleep(86400)

def start_scheduler():
    # Run in background
    asyncio.create_task(run_daily_sync())
