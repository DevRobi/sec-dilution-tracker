import httpx
import pandas as pd
import asyncio
from datetime import datetime, timedelta
import logging
import yfinance as yf
import os
import re
from bs4 import BeautifulSoup
import html
import json

logger = logging.getLogger(__name__)

# The legacy offering scanner and the daily AI worker both call Claude through
# smart_dilution_extractor.llm_providers. Import the legacy helpers lazily so
# ticker/stat sync utilities don't pull in the LLM stack.
_extractor = None
_summarizer = None


def _get_legacy_extractor():
    global _extractor
    if _extractor is None:
        from app.services.ai.extraction import DilutionExtractor

        _extractor = DilutionExtractor()
    return _extractor


def _get_legacy_summarizer():
    global _summarizer
    if _summarizer is None:
        from app.services.ai.summary import DilutionSummarizer

        _summarizer = DilutionSummarizer()
    return _summarizer

# Local LLM-friendly filing scan controls. These defaults keep memory pressure
# low on 16GB Macs while still reading every fetched filing end-to-end.
MAX_AI_CHUNKS_PER_FILING = int(os.getenv("MAX_AI_CHUNKS_PER_FILING", "8"))
AI_CHUNK_CONTEXT_CHARS = int(os.getenv("AI_CHUNK_CONTEXT_CHARS", "1800"))
MAX_AI_CHUNK_CHARS = int(os.getenv("MAX_AI_CHUNK_CHARS", "4200"))

# SEC requires a descriptive User-Agent with a real contact email.
# Configure via SEC_USER_AGENT, e.g. "DilutionTerminal/1.0 (you@domain.com)".
HEADERS = {
    "User-Agent": os.getenv("SEC_USER_AGENT", "DilutionTerminal/1.0 (contact@example.com)")
}

# --- SEC Rate Limiting ---
# SEC allows ~10 requests/second. We use a semaphore to prevent flooding.
SEC_SEMAPHORE = asyncio.Semaphore(5)  # Max 5 concurrent SEC requests
SEC_DELAY = 0.15  # 150ms between SEC batches

async def sec_request(client, url, **kwargs):
    """Rate-limited request to SEC APIs."""
    async with SEC_SEMAPHORE:
        response = await client.get(url, **kwargs)
        await asyncio.sleep(SEC_DELAY)  # Polite delay
        return response

# --- Shared Async Client ---
_client = None

async def get_client():
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=HEADERS, 
            timeout=30.0, 
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)  # Reduced to avoid SEC throttling
        )
    return _client

# We need a way to close it on shutdown, but for now relying on process exit is okay for dev
# Ideally, we hook into FastAPI shutdown event in main.py to close this.

_ticker_cache = None
_ticker_cache_time = None
_ticker_lock = asyncio.Lock()
_ticker_cache_ready = asyncio.Event()  # Signal when cache is ready

# Docker-friendly path - use /app for container, fallback for local dev
CACHE_FILE = "/app/ticker_cache.json" if os.path.exists("/app") else "ticker_cache.json"

def is_cache_ready():
    """Check if ticker cache is loaded and ready."""
    return _ticker_cache is not None and len(_ticker_cache) > 0

async def get_tickers(wait_for_cache: bool = True):
    """
    Fetches the mapping of tickers to CIKs from SEC.
    Effective cache duration: 1 hour.
    Persists to disk to survive server restarts.
    
    Args:
        wait_for_cache: If True, waits for cache to be ready (for search requests)
    """
    global _ticker_cache, _ticker_cache_time
    
    # 1. RAM Cache (instant)
    if _ticker_cache and _ticker_cache_time and (datetime.now() - _ticker_cache_time < timedelta(hours=1)):
        return _ticker_cache

    # 2. Disk Cache (Fast fallback)
    if not _ticker_cache and os.path.exists(CACHE_FILE):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
            # Allow 24h disk cache for faster startup
            if datetime.now() - mtime < timedelta(hours=24):
                logger.info(f"Loading tickers from disk cache ({CACHE_FILE})...")
                with open(CACHE_FILE, 'r') as f:
                    _ticker_cache = json.load(f)
                    _ticker_cache_time = mtime
                    logger.info(f"✅ Loaded {len(_ticker_cache)} tickers from disk in RAM.")
                    _ticker_cache_ready.set()
                    return _ticker_cache
        except Exception as e:
            logger.error(f"Error reading disk cache: {e}")

    # If cache is loading elsewhere, wait for it (prevents multiple SEC fetches)
    if wait_for_cache and not _ticker_cache:
        # Check if another task is already fetching
        if _ticker_lock.locked():
            logger.info("Waiting for ticker cache to load...")
            try:
                await asyncio.wait_for(_ticker_cache_ready.wait(), timeout=30)
                if _ticker_cache:
                    return _ticker_cache
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for ticker cache")

    async with _ticker_lock:
        # Double check RAM inside lock
        if _ticker_cache and _ticker_cache_time and (datetime.now() - _ticker_cache_time < timedelta(hours=1)):
             return _ticker_cache

        logger.info("🔄 Fetching tickers from SEC (this may take a few seconds)...")
        url = "https://www.sec.gov/files/company_tickers.json"
        try:
            client = await get_client()
            fetch_start = datetime.now()
            # Use rate-limited request
            response = await sec_request(client, url)
            duration = (datetime.now() - fetch_start).total_seconds()
            logger.info(f"SEC Response: {response.status_code} took {duration:.2f}s")
            response.raise_for_status()
            data = response.json()
            
            # Convert dict of dicts to list and pre-process for search
            companies = []
            for item in data.values():
                companies.append({
                    "cik_str": item["cik_str"],
                    "ticker": item["ticker"],
                    "title": item["title"],
                    "title_search": item["title"].upper()
                })
            
            # Sort by ticker for binary search
            companies.sort(key=lambda x: x['ticker'])
            
            # Update RAM Cache
            _ticker_cache = companies
            _ticker_cache_time = datetime.now()
            _ticker_cache_ready.set()
            
            # Save to Disk Cache
            try:
                with open(CACHE_FILE, 'w') as f:
                    json.dump(companies, f)
                logger.info(f"✅ Ticker cache saved to disk: {CACHE_FILE}")
            except Exception as e:
                logger.error(f"Failed to write disk cache: {e}")
            
            logger.info(f"✅ Ticker cache ready: {len(companies)} companies loaded.")
            return companies
        except Exception as e:
            logger.error(f"❌ Error fetching tickers: {e}")
            # Return empty but don't block
            return _ticker_cache or []

