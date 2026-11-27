import requests
import pandas as pd
import time
from datetime import datetime, timedelta
import logging
import yfinance as yf
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SEC requires a proper User-Agent
# IMPORTANT: Replace the email below with your actual contact email to comply with SEC fair access policy.
HEADERS = {
    "User-Agent": "ShareTrackerApp contact@sharetracker.local"
}

def get_tickers():
    """
    Fetches the mapping of tickers to CIKs from SEC.
    Returns a list of dicts: {'cik_str': ..., 'ticker': ..., 'title': ...}
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        
        # The JSON is a dict of dicts indexed by "0", "1", ...
        # We convert it to a list
        companies = list(data.values())
        return companies
    except Exception as e:
        logger.error(f"Error fetching tickers: {e}")
        return []

def get_share_history(cik):
    """
    Fetches the historical share counts for a given CIK.
    """
    # Pad CIK to 10 digits
    cik_padded = str(cik).zfill(10)
    
    # List of concepts to try, in order of preference
    # Structure: (taxonomy, concept_name)
    concepts = [
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        # IFRS tags for foreign issuers (AZN, SAP, HSBC, NVS, etc.)
        ("ifrs-full", "NumberOfSharesOutstanding"),
        ("ifrs-full", "IssuedCapital"),
        ("dei", "EntityCommonStockSharesOutstanding")
    ]
    
    valid_dfs = []
    
    for taxonomy, concept in concepts:
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik_padded}/{taxonomy}/{concept}.json"
        
        try:
            response = requests.get(url, headers=HEADERS)
            if response.status_code == 404:
                continue # Try next concept
                
            if response.status_code != 200:
                logger.warning(f"Error fetching data for CIK {cik} with concept {concept} (Status {response.status_code})")
                continue

            data = response.json()
            
            if "units" not in data or "shares" not in data["units"]:
                 # specific check for EntityCommonStockSharesOutstanding which might be under different units? 
                 # usually it's under 'shares'
                continue

            history = data["units"]["shares"]
            
            # Convert to DataFrame
            df = pd.DataFrame(history)
            
            # We need 'end' date and 'val'. 
            # Sometimes duplicates exist for same 'end' date from different filings.
            # We prioritize the latest 'filed' date.
            df['end'] = pd.to_datetime(df['end'])
            df['filed'] = pd.to_datetime(df['filed'])
            
            # Sort by end date then filed date
            df = df.sort_values(by=['end', 'filed'])
            
            # Drop duplicates on 'end' keeping the last one (latest filing)
            df = df.drop_duplicates(subset=['end'], keep='last')
            
            # Append valid dataframe to list instead of returning immediately
            if not df.empty:
                valid_dfs.append(df)
            
        except Exception as e:
            logger.error(f"Error fetching history for CIK {cik} with concept {concept}: {e}")
            continue

    # Combine all found dataframes
    if not valid_dfs:
        logger.warning(f"No share data found for CIK {cik} after trying all concepts")
        return None
    
    # Concatenate all frames
    combined_df = pd.concat(valid_dfs, ignore_index=True)
    
    # Sort by end date then filed date to ensure priority
    combined_df = combined_df.sort_values(by=['end', 'filed'])
    
    # Drop duplicates on 'end' keeping the last one (latest filing)
    combined_df = combined_df.drop_duplicates(subset=['end'], keep='last')
    
    return combined_df

def get_share_history_yf(ticker):
    """
    Fetches full share count history using yfinance.
    Returns DataFrame with columns: end, val (matching SEC format)
    """
    if not ticker:
        return None
        
    try:
        t = yf.Ticker(ticker)
        shares = t.get_shares_full(start="1900-01-01") # Get all available
        
        if shares is None or shares.empty:
            return None
            
        # Convert Series to DataFrame
        df = shares.reset_index()
        df.columns = ['end', 'val']
        
        # Ensure 'end' is datetime and normalized
        df['end'] = pd.to_datetime(df['end']).dt.tz_localize(None)
        
        # Add 'filed' column as same as 'end' for compatibility (since we don't know filing date)
        df['filed'] = df['end']
        
        return df
        
    except Exception as e:
        logger.warning(f"Error fetching yfinance shares for {ticker}: {e}")
        return None

import re

# ... existing imports ...

def get_8k_share_data(cik):
    """
    Parses recent 8-K filings for share count data.
    Focuses on Item 3.02 (Unregistered Sales) and general 'shares outstanding' mentions.
    Returns a DataFrame with columns: end, val, filed
    """
    cik = str(cik).zfill(10) # No 'CIK' prefix for submissions URL usually, just number
    # But submissions URL uses CIK##########.json format
    
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    
    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            return None
            
        data = response.json()
        filings = data['filings']['recent']
        df_filings = pd.DataFrame(filings)
        
        # Filter for 8-K
        k8s = df_filings[df_filings['form'] == '8-K']
        
        if k8s.empty:
            return None
            
        # Filter for interesting items if available
        # We look for Item 3.02 (Unregistered Sales) or Item 8.01 (Other Events)
        # or Item 1.01 (Material Agreement)
        interesting_items = ['3.02', '1.01', '8.01']
        
        # 'items' column contains comma-separated strings
        if 'items' in k8s.columns:
            # Filter rows where items contain any of interesting items
            # This is a bit loose but catches most
            pattern = '|'.join(interesting_items)
            relevant_k8s = k8s[k8s['items'].astype(str).str.contains(pattern, na=False)]
            
            # If no specific relevant items found, maybe fallback to checking latest 3 8-Ks just in case?
            if relevant_k8s.empty:
                relevant_k8s = k8s.head(3)
        else:
            relevant_k8s = k8s.head(5) # Fallback
            
        # Limit to recent 5 to avoid long processing
        relevant_k8s = relevant_k8s.head(5)
        
        found_data = []
        
        for _, row in relevant_k8s.iterrows():
            # Construct Document URL
            acc_no = row['accessionNumber'].replace('-', '')
            doc_name = row['primaryDocument']
            # CIK in URL needs no leading zeros usually? No, it needs them.
            # https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20231102.htm
            # Wait, CIK in path is integer (no leading zeros) usually.
            
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/{doc_name}"
            
            try:
                time.sleep(0.1)
                r = requests.get(doc_url, headers=HEADERS, timeout=5)
                if r.status_code != 200:
                    continue
                    
                text = r.text
                
                # Simple cleaning
                # Remove script/style
                text = re.sub(r'<(script|style).*?>.*?</\1>', '', text, flags=re.DOTALL|re.IGNORECASE)
                # Remove tags
                text = re.sub(r'<[^>]+>', ' ', text)
                # Normalize whitespace
                text = ' '.join(text.split())
                
                # Search for "X shares of common stock outstanding"
                # "As of [Date], ... X shares"
                
                # Regex for date: Month DD, YYYY
                date_pattern = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
                
                # Pattern: "As of [Date], the registrant had [Number] shares of common stock outstanding"
                # We want to capture Date and Number
                # Be flexible with middle text
                
                # Match "As of Month DD, YYYY... Number shares ... outstanding"
                p1 = re.search(f"As of ({date_pattern}).*?([0-9,]{{3,15}})\s+shares.*?outstanding", text, re.IGNORECASE)
                
                if p1:
                    date_str = p1.group(1)
                    val_str = p1.group(2)
                    
                    try:
                        val = float(val_str.replace(',', ''))
                        date = pd.to_datetime(date_str)
                        
                        found_data.append({
                            'end': date,
                            'val': val,
                            'filed': pd.to_datetime(row['filingDate']),
                            'form': '8-K'
                        })
                        continue # Found one, move to next doc
                    except:
                        pass
                
                # Pattern: "indicated [Number] shares ... outstanding" (often on cover page)
                p2 = re.search(r"([0-9,]{3,15})\s+shares\s+(?:of\s+.*?)?common\s+stock\s+outstanding", text, re.IGNORECASE)
                if p2:
                    val_str = p2.group(1)
                    try:
                        val = float(val_str.replace(',', ''))
                        # Use filing date as proxy if "As of" date not found close by
                        # Or look for date before?
                        # Let's use filing date - 1 day or just filing date
                        date = pd.to_datetime(row['filingDate'])
                        
                        found_data.append({
                            'end': date,
                            'val': val,
                            'filed': date,
                            'form': '8-K'
                        })
                    except:
                        pass
                        
            except Exception as e:
                logger.warning(f"Error parsing 8-K {doc_url}: {e}")
                
        if not found_data:
            return None
            
        return pd.DataFrame(found_data)
        
    except Exception as e:
        logger.error(f"Error fetching submissions for CIK {cik}: {e}")
        return None

def get_shares_history_polygon(ticker, api_key):
    """
    Fetches historical shares outstanding from Polygon.io Financials API (vX).
    Returns DataFrame with columns: end, val, filed, form
    """
    if not ticker:
        return None
        
    url = "https://api.polygon.io/vX/reference/financials"
    params = {
        'ticker': ticker,
        'limit': 100,
        'apiKey': api_key,
        'order': 'desc'
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            return None
            
        data = response.json()
        results = data.get('results', [])
        if not results:
            return None
            
        found_data = []
        for res in results:
            fiscal_period = res.get('fiscal_period')
            fiscal_year = res.get('fiscal_year')
            filing_date = res.get('filing_date')
            form = res.get('form')
            end_date = res.get('end_date')
            
            # Fundamentals are in financials -> balance_sheet -> common_stock_shares_outstanding (concept varies)
            financials = res.get('financials', {})
            bs = financials.get('balance_sheet', {})
            
            # Common concepts for shares outstanding in Polygon
            share_concepts = [
                'common_stock_shares_outstanding',
                'entity_common_stock_shares_outstanding',
                'shares_outstanding'
            ]
            
            shares_val = None
            for concept in share_concepts:
                if concept in bs:
                    shares_val = bs[concept].get('value')
                    break
            
            if shares_val:
                found_data.append({
                    'end': pd.to_datetime(end_date),
                    'val': float(shares_val),
                    'filed': pd.to_datetime(filing_date),
                    'form': form
                })
        
        if not found_data:
            return None
            
        df = pd.DataFrame(found_data)
        return df
        
    except Exception as e:
        logger.error(f"Error fetching Polygon financials for {ticker}: {e}")
        return None

def get_share_history_combined(cik, ticker=None):
    """
    Fetches share history from SEC (XBRL), Polygon, and 8-K Parser.
    """
    dfs = []
    
    # 1. Try Polygon.io Financials if API key is present
    polygon_key = os.getenv('POLYGON_API_KEY')
    if polygon_key and ticker:
        poly_df = get_shares_history_polygon(ticker, polygon_key)
        if poly_df is not None and not poly_df.empty:
            dfs.append(poly_df)
    
    # 2. Get SEC XBRL data (10-K, 10-Q)
    sec_df = get_share_history(cik)
    if sec_df is not None and not sec_df.empty:
        dfs.append(sec_df)
    
    # 3. Get SEC 8-K Parsed data
    k8_df = get_8k_share_data(cik)
    if k8_df is not None and not k8_df.empty:
        dfs.append(k8_df)
    
    if not dfs:
        return None
        
    # Combine
    combined = pd.concat(dfs, ignore_index=True)
    
    # Standardization
    if 'filed' not in combined.columns:
        combined['filed'] = combined['end'] # Fallback
    
    # Sort
    combined = combined.sort_values(by=['end', 'filed'])
    
    # Deduplicate
    combined = combined.drop_duplicates(subset=['end'], keep='last')
    
    return combined


def get_financial_data(cik):
    """
    Fetches financial metrics (SBC, Buybacks, Dividends, Repurchased Shares) for a CIK.
    Returns a DataFrame with columns: date, metric, value
    """
    cik_padded = str(cik).zfill(10)
    
    # Structure: (taxonomy, concept_name, metric_label, units_key)
    # units_key is usually 'USD' or 'shares'
    concepts_map = [
        # Stock Based Compensation (SBC)
        ("us-gaap", "ShareBasedCompensation", "SBC", "USD"),
        ("us-gaap", "AllocatedShareBasedCompensationExpense", "SBC", "USD"), # fallback
        
        # Buybacks ($ Amount) - Cash Flow
        ("us-gaap", "PaymentsForRepurchaseOfCommonStock", "Buybacks", "USD"),
        ("us-gaap", "PaymentsForRepurchaseOfEquity", "Buybacks", "USD"), # fallback
        
        # Repurchased Shares (Count) - Equity
        ("us-gaap", "StockRepurchasedDuringPeriodShares", "RepurchasedShares", "shares"),
        ("us-gaap", "TreasuryStockSharesAcquired", "RepurchasedShares", "shares"), # fallback
        
        # Dividends ($ Amount)
        ("us-gaap", "PaymentsOfDividends", "Dividends", "USD"),
        ("us-gaap", "PaymentsOfDividendsCommonStock", "Dividends", "USD"), # fallback

        # Stock Issuance (ATM, Shelf, Offerings) - Cash Flow Inflow
        ("us-gaap", "ProceedsFromIssuanceOfCommonStock", "Issuance", "USD"),
        ("us-gaap", "ProceedsFromIssuanceOfEquity", "Issuance", "USD"), # fallback
        ("us-gaap", "ProceedsFromStockOptionsExercised", "Issuance", "USD"), # Options exercise
        ("us-gaap", "ProceedsFromWarrantExercises", "Issuance", "USD"), # Warrants
        ("us-gaap", "ProceedsFromIssuanceOfCommonStockAndWarrants", "Issuance", "USD"), # Combo
        ("us-gaap", "ProceedsFromIssuanceOfPrivatePlacement", "Issuance", "USD") # Private placements
    ]
    
    all_data = []
    
    # We need to ensure we don't duplicate metrics for the same date/period.
    # We will prioritize the first match for each metric label per period.
    
    for taxonomy, concept, label, unit_type in concepts_map:
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik_padded}/{taxonomy}/{concept}.json"
        
        try:
            time.sleep(0.1) # Rate limit kindness
            response = requests.get(url, headers=HEADERS)
            if response.status_code != 200:
                continue
                
            data = response.json()
            if "units" not in data or unit_type not in data["units"]:
                continue
                
            raw_points = data["units"][unit_type]
            
            for point in raw_points:
                # We typically want "for period" data.
                # 'start' and 'end' define the period.
                # We will use 'end' as the reference date.
                if 'end' not in point or 'val' not in point:
                    continue
                
                # Filter for Quarterly or Annual data if possible?
                # Actually, capturing all available data points is okay, we can filter in UI or Logic.
                # But 'frame' helps identify Q1, Q2, etc.
                # If we just dump everything, we might get YTD and QTD mixed.
                # Let's try to prioritize: if 'frame' exists (e.g. CY2023Q1), it's good.
                # If no frame, check duration.
                
                # Simplification: Store everything, let analysis layer handle it?
                # No, too messy. Let's try to infer from 'frame'.
                # Frame usually looks like 'CY2023Q1' or 'CY2023'.
                # If frame is missing, it might be a fiscal year variant.
                
                frame = point.get('frame', '')
                
                # Check duration roughly to distinguish Quarter vs Year vs YTD
                start = pd.to_datetime(point.get('start', point['end'])) # Default to end if instant (shouldn't be for these flows)
                end = pd.to_datetime(point['end'])
                duration_days = (end - start).days
                
                # Classification
                period_type = "Unknown"
                if 80 <= duration_days <= 100: period_type = "Quarter"
                elif 350 <= duration_days <= 370: period_type = "Year"
                
                # We only want Quarter or Year data for clean charts. 
                # (YTD Q3 is not useful directly unless we subtract, which is hard here).
                # So we filter for explicit Quarters or Years.
                
                if period_type == "Unknown" and not (len(frame) > 2 and 'Q' in frame): 
                    # If frame has Q (e.g. CY2023Q3I - instant?), but these are flows.
                    continue
                    
                all_data.append({
                    'date': end,
                    'start_date': start,
                    'metric': label,
                    'value': point['val'],
                    'frame': frame,
                    'period_type': period_type,
                    'concept': concept
                })
                
        except Exception as e:
            logger.error(f"Error fetching financial {concept} for CIK {cik}: {e}")
            continue

    if not all_data:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_data)
    
    # Deduplication logic:
    # For same date and metric, prioritize "Quarter" over "Year" if we want granularity?
    # No, we want both. But we don't want duplicate concepts (e.g. PaymentsForRepurchase vs PaymentsForRepurchaseEquity).
    # Concepts were ordered by priority in the list.
    
    # Sort by date, then by concept preference (implied by insertion order? No, we need explicit rank)
    # Let's just drop duplicates on [date, metric, period_type] keeping first (which is higher priority concept)
    df = df.drop_duplicates(subset=['date', 'metric', 'period_type'], keep='first')
    
    return df

def get_price_history_finnhub(ticker, api_key):
    """
    Fetches historical stock prices using Finnhub.
    Returns a DataFrame with columns: date, price.
    """
    # Using the stock candles endpoint
    # https://finnhub.io/docs/api/stock-candles
    url = "https://finnhub.io/api/v1/stock/candle"
    
    # Calculate timestamps for "max" history roughly (e.g. 25 years)
    # Finnhub free tier might limit this, but let's try
    end_ts = int(time.time())
    start_ts = int((datetime.now() - timedelta(days=365*20)).timestamp())
    
    params = {
        'symbol': ticker,
        'resolution': 'D', # Daily
        'from': start_ts,
        'to': end_ts,
        'token': api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Finnhub request failed: {response.status_code}")
            return pd.DataFrame()
            
        data = response.json()
        
        if data.get('s') != 'ok':
            logger.warning(f"Finnhub returned no data for {ticker}: {data.get('s')}")
            return pd.DataFrame()
            
        # Parse data
        # c: close prices, t: timestamps
        df = pd.DataFrame({
            'date': pd.to_datetime(data['t'], unit='s'),
            'price': data['c']
        })
        
        # Remove timezone info (Finnhub usually returns UTC timestamps)
        # df['date'] = df['date'].dt.tz_localize(None) # Already naive if unit='s' usually, but let's ensure
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching prices from Finnhub for {ticker}: {e}")
        return pd.DataFrame()


import html

from bs4 import BeautifulSoup

def extract_dollar_amount(text):
    """
    Extracts dollar amounts from text snippets.
    Returns (amount_in_dollars, amount_text) tuple.
    amount_in_dollars is float or None, amount_text is the matched string.
    
    Examples:
    - "$500 million" -> (500000000.0, "$500 million")
    - "$1.5 billion" -> (1500000000.0, "$1.5 billion")
    - "$100M" -> (100000000.0, "$100M")
    """
    if not text:
        return None, None
    
    # Patterns for dollar amounts
    # Match: $X.X million/billion or $XM/$XB
    patterns = [
        r'\$\s*(\d+(?:\.\d+)?)\s*(million|billion|M|B)',
        r'\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)',
        r'(\d+(?:\.\d+)?)\s*million',
        r'(\d+(?:\.\d+)?)\s*billion'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                # Extract number and unit
                num_str = match.group(1).replace(',', '')
                num = float(num_str)
                
                # Get unit if exists (group 2)
                if len(match.groups()) > 1:
                    unit = match.group(2).lower()
                    
                    # Convert to dollars
                    if unit in ['million', 'm']:
                        amount = num * 1_000_000
                    elif unit in ['billion', 'b']:
                        amount = num * 1_000_000_000
                    else:
                        amount = num
                else:
                    # No unit, assume it's already in dollars (unlikely from this pattern)
                    amount = num
                
                # Return amount and the matched text
                amount_text = match.group(0)
                return amount, amount_text
            except (ValueError, IndexError):
                continue
    
    return None, None

def get_offering_text(cik):
    """
    Scrapes recent filings for text related to Convertible Notes, ATM Offerings, and Shelf Offerings.
    Uses BeautifulSoup to intelligently parse tables and full text.
    """
    cik = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    
    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            return None
            
        data = response.json()
        filings = data['filings']['recent']
        df_filings = pd.DataFrame(filings)
        
        # Expanded relevant forms
        relevant_forms = ['10-K', '10-Q', '8-K', 'S-3', '424B', 'S-1', 'S-8', '424B5', '424B3', '424B2']
        pattern = '|'.join(relevant_forms)
        df_relevant = df_filings[df_filings['form'].astype(str).str.contains(pattern, na=False, case=False)]
        
        # Search depth: 25 filings (slight reduction for performance with BS4)
        df_relevant = df_relevant.head(25)
        
        found_data = []
        
        keywords = {
            'ATM Offering': [
                r"at-the-market", r"sales agreement", r"equity distribution", 
                r"ATM program", r"controlled equity", r"open market sale"
            ],
            'Convertible Note': [
                r"convertible senior notes?", r"convertible notes? due 20", 
                r"convertible debt", r"exchangeable notes?", 
                r"convertible senior subordinated", r"conversion price"
            ],
            'Shelf Offering': [
                r"shelf registration", r"base prospectus", 
                r"automatic shelf", r"shelf offering",
                r"registration statement on form s-3"
            ]
        }
        
        for _, row in df_relevant.iterrows():
            acc_no = row['accessionNumber'].replace('-', '')
            doc_name = row['primaryDocument']
            if not doc_name or doc_name.endswith('.xml'): continue 
            
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/{doc_name}"
            
            try:
                time.sleep(0.1)
                r = requests.get(doc_url, headers=HEADERS, timeout=10)
                if r.status_code != 200: continue
                
                soup = BeautifulSoup(r.text, 'html.parser')
                
                # Check Tables first
                table_snippets = []
                for table in soup.find_all('table'):
                    table_text = table.get_text(" ", strip=True)
                    table_text = html.unescape(table_text)
                    
                    # Search keywords in table
                    for category, patterns in keywords.items():
                        for pat in patterns:
                            if re.search(pat, table_text, re.IGNORECASE):
                                # If match, get a reasonable chunk of the table or surrounding text
                                snippet = table_text[:800] # Grab first 800 chars of table
                                if len(table_text) > 800: snippet += "..."
                                table_snippets.append((category, snippet, 30)) # High initial score for tables
                                break # One match per table per category
                
                # Full Text Search (similar to before but using soup.get_text for cleaner content)
                full_text = soup.get_text(" ", strip=True)
                full_text = html.unescape(full_text)
                full_text = ' '.join(full_text.split())
                
                for category, patterns in keywords.items():
                    best_snippet = None
                    best_score = -1
                    
                    # Combine table snippets for this category
                    for cat, snip, score in table_snippets:
                        if cat == category:
                            if score > best_score:
                                best_score = score
                                best_snippet = snip

                    # Search in full text
                    for pat in patterns:
                        matches = re.finditer(f"(.{{0,400}}{pat}.{{0,400}})", full_text, re.IGNORECASE)
                        for m in matches:
                            snippet = m.group(1).strip()
                            score = 0
                            
                            # Scoring
                            if '$' in snippet or 'million' in snippet or 'billion' in snippet: score += 15
                            if '%' in snippet or 'interest' in snippet.lower(): score += 5
                            if 'maturity' in snippet.lower() or 'due 20' in snippet.lower(): score += 15
                            if 'aggregate principal amount' in snippet.lower(): score += 20
                            if 'convertible' in snippet.lower(): score += 5
                            
                            # Penalties
                            if 'table of contents' in snippet.lower(): score -= 50
                            digit_count = sum(c.isdigit() for c in snippet)
                            if digit_count > 40 and len(snippet) < 300: score -= 20
                            
                            if score > best_score:
                                best_score = score
                                best_snippet = snippet
                    
                    if best_snippet and best_score > 5:
                        # Extract dollar amount
                        amount, amount_text = extract_dollar_amount(best_snippet)
                        
                        # Clean and add
                        if not best_snippet.startswith("..."): best_snippet = "..." + best_snippet
                        if not best_snippet.endswith("..."): best_snippet += "..."
                        
                        found_data.append({
                            'date': pd.to_datetime(row['filingDate']),
                            'category': category,
                            'text': best_snippet,
                            'source_filing': row['form'],
                            'amount': amount,
                            'amount_text': amount_text
                        })
                            
            except Exception as e:
                logger.warning(f"Error parsing doc {doc_url}: {e}")
                
        if not found_data:
            return pd.DataFrame()
            
        df_found = pd.DataFrame(found_data)
        # Drop duplicates based on date/category/source if text is very similar
        df_found = df_found.drop_duplicates(subset=['date', 'category', 'source_filing'])
        return df_found
        
    except Exception as e:
        logger.error(f"Error fetching offering text for CIK {cik}: {e}")
        return pd.DataFrame()

def get_ticker_details_polygon(ticker, api_key):
    """
    Fetches current ticker details (market cap, name, shares) from Polygon.io.
    """
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
    params = {'apiKey': api_key}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get('results', {})
        return None
    except Exception as e:
        logger.warning(f"Polygon ticker details failed: {e}")
        return None

def get_current_price(ticker):
    """
    Fetches real-time/delayed quote for a ticker.
    Prioritizes Finnhub, then Polygon.io, then yfinance.
    Returns float or None.
    """
    if not ticker:
        return None

    # 1. Check Finnhub first
    finnhub_key = os.getenv('FINNHUB_API_KEY')
    if finnhub_key and finnhub_key != "YOUR_API_KEY_HERE":
        try:
            url = "https://finnhub.io/api/v1/quote"
            params = {'symbol': ticker, 'token': finnhub_key}
            r = requests.get(url, params=params, timeout=5)
            if r.status_code == 200:
                data = r.json()
                # c is current price
                if 'c' in data and data['c'] != 0:
                    return float(data['c'])
        except Exception as e:
            logger.warning(f"Finnhub quote failed: {e}")

    # 2. Check Polygon.io
    polygon_key = os.getenv('POLYGON_API_KEY')
    if polygon_key and polygon_key != "YOUR_POLYGON_KEY_HERE":
        try:
            url = f"https://api.polygon.io/v2/last/trade/{ticker}"
            params = {'apiKey': polygon_key}
            r = requests.get(url, params=params, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if 'results' in data and 'p' in data['results']:
                    return float(data['results']['p'])
        except Exception as e:
            logger.warning(f"Polygon quote failed: {e}")
            
    # Fallback to YF
    try:
        t = yf.Ticker(ticker)
        # Try fast_info first (faster, no scraping usually)
        if hasattr(t, 'fast_info') and 'last_price' in t.fast_info:
             price = t.fast_info['last_price']
             if price: return float(price)
             
        # Try info
        info = t.info
        return info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
    except Exception as e:
        logger.warning(f"YF quote failed: {e}")
        return None

def get_price_history_polygon(ticker, api_key):
    """
    Fetches historical stock prices using Polygon.io Aggregates API.
    Returns a DataFrame with columns: date, price.
    """
    if not ticker:
        return pd.DataFrame()

    # Calculate dates for 5 years
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=365*5)).strftime('%Y-%m-%d')
    
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
    params = {
        'adjusted': 'true',
        'sort': 'asc',
        'limit': 50000,
        'apiKey': api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            logger.warning(f"Polygon request failed: {response.status_code}")
            return pd.DataFrame()
            
        data = response.json()
        if not data.get('results'):
            logger.warning(f"Polygon returned no data for {ticker}")
            return pd.DataFrame()
            
        # Parse results: 'c' is close, 't' is timestamp (ms)
        df = pd.DataFrame([
            {'date': pd.to_datetime(r['t'], unit='ms'), 'price': r['c']}
            for r in data['results']
        ])
        
        # Ensure date is naive to match other periods
        df['date'] = df['date'].dt.tz_localize(None)
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching prices from Polygon for {ticker}: {e}")
        return pd.DataFrame()

def get_price_history(ticker):
    """
    Fetches historical stock prices.
    Prioritizes Finnhub if API key is set, then Polygon.io, then yfinance.
    Returns a DataFrame with columns: date, price.
    """
    # 1. Check for Finnhub API Key
    finnhub_key = os.getenv('FINNHUB_API_KEY')
    if finnhub_key and finnhub_key != "YOUR_API_KEY_HERE":
        logger.info(f"Using Finnhub for {ticker} prices...")
        df = get_price_history_finnhub(ticker, finnhub_key)
        if not df.empty:
            return df
        else:
            logger.warning(f"Finnhub failed for {ticker}, falling back...")

    # 2. Check for Polygon.io API Key
    polygon_key = os.getenv('POLYGON_API_KEY')
    if polygon_key and polygon_key != "YOUR_POLYGON_KEY_HERE":
        logger.info(f"Using Polygon.io for {ticker} prices...")
        df = get_price_history_polygon(ticker, polygon_key)
        if not df.empty:
            return df
        else:
            logger.warning(f"Polygon.io failed for {ticker}, falling back to Yahoo Finance")
    
    # 3. Fallback to Yahoo Finance
    try:
        # Fetch max history
        t = yf.Ticker(ticker)
        hist = t.history(period="max", auto_adjust=True)
        
        if hist.empty:
            logger.warning(f"No price history found for {ticker}")
            return pd.DataFrame()
        
        hist = hist.reset_index()
        hist['Date'] = pd.to_datetime(hist['Date'])
        df = hist[['Date', 'Close']].copy()
        df.columns = ['date', 'price']
        df['date'] = df['date'].dt.tz_localize(None)
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching prices for {ticker}: {e}")
        return pd.DataFrame()

def calculate_changes(df):
    """
    Calculates percentage changes for 1Q, 1Y, 3Y, 5Y.
    Returns a dict of stats.
    """
    if df is None or df.empty:
        return {}
    
    # Ensure sorted by date
    df = df.sort_values(by='end')
    
    latest_row = df.iloc[-1]
    latest_date = latest_row['end']
    # Ensure standard Python integer/float, not numpy type
    try:
        latest_val = float(latest_row['val']) 
    except:
        latest_val = None
    
    # Define all years to calculate: 1 to 10
    # We will compute change_pct_Xy and date_Xy for X in [1..10]
    # Keep 1q separate as it's months based (3 months)
    
    stats = {
        'latest_shares': latest_val,
        'last_date': latest_date.strftime('%Y-%m-%d'),
        'prev_shares_1y': None, # Special legacy field
        'change_pct_1q': None,
        'date_1q': None
    }
    
    # Initialize dictionary keys for all years 1-10
    for y in range(1, 11):
        stats[f'change_pct_{y}y'] = None
        stats[f'date_{y}y'] = None

    def get_val_at_offset(months):
        target_date = latest_date - timedelta(days=30*months)
        # Find the row with date closest to target_date, but not after it (ideally)
        # or just closest absolute difference
        
        # Filter for dates <= target_date + buffer (e.g. 15 days) to find closest past date
        # If we just take nearest, we might take a future date relative to target 
        # (which is still in the past relative to now).
        # Simplest: absolute difference
        
        # Make a copy to avoid SettingWithCopy warning
        df_copy = df.copy()
        df_copy['diff'] = (df_copy['end'] - target_date).abs()
        
        # Get closest match
        closest = df_copy.sort_values('diff').iloc[0]
        
        # If the closest date is too far off (e.g. > 3 months gap), it's not valid comparison
        if closest['diff'].days > 90: 
            return None, None
            
        return closest['val'], closest['end']

    # 1 Quarter (~3 months)
    val_1q, date_1q = get_val_at_offset(3)
    if val_1q and val_1q != 0:
        stats['change_pct_1q'] = ((latest_val - val_1q) / val_1q) * 100
        stats['date_1q'] = date_1q.strftime('%Y-%m-%d')
        
    # Calculate for Years 1 to 10
    for y in range(1, 11):
        val_y, date_y = get_val_at_offset(y * 12)
        if val_y and val_y != 0:
            stats[f'change_pct_{y}y'] = ((latest_val - val_y) / val_y) * 100
            stats[f'date_{y}y'] = date_y.strftime('%Y-%m-%d')
            
            # Special legacy field
            if y == 1:
                stats['prev_shares_1y'] = val_y

    return stats
