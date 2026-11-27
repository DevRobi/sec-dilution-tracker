import asyncio
import logging
import sys
import os
from datetime import datetime

# Ensure we can import from the app directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import database, models
from app.services import sync
from sqlalchemy.future import select

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("PortfolioScanner")

async def run_portfolio_scan_daemon():
    """
    Continuous daemon scanner that:
    1. Watches for NEW stocks in user portfolios (Deep Scan).
    2. Re-scans EXISTING stocks every 24 hours (Standard Scan).
    3. Prevents re-scanning the same stock repeatedly in the same session.
    """
    print("\n" + "="*50)
    print("🚀 PORTFOLIO DAEMON SCANNER STARTED")
    print("   Watching for new portfolio additions and daily updates...")
    print("="*50 + "\n")

    # Tracking scan history
    # stored as {cik: datetime_of_last_scan}
    last_scan_times = {}

    while True:
        try:
            async with database.AsyncSessionLocal() as session:
                # Get all unique CIKs in all portfolios
                stmt = select(models.PortfolioItem.cik, models.Company.ticker, models.Company.title).join(
                    models.Company, models.Company.cik == models.PortfolioItem.cik
                ).distinct()
                
                result = await session.execute(stmt)
                current_items = result.all() # List of (cik, ticker, title)
            
            # 1. Identify NEW items (Never scanned in this session)
            # useful for fresh restarts, but we should also rely on DB or simple memory map
            new_items = [item for item in current_items if item[0] not in last_scan_times]
            
            # 2. Identify STALE items (Scanned > 24h ago)
            stale_items = []
            now = datetime.now()
            for item in current_items:
                cik = item[0]
                if cik in last_scan_times:
                    if (now - last_scan_times[cik]).total_seconds() > 86400: # 24 hours
                        stale_items.append(item)
            
            # Combine work 
            # Prioritize NEW items first
            work_queue = []
            
            if new_items:
                print(f"🆕 Found {len(new_items)} new tickers to scan.")
                # For new items, we do DEEP SCAN
                for item in new_items:
                    work_queue.append({'item': item, 'deep': True, 'reason': 'NEW'})
            
            if stale_items:
                print(f"⏰ Found {len(stale_items)} tickers due for daily re-scan.")
                # For daily updates, Standard Scan (30 filings) is enough to catch recent activity
                for item in stale_items:
                    work_queue.append({'item': item, 'deep': False, 'reason': 'DAILY'})

            # SORTING: Prioritize IMRX if present (User preference)
            work_queue.sort(key=lambda x: x['item'][1] != 'IMRX')

            if work_queue:
                for i, job in enumerate(work_queue):
                    cik, ticker, title = job['item']
                    is_deep = job['deep']
                    reason = job['reason']
                    
                    print(f"[{i+1}/{len(work_queue)}] 🔍 Scanning {ticker} ({reason})...")
                    
                    try:
                        # Scan
                        # force=True is needed to bypass the internal 24h cache check in sync.py
                        # We are managing the schedule ourselves here.
                        await sync.sync_company_offerings(ticker, cik, force=True, deep_scan=is_deep)
                        
                        # Summary update
                        await sync.generate_dilution_summary_from_db(ticker, cik, force=True)
                        
                        print(f"✅ Finished {ticker}\n")
                        
                        # Update timestamp
                        last_scan_times[cik] = datetime.now()
                        
                        # Sleep
                        await asyncio.sleep(5)
                        
                    except Exception as e:
                        print(f"❌ Error scanning {ticker}: {e}")
                        logger.error(f"Failed to scan {ticker}: {e}")
                        # Don't update timestamp so it retries next loop (or eventually)
            else:
                pass
                # No work needed right now

        except Exception as e:
            logger.error(f"Scanner loop error: {e}")

        # Sleep for 60 seconds before checking again
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(run_portfolio_scan_daemon())