import bisect
import time

# Pre-built search index for O(log N) ticker prefix search
_ticker_index = None
_ticker_index_time = None
_search_result_cache = {}
_SEARCH_CACHE_TTL_SECONDS = 30

def _build_search_index_sync(tickers):
    """Build a sorted list of ticker symbols for binary search. SYNC version."""
    global _ticker_index, _ticker_index_time
    if _ticker_index is None or _ticker_index_time != _ticker_cache_time:
        _ticker_index = [t['ticker'] for t in tickers]
        _ticker_index_time = _ticker_cache_time
    return _ticker_index

def search_tickers_sync(query: str, limit: int = 10):
    """
    SYNC version of search - uses only in-memory cache, never waits.
    This is critical for instant search response.
    Runs in dedicated thread pool, isolated from async event loop.
    """
    st = time.time()
    if not query: return []
    query = query.upper()

    cache_key = f"{query}:{limit}"
    cached = _search_result_cache.get(cache_key)
    if cached:
        expires_at, value = cached
        if time.monotonic() < expires_at:
            return value
        _search_result_cache.pop(cache_key, None)
    
    # Use ONLY RAM cache - if not loaded, return empty (don't block)
    if not _ticker_cache:
        logger.warning(f"⚠️ Search '{query}' - cache not ready!")
        return []
    
    tickers = _ticker_cache
    matches = []
    
    # 1. Ticker Prefix Match using Binary Search
    ticker_symbols = _build_search_index_sync(tickers)
    start_idx = bisect.bisect_left(ticker_symbols, query)
    
    for i in range(start_idx, len(ticker_symbols)):
        if ticker_symbols[i].startswith(query):
            matches.append(tickers[i])
            if len(matches) >= limit:
                break
        elif ticker_symbols[i] > query + 'Z' * 10:
            break
            
    # 2. Title Match (Fallback)
    if len(matches) < limit:
        seen = set(m['ticker'] for m in matches)
        for t in tickers:
            if t['ticker'] not in seen and query in t['title_search']:
                matches.append(t)
                if len(matches) >= limit: break
                 
    duration = (time.time() - st) * 1000
    # Always log search timing for debugging
    logger.info(f"🔍 Search '{query}' → {len(matches)} results in {duration:.1f}ms")
    results = matches[:limit]
    _search_result_cache[cache_key] = (time.monotonic() + _SEARCH_CACHE_TTL_SECONDS, results)
    return results

async def search_tickers(query: str, limit: int = 10):
    """
    Async wrapper - but search itself is sync and instant.
    """
    # Run sync search in thread to not block event loop parsing
    # But actually since it's pure CPU with in-memory data, it's faster to just run sync
    return search_tickers_sync(query, limit)

async def get_cik_helper(ticker):
    """
    Finds CIK for a given ticker from SEC cache.
    """
    tickers = await get_tickers()
    for t in tickers:
        if t.get('ticker') == ticker.upper():
            return str(t.get('cik_str')).zfill(10)
    return None

