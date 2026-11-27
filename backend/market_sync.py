import asyncio
import os
import sys
import httpx
import logging
from sqlalchemy import select
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load env vars first
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Ensure we can import app modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import database, models
from app.services import sync, securities

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('market_sync.log')
    ]
)
logger = logging.getLogger(__name__)

async def get_all_us_stocks():
    """Fetch all active US Common Stocks (CS) from Polygon."""
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        logger.error("POLYGON_API_KEY is missing!")
        return []

    url = "https://api.polygon.io/v3/reference/tickers"
    params = {
        "market": "stocks",
        "type": "CS",  # Common Stock only
        "active": "true",
        "limit": 1000,
        "apiKey": api_key
    }
    
    all_tickers = []
    
    async with httpx.AsyncClient() as client:
        while url:
            try:
                logger.info(f"Fetching tickers from {url}...")
                response = await client.get(url, params=params if url == "https://api.polygon.io/v3/reference/tickers" else None)
                if response.status_code != 200:
                    logger.error(f"Failed to fetch tickers: {response.status_code} - {response.text}")
                    break
                
                data = response.json()
                results = data.get('results', [])
                
                for r in results:
                    if r.get('currency_name') == 'usd': # Double check USD
                         all_tickers.append(r['ticker'])
                
                # Check for next page
                url = data.get('next_url')
                if url:
                    url += f"&apiKey={api_key}"
                else:
                    break
            except Exception as e:
                logger.error(f"Error fetching tickers: {e}")
                break
                
    logger.info(f"Found {len(all_tickers)} active US common stocks.")
    return all_tickers

async def should_skip_sync(ticker: str):
    """Check if we already have fresh stats for this ticker."""
    async with database.AsyncSessionLocal() as session:
        # Find CIK first (need join or look up)
        # Actually, simpler: Look up company by ticker
        stmt = select(models.Company).where(models.Company.ticker == ticker)
        res = await session.execute(stmt)
        company = res.scalars().first()
        
        if not company: return False
        
        # Check stats
        stmt2 = select(models.Stats).where(models.Stats.cik == company.cik)
        res2 = await session.execute(stmt2)
        stats = res2.scalars().first()
        
        if stats and stats.last_updated:
            if datetime.utcnow() - stats.last_updated < timedelta(hours=24):
                return True
                
        return False

async def main():
    logger.info("🚀 Starting Full Market Stats Sync...")
    
    # 1. Get List
    tickers = await get_all_us_stocks()
    if not tickers:
        logger.error("No tickers found. Exiting.")
        return

    # 2. Sync Loop
    semaphore = asyncio.Semaphore(10) # 10 concurrent requests
    
    async def process(ticker):
        async with semaphore:
            try:
                if await should_skip_sync(ticker):
                    logger.info(f"Skipping {ticker} (Fresh)")
                    return

                # Perform Sync
                logger.info(f"Syncing {ticker}...")
                success = await sync.sync_company_stats(ticker)
                if success:
                    logger.info(f"✅ Synced {ticker}")
                else:
                    logger.warning(f"⚠️ Failed {ticker}")
            except Exception as e:
                logger.error(f"Error processing {ticker}: {e}")

    # Process in chunks to avoid blowing up memory/tasks
    CHUNK_SIZE = 50
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i+CHUNK_SIZE]
        logger.info(f"Processing batch {i} to {i+len(chunk)}...")
        await asyncio.gather(*[process(t) for t in chunk])
        
    logger.info("🎉 Market Sync Complete!")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