async def get_company_title(ticker=None, cik=None):
    """
    Return the official company name from the SEC ticker cache
    (company_tickers.json). This is authoritative and always present, unlike the
    optional Polygon details. Matches by ticker first, then by CIK.
    """
    tickers = await get_tickers()
    if ticker:
        tu = str(ticker).upper()
        for t in tickers:
            if str(t.get('ticker', '')).upper() == tu:
                return t.get('title')
    if cik:
        try:
            cik_int = int(cik)
        except (TypeError, ValueError):
            cik_int = None
        if cik_int is not None:
            for t in tickers:
                if int(t.get('cik_str', -1)) == cik_int:
                    return t.get('title')
    return None

async def get_share_history(cik):
    """
    Fetches the historical share counts for a given CIK (SEC XBRL).
    """
    cik_padded = str(cik).zfill(10)
    
    concepts = [
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("ifrs-full", "NumberOfSharesOutstanding"),
        ("ifrs-full", "IssuedCapital"),
        ("dei", "EntityCommonStockSharesOutstanding")
    ]
    
    valid_dfs = []
    
    valid_dfs = []
    
    async def fetch_concept(client, taxonomy, concept):
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik_padded}/{taxonomy}/{concept}.json"
        try:
            response = await sec_request(client, url)
            if response.status_code == 404: return None
            if response.status_code != 200:
                logger.warning(f"Error fetching data for CIK {cik} {concept}: {response.status_code}")
                return None

            data = response.json()
            if "units" not in data or "shares" not in data["units"]: return None

            history = data["units"]["shares"]
            df = pd.DataFrame(history)
            
            if not df.empty and 'end' in df.columns and 'filed' in df.columns:
                df['end'] = pd.to_datetime(df['end'])
                df['filed'] = pd.to_datetime(df['filed'])
                return df
        except Exception as e:
            logger.error(f"Error fetching XBRL for {cik} {concept}: {e}")
        return None



    client = await get_client()
    # Fetch submissions to map accessionNumber -> (primaryDocument, form)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    accn_map = {}
    try:
        sub_res = await sec_request(client, submissions_url)
        if sub_res.status_code == 200:
            sub_data = sub_res.json()
            recent = sub_data.get('filings', {}).get('recent', {})
            if recent:
                for i in range(len(recent.get('accessionNumber', []))):
                    full_acc = recent['accessionNumber'][i]
                    acc_no_dashes = full_acc.replace('-', '')
                    doc = recent['primaryDocument'][i]
                    form = recent['form'][i]
                    accn_map[full_acc] = {
                        'url': f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dashes}/{doc}",
                        'form': form
                    }
    except Exception as e:
        logger.warning(f"Error fetching submissions for mapping: {e}")

    tasks = [fetch_concept(client, tax, con) for tax, con in concepts]
    results = await asyncio.gather(*tasks)
    
    for df in results:
        if df is not None:
            valid_dfs.append(df)

    if not valid_dfs:
        return None
    
    combined_df = pd.concat(valid_dfs, ignore_index=True)
    
    # Sort by end (asc) and filed (asc)
    combined_df = combined_df.sort_values(by=['end', 'filed'])
    # Keep the EARLIEST filing for each period end (usually the 10-Q/K)
    combined_df = combined_df.drop_duplicates(subset=['end'], keep='first')
    
    # Map URLs and Forms using accn (Accession Number)
    def map_accn(row):
        acc = row.get('accn')
        mapped = accn_map.get(acc, {})
        row['source_url'] = mapped.get('url')
        if mapped.get('form'):
            row['form'] = mapped['form']
        elif not row.get('form'):
            row['form'] = 'Unknown'
        return row

    combined_df = combined_df.apply(map_accn, axis=1)
    
    return combined_df

async def get_share_history_yf(ticker):
    """
    Fetches full share count history using yfinance (Blocking, run in thread).
    """
    if not ticker: return None
        
    def fetch():
        try:
            t = yf.Ticker(ticker)
            shares = t.get_shares_full(start="1900-01-01")
            
            if shares is None or shares.empty: return None
                
            df = shares.reset_index()
            df.columns = ['end', 'val']
            df['end'] = pd.to_datetime(df['end']).dt.tz_localize(None)
            df['filed'] = df['end']
            return df
        except Exception:
            return None

    return await asyncio.to_thread(fetch)

async def get_8k_share_data(cik):
    """
    Parses recent 8-K filings for share count data.
    """
    cik = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    
    client = await get_client() 
    try:
        response = await sec_request(client, url)
        if response.status_code != 200: return None
            
        data = response.json()
        filings = data['filings']['recent']
        df_filings = pd.DataFrame(filings)
        
        # Filter for 8-K
        k8s = df_filings[df_filings['form'] == '8-K']
        if k8s.empty: return None
        
        # Simple heuristic: Check recent 5
        relevant_k8s = k8s.head(5)
        found_data = []
        
        for _, row in relevant_k8s.iterrows():
            acc_no = row['accessionNumber'].replace('-', '')
            doc_name = row['primaryDocument']
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/{doc_name}"
            
            try:
                r = await sec_request(client, doc_url)
                if r.status_code != 200: continue
                    
                text = r.text
                
                # Regex logic (Simplified for brevity, similar to original)
                date_pattern = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
                p1 = re.search(rf"As of ({date_pattern}).*?([0-9,]{{3,15}})\s+shares.*?outstanding", text, re.IGNORECASE)
                
                if p1:
                    date_str = p1.group(1)
                    val_str = p1.group(2)
                    val = float(val_str.replace(',', ''))
                    date = pd.to_datetime(date_str)
                    found_data.append({
                        'end': date,
                        'val': val,
                        'filed': pd.to_datetime(row['filingDate']),
                        'form': '8-K',
                        'source_url': doc_url
                    })
            except Exception:
                pass
        
        if not found_data: return None
        return pd.DataFrame(found_data)
        
    except Exception as e:
        logger.error(f"Error 8k scan {cik}: {e}")
        return None

async def get_shares_history_polygon(ticker, api_key):
    if not ticker: return None
    url = "https://api.polygon.io/vX/reference/financials"
    params = {'ticker': ticker, 'limit': 100, 'apiKey': api_key, 'order': 'desc'}
    
    client = await get_client()
    try:
        response = await client.get(url, params=params)
        if response.status_code != 200: return None
        data = response.json()
        results = data.get('results', [])
        if not results: return None
        
        found_data = []
        for res in results:
            end_date = res.get('end_date')
            filing_date = res.get('filing_date')
            form = res.get('form')
            bs = res.get('financials', {}).get('balance_sheet', {})
            
            # Check concepts
            shares_val = None
            for c in ['common_stock_shares_outstanding', 'entity_common_stock_shares_outstanding', 'shares_outstanding']:
                if c in bs:
                    shares_val = bs[c].get('value')
                    break
            
            if shares_val:
                found_data.append({
                    'end': pd.to_datetime(end_date),
                    'val': float(shares_val),
                    'filed': pd.to_datetime(filing_date),
                    'form': form,
                    'source_url': res.get('source_filing_url')
                })
        
        if not found_data: return None
        return pd.DataFrame(found_data)
    except Exception:
        return None

async def get_share_history_combined(cik, ticker=None):
    dfs = []
    
    # Run all data sources concurrently
    polygon_key = os.getenv('POLYGON_API_KEY')
    
    tasks = []
    if polygon_key and ticker:
        tasks.append(get_shares_history_polygon(ticker, polygon_key))
    
    tasks.append(get_share_history(cik))
    tasks.append(get_8k_share_data(cik))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for res in results:
        if isinstance(res, pd.DataFrame) and not res.empty:
            dfs.append(res)
            
    if not dfs: return None
    
    combined = pd.concat(dfs, ignore_index=True)
    if 'filed' not in combined.columns: combined['filed'] = combined['end']
    
    # Sort by date
    combined = combined.sort_values(by=['end', 'filed'])
    
    # Remove exact date duplicates first
    combined = combined.drop_duplicates(subset=['end'], keep='last')
    
    # Aggregate to ONE entry per month (keep the latest entry for each month)
    # This prevents charts from having 30+ entries per month from daily YFinance data
    combined['year_month'] = combined['end'].dt.to_period('M')
    combined = combined.sort_values(by=['year_month', 'end'])
    combined = combined.drop_duplicates(subset=['year_month'], keep='last')
    combined = combined.drop(columns=['year_month'])
    
    return combined

async def get_ticker_details_polygon(ticker, api_key):
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
    params = {'apiKey': api_key}
    client = await get_client()
    try:
        r = await client.get(url, params=params)
        res = r.json()
        return res.get('results', {}) if r.status_code == 200 else None
    except: return None

async def get_current_price(ticker):
    # Async price fetch: Finnhub -> Polygon -> YF
    finnhub_key = os.getenv('FINNHUB_API_KEY')
    client = await get_client()
    if finnhub_key:
        try:
            r = await client.get("https://finnhub.io/api/v1/quote", params={'symbol': ticker, 'token': finnhub_key})
            if r.status_code == 200:
                d = r.json()
                if d.get('c'): return float(d['c'])
        except: pass
        
    polygon_key = os.getenv('POLYGON_API_KEY')
    if polygon_key:
        try:
            r = await client.get(f"https://api.polygon.io/v2/last/trade/{ticker}", params={'apiKey': polygon_key})
            if r.status_code == 200:
                d = r.json()
                if 'results' in d: return float(d['results']['p'])
        except: pass
            
    # Fallback to YF (sync)
    def fetch_yf():
         try:
             t = yf.Ticker(ticker)
             if hasattr(t, 'fast_info') and 'last_price' in t.fast_info: return float(t.fast_info['last_price'])
             return t.history(period="1d")['Close'].iloc[-1]
         except: return None
    
    return await asyncio.to_thread(fetch_yf)

async def get_price_history(ticker):
    # Simplified async price history 
    # Try Polygon first for clear data
    polygon_key = os.getenv('POLYGON_API_KEY')
    if polygon_key:
        client = await get_client()
        end = datetime.now().strftime('%Y-%m-%d')
        start_5y = (datetime.now() - timedelta(days=365*5)).strftime('%Y-%m-%d')
        start_7d = (datetime.now() - timedelta(days=8)).strftime('%Y-%m-%d') # 8d to be safe
        
        # 1. Fetch Daily Data (5 years)
        daily_url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_5y}/{end}"
        params_daily = {'adjusted': 'true', 'limit': 50000, 'apiKey': polygon_key}
        
        # 2. Fetch Hourly Data (7 days)
        hourly_url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/hour/{start_7d}/{end}"
        params_hourly = {'adjusted': 'true', 'limit': 50000, 'apiKey': polygon_key}
        
        try:
            # Fetch concurrently
            res_daily, res_hourly = await asyncio.gather(
                client.get(daily_url, params=params_daily),
                client.get(hourly_url, params=params_hourly)
            )
            
            all_points = []
            
            if res_daily.status_code == 200:
                data = res_daily.json()
                if data.get('results'):
                    for x in data['results']:
                        all_points.append({
                            'date': pd.to_datetime(x['t'], unit='ms').tz_localize(None),
                            'price': x['c'],
                            'source': 'daily'
                        })
            
            if res_hourly.status_code == 200:
                data = res_hourly.json()
                if data.get('results'):
                    for x in data['results']:
                        all_points.append({
                            'date': pd.to_datetime(x['t'], unit='ms').tz_localize(None),
                            'price': x['c'],
                            'source': 'hourly'
                        })
            
            if all_points:
                df = pd.DataFrame(all_points)
                # For the overlapping period, hourly data is more granular and usually preferred for those specific timestamps
                # But we want to keep one record per unique timestamp
                # Actually, Polygon's daily 't' is start of day, hourly 't' is start of hour.
                # They won't overlap exactly except maybe at 00:00:00.
                df = df.sort_values(by=['date', 'source'])
                # If we have same timestamp from both, keep hourly? (unlikely but safe)
                df = df.drop_duplicates(subset=['date'], keep='last')
                return df[['date', 'price']]
        except Exception as e:
            logger.error(f"Error fetching Polygon price history: {e}")
            
    # Fallback YF
    def fetch_yf():
        try:
            hist = yf.Ticker(ticker).history(period="max")
            if hist.empty: return pd.DataFrame()
            df = hist.reset_index()[['Date', 'Close']].copy()
            df.columns = ['date', 'price']
            df['date'] = df['date'].dt.tz_localize(None)
            return df
        except: return pd.DataFrame()

    return await asyncio.to_thread(fetch_yf)

def calculate_changes(df):
    # This is pure CPU bound, no async needed, but valid to keep sync
    if df is None or df.empty: return {}
    # Zero/negative share counts are XBRL tagging errors; letting one become the
    # "latest" point produced stats.latest_shares=0 (see data_quality audit).
    df = df[df['val'] > 0]
    if df.empty: return {}
    df = df.sort_values(by='end')
    latest_row = df.iloc[-1]
    latest_date = latest_row['end']
    latest_val = float(latest_row['val'])
    
    stats = {
        'latest_shares': int(latest_val),
        'last_date': latest_date.strftime('%Y-%m-%d'),
        'prev_shares_1y': None,
        'change_pct_1q': None
    }
    
    for y in range(1, 11):
        stats[f'change_pct_{y}y'] = None
        stats[f'date_{y}y'] = None

    def get_val(months):
        target = latest_date - timedelta(days=30*months)
        # simplistic closest
        idx = (df['end'] - target).abs().idxmin()
        row = df.loc[idx]
        # abs(): the nearest point may be BEFORE the target. Without abs a
        # "1Y" change could silently compare against a point 1.5+ years back.
        if abs((row['end'] - target).days) > 90: return None, None
        return float(row['val']), row['end']

    # 1Q
    v1q, d1q = get_val(3)
    if v1q:
        stats['change_pct_1q'] = ((latest_val - v1q)/v1q)*100
        stats['date_1q'] = d1q.strftime('%Y-%m-%d')
        
    for y in range(1, 11):
        vy, dy = get_val(y*12)
        if vy:
            stats[f'change_pct_{y}y'] = ((latest_val - vy)/vy)*100
            stats[f'date_{y}y'] = dy.strftime('%Y-%m-%d')
            if y == 1: stats['prev_shares_1y'] = int(vy)
            
    return stats

def _clean_filing_text(html_text: str) -> str:
    """Convert SEC HTML into compact readable text for deterministic scanning."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "ix:header", "ix:hidden"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = html.unescape(text)
    return " ".join(text.split())


def _is_boilerplate_only(snippet: str) -> bool:
    """
    Filter generic risk-factor dilution warnings unless they mention a concrete
    instrument, agreement, registration statement, warrant, or dollar amount.
    """
    lowered = snippet.lower()
    risk_terms = (
        "risk factors",
        "may dilute",
        "could dilute",
        "substantial dilution",
        "future issuances",
    )
    concrete_terms = (
        "sales agreement",
        "purchase agreement",
        "registration statement",
        "prospectus supplement",
        "at-the-market",
        "warrant",
        "convertible",
        "preferred stock",
        "$",
    )
    return any(term in lowered for term in risk_terms) and not any(term in lowered for term in concrete_terms)


def _score_dilution_snippet(snippet: str, keywords: dict) -> int:
    score = 0
    category_weights = {
        "ATM Offering": 10,
        "Series / Direct": 10,
        "Shelf Offering": 8,
        "Convertible Note": 8,
        "Warrants / SAFEs": 8,
        "SBC / Stock-Based Comp": 4,
    }

    for category, patterns in keywords.items():
        matches = sum(1 for pat in patterns if re.search(pat, snippet, re.IGNORECASE))
        if matches:
            score += category_weights.get(category, 5) * matches

    high_signal_patterns = [
        r"\$\s*[0-9]",
        r"up to\s+\$?",
        r"aggregate(?:\s+gross)?\s+proceeds",
        r"entered into",
        r"closed (?:the|an)",
        r"prospectus supplement",
        r"registration statement",
        r"common stock",
        r"ordinary shares",
    ]
    score += sum(3 for pat in high_signal_patterns if re.search(pat, snippet, re.IGNORECASE))

    if _is_boilerplate_only(snippet):
        score -= 20

    return score


def _compact_snippet(text: str, max_chars: int = MAX_AI_CHUNK_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def identify_relevant_chunks(html_text, keywords):
    """
    Read the whole filing deterministically, then return only the highest-signal
    dilution snippets for local AI extraction.

    This keeps local Ollama models from ingesting raw 10-Q/10-K filings while
    still scanning the entire document for relevant language.
    """
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        candidates: list[tuple[int, int, str]] = []

        # 1. Score relevant tables. Tables often contain exact warrant,
        # convertible, and stock-compensation terms that prose misses.
        for table in soup.find_all('table'):
            table_text = html.unescape(table.get_text(" ", strip=True))
            table_text = " ".join(table_text.split())
            if len(table_text) < 80:
                continue
            score = _score_dilution_snippet(table_text, keywords) + 4
            if score > 0:
                candidates.append((score, len(table_text), _compact_snippet(table_text, 2600)))

        # 2. Scan normalized full text and merge overlapping windows around all
        # keyword hits. This is the "read entire filing, feed targeted chunks" step.
        full_text = _clean_filing_text(html_text)
        windows: list[tuple[int, int]] = []
        for category, patterns in keywords.items():
            for pat in patterns:
                for m in re.finditer(pat, full_text, re.IGNORECASE):
                    start = max(0, m.start() - AI_CHUNK_CONTEXT_CHARS)
                    end = min(len(full_text), m.end() + AI_CHUNK_CONTEXT_CHARS)
                    windows.append((start, end))

        if windows:
            windows.sort()
            merged: list[list[int]] = []
            for start, end in windows:
                if not merged or start > merged[-1][1] + 400:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)

            for start, end in merged:
                snippet = _compact_snippet(full_text[start:end])
                if len(snippet) < 100:
                    continue
                score = _score_dilution_snippet(snippet, keywords)
                if score > 0:
                    candidates.append((score, len(snippet), snippet))

        # 3. Deduplicate near-identical snippets and keep the highest-scoring,
        # richest chunks. The AI still sees every relevant instrument type, but
        # not dozens of repeated risk-factor paragraphs.
        deduped: dict[str, tuple[int, int, str]] = {}
        for score, length, snippet in candidates:
            key = re.sub(r"\W+", " ", snippet[:500].lower()).strip()
            existing = deduped.get(key)
            if existing is None or (score, length) > (existing[0], existing[1]):
                deduped[key] = (score, length, snippet)

        ranked = sorted(deduped.values(), key=lambda item: (item[0], item[1]), reverse=True)
        return [snippet for _, _, snippet in ranked[:MAX_AI_CHUNKS_PER_FILING]]
    
    except Exception as e:
        logger.warning(f"Error parsing HTML: {e}")
        return []


def _amount_to_float(amount_str: str, unit: str | None, context_text: str = "") -> float | None:
    """Convert extracted amount text to float dollars."""
    try:
        base = float(amount_str.replace(",", ""))
    except Exception:
        return None

    if unit:
        unit_l = unit.lower()
        if unit_l.startswith("b"):
            return base * 1_000_000_000
        if unit_l.startswith("m"):
            return base * 1_000_000
        if unit_l.startswith("t") or unit_l.startswith("k"):
            return base * 1_000
        return base

    # If the filing section states values are presented in millions, scale bare numbers.
    if re.search(r"in\s+millions", context_text, re.IGNORECASE) and base <= 10000:
        return base * 1_000_000

    return base


def _extract_amount_near_offering(text: str) -> tuple[float | None, str | None]:
    amount_patterns = [
        r"up to\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(billion|million|thousand|bn|mn|m|b)?",
        r"aggregate(?:\s+gross)?\s+proceeds?\s+(?:of\s+)?up\s+to\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(billion|million|thousand|bn|mn|m|b)?",
        r"(?:offering|sales agreement|equity distribution)[^$]{0,200}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(billion|million|thousand|bn|mn|m|b)",
    ]
    for pat in amount_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        amount = _amount_to_float(m.group(1), m.group(2), text)
        if amount is not None:
            return amount, m.group(0)
    return None, None


def _extract_rule_based_offering_events(html_text: str, filing_info: dict) -> list[dict]:
    """
    Deterministic fallback extraction to catch obvious offerings/amounts when LLM misses.
    """
    try:
        full_text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
        full_text = html.unescape(" ".join(full_text.split()))
    except Exception:
        return []

    events = []
    source_filing = filing_info.get("source_filing", filing_info.get("form", "Unknown"))
    source_url = filing_info.get("url")
    filing_date = filing_info.get("filing_date")

    def add_event(category: str, status: str, description: str, snippet: str):
        amount, amount_text = _extract_amount_near_offering(snippet)
        events.append(
            {
                "category": category,
                "amount": amount,
                "amount_text": amount_text,
                "status": status,
                "description": description,
                "date": filing_date,
                "source_url": source_url,
                "source_filing": source_filing,
                "text": snippet[:500],
            }
        )

    # ATM / Equity distribution fallback
    for m in re.finditer(r"at-the-market|ATM program|equity distribution|sales agreement", full_text, re.IGNORECASE):
        start = max(0, m.start() - 500)
        end = min(len(full_text), m.end() + 500)
        snippet = full_text[start:end]
        add_event("ATM Offering", "Active", "Rule-based ATM detection.", snippet)

    # Shelf fallback
    for m in re.finditer(r"shelf registration|base prospectus|automatic shelf", full_text, re.IGNORECASE):
        start = max(0, m.start() - 500)
        end = min(len(full_text), m.end() + 500)
        snippet = full_text[start:end]
        add_event("Shelf Offering", "Potential", "Rule-based shelf detection.", snippet)

    # Deduplicate fallback events by category/source_url; prefer event with explicit amount.
    dedup = {}
    for e in events:
        key = (e["category"], e["source_url"])
        existing = dedup.get(key)
        if not existing:
            dedup[key] = e
            continue
        existing_has_amount = existing.get("amount") is not None
        new_has_amount = e.get("amount") is not None
        if new_has_amount and not existing_has_amount:
            dedup[key] = e

    return list(dedup.values())

async def get_offering_text(cik: str, limit: int = 30, periodic_callback=None, ignore_filings=None):
    """
    Scrapes SEC filings for text related to offerings and uses AI to extract structured info.
    Now supports configurable limit for 'deep' scans.
    """
    cik = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    
    client = await get_client()
    try:
        response = await sec_request(client, url)
        if response.status_code != 200:
            return pd.DataFrame()
            
        data = response.json()
        filings = data['filings']['recent']
        df_filings = pd.DataFrame(filings)
        
        # Include F-3, F-3ASR, F-1 for foreign issuers (like Canadian companies), and 6-K for material events
        relevant_forms = ['10-K', '10-Q', '8-K', 'S-3', 'S-3ASR', 'F-3', 'F-3ASR', 'F-1', '6-K', '424B', 'S-1', 'S-8', '424B5', '424B2', '424B3', '424B4', '424B7', '424B8', '20-F']
        pattern = '|'.join(relevant_forms)
        df_relevant = df_filings[df_filings['form'].astype(str).str.contains(pattern, na=False, case=False)]
        
        # Use configurable limit (defaults to 30, but scanner can set 50+)
        df_relevant = df_relevant.head(limit)
        
        found_data = []
        keywords = {
            'ATM Offering': [
                r"at-the-market", r"sales agreement", r"equity distribution", 
                r"ATM program", r"controlled equity", r"open market sale",
                r"purchase agreement"
            ],
            'Convertible Note': [
                r"convertible senior notes?", r"convertible notes? due 20", 
                r"convertible debt", r"exchangeable notes?", 
                r"convertible senior subordinated", r"conversion price",
                r"principal amount", r"notes? payable", r"indenture"
            ],
            'Warrants / SAFEs': [
                r"warrants?", r"SAFE agreement", r"simple agreement for future equity",
                r"warrant issuance", r"pre-funded warrants?", r"series [a-z] warrants?"
            ],
            'Series / Direct': [
                r"registered direct", r"common stock and warrants", r"purchase price of \$",
                r"PIPE", r"securities purchase agreement", r"private placement"
            ],
            'SBC / Stock-Based Comp': [
                r"stock-based compensation", r"share-based compensation", 
                r"restricted stock units", r"RSUs?", r"stock options",
                r"equity incentive plan", r"share-based payment"
            ],
            'Shelf Offering': [
                r"shelf registration", r"base prospectus", 
                r"automatic shelf", r"shelf offering",
                r"registration statement on form s-3", r"anti-dilution"
            ]
        }
        
        # Build list of filing URLs to fetch
        filings_to_fetch = []
        skipped_count = 0
        for _, row in df_relevant.iterrows():
            acc_no = row['accessionNumber'].replace('-', '')
            doc_name = row['primaryDocument']
            if not doc_name or doc_name.endswith('.xml'): continue 
            
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/{doc_name}"
            
            # SKIPPING LOGIC: If this URL or accession number is already known, skip it
            if ignore_filings and (doc_url in ignore_filings or row['accessionNumber'] in ignore_filings or doc_name in ignore_filings):
                skipped_count += 1
                continue

            filings_to_fetch.append({
                'url': doc_url,
                'filing_date': row['filingDate'],
                'form': row['form'],
                'accession': row['accessionNumber'],
                'source_filing': f"{row['form']}:{row['accessionNumber']}",
            })

        if skipped_count:
            logger.info(f"Skipped {skipped_count} already-processed filings for CIK {cik}")
        
        # Batch fetch filings using rate-limited requests
        BATCH_SIZE = 5 
        
        # Limit concurrent AI extractions to avoid local LLM (Ollama) bottleneck
        AI_SEMAPHORE = asyncio.Semaphore(1)

        async def process_chunk(chunk, filing_info):
            async with AI_SEMAPHORE:
                found_events = await _get_legacy_extractor().extract(
                    chunk, filing_info['form']
                )
                chunk_events = []
                for e in found_events:
                    e['date'] = filing_info['filing_date']
                    e['source_url'] = filing_info['url']
                    e['source_filing'] = filing_info['source_filing']
                    # Use AI description or fallback to snippet
                    e['text'] = e.get('description', chunk[:500])
                    chunk_events.append(e)
                return chunk_events

        async def fetch_and_parse(filing_info):
            try:
                r = await sec_request(client, filing_info['url'])
                if r.status_code != 200: return []
                
                # Offload CPU-bound identification of chunks
                chunks = await asyncio.to_thread(
                    identify_relevant_chunks, 
                    r.text, 
                    keywords
                )

                events = []
                if chunks:
                    logger.info(
                        f"{filing_info['form']} {filing_info['accession']}: "
                        f"selected {len(chunks)} targeted chunks for AI extraction"
                    )
                    # Process chunks in parallel. The semaphore keeps local Ollama
                    # work serialized so a 16GB Mac does not get swamped.
                    tasks = [process_chunk(chunk, filing_info) for chunk in chunks]
                    chunk_results = await asyncio.gather(*tasks)
                    events = [item for sublist in chunk_results for item in sublist]

                # Deterministic fallback for obvious offering disclosures.
                events.extend(_extract_rule_based_offering_events(r.text, filing_info))
                return events
            except Exception as e:
                logger.warning(f"Error fetching/parsing doc {filing_info['url']}: {e}")
                return []
        
        # Step 1: Fetch and identify chunks for all filings in parallel batches
        total_batches = (len(filings_to_fetch) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(f"Processing {len(filings_to_fetch)} filings in {total_batches} batches for CIK {cik}")
        
        for i in range(0, len(filings_to_fetch), BATCH_SIZE):
            batch = filings_to_fetch[i:i + BATCH_SIZE]
            # Processes filings in batch, and each filing processes chunks in parallel
            results = await asyncio.gather(*[fetch_and_parse(f) for f in batch])
            batch_found = []
            for items in results:
                batch_found.extend(items)
            
            if batch_found:
                found_data.extend(batch_found)
                # Call callback if provided with incremental results
                if periodic_callback:
                    await periodic_callback(pd.DataFrame(batch_found))
        
        logger.info(f"Finished processing CIK {cik}: found {len(found_data)} potential events")
                
        if not found_data:
            return pd.DataFrame()
            
        df_found = pd.DataFrame(found_data)
        
        # Robust Deduplication: Group by date/category/amount and keep the longest description
        if not df_found.empty:
            # Prefer entries with explicit amounts and richer context.
            df_found['_has_amount'] = df_found['amount'].notna().astype(int)
            df_found['_len'] = df_found['text'].str.len()
            df_found = df_found.sort_values(by=['_has_amount', '_len'], ascending=False)
            dedupe_cols = ['date', 'category', 'source_url'] if 'source_url' in df_found.columns else ['date', 'category', 'source_filing']
            df_found = df_found.drop_duplicates(subset=dedupe_cols, keep='first')
            df_found = df_found.drop(columns=['_has_amount', '_len'])
            
        return df_found
        
    except Exception as e:
        logger.error(f"Error fetching offering text for CIK {cik}: {e}")
        return pd.DataFrame()

async def get_dilution_summary(ticker, events):
    """
    Generates a natural language summary of the active/potential dilution.
    """
    return await _get_legacy_summarizer().summarize(ticker, events)
