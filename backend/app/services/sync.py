import logging
import pandas as pd
from sqlalchemy.future import select
from datetime import datetime, timedelta
import os
import json
import asyncio
from .. import database, models
from . import securities

logger = logging.getLogger(__name__)

async def is_ticker_tracked(cik: str) -> bool:
    """Check if any user still has this ticker in their portfolio."""
    async with database.AsyncSessionLocal() as session:
        stmt = select(models.PortfolioItem).where(models.PortfolioItem.cik == cik)
        result = await session.execute(stmt)
        return result.scalars().first() is not None

async def sync_company_stats(ticker: str, cik: str = None):
    """
    Orchestrates the fetching and saving of all data for a company.
    Uses MASSIVE PARALLELISM - fetches all external data concurrently.
    """
    async with database.AsyncSessionLocal() as session:
        try:
            # If CIK is missing, fetch it first
            if not cik:
                cik = await securities.get_cik_helper(ticker)
                if not cik:
                    logger.warning(f"CIK not found for {ticker}")
                    return False

            logger.info(f"Syncing stats data for {ticker} ({cik})")
            
            # PARALLEL FETCH: Get all external data at once
            api_key = os.getenv("POLYGON_API_KEY")
            
            # Launch all fetches in parallel
            details_task = securities.get_ticker_details_polygon(ticker, api_key)
            history_task = securities.get_share_history_combined(cik, ticker)
            prices_task = securities.get_price_history(ticker)
            
            # Wait for all to complete
            details, df, prices_df = await asyncio.gather(
                details_task, 
                history_task, 
                prices_task,
                return_exceptions=True
            )
            
            # Handle exceptions gracefully
            if isinstance(details, Exception):
                logger.warning(f"Details fetch failed for {ticker}: {details}")
                details = None
            if isinstance(df, Exception):
                logger.warning(f"History fetch failed for {ticker}: {df}")
                df = None
            if isinstance(prices_df, Exception):
                logger.warning(f"Prices fetch failed for {ticker}: {prices_df}")
                prices_df = pd.DataFrame()
            
            # Resolve the company name. Prefer Polygon's nicely-cased name when
            # available, then fall back to the SEC ticker cache (authoritative,
            # always present), and only as a last resort to the ticker itself.
            # Previously a missing/failed Polygon lookup fell straight to the
            # ticker, leaving thousands of companies with title == ticker.
            polygon_name = details.get('name') if details else None
            sec_title = await securities.get_company_title(ticker, cik)
            title = polygon_name or sec_title or ticker

            # Upsert Company
            stmt = select(models.Company).where(models.Company.cik == cik)
            result = await session.execute(stmt)
            company = result.scalars().first()
            if not company:
                company = models.Company(cik=cik, ticker=ticker, title=title)
                session.add(company)
            else:
                # Don't let a worse value overwrite a good stored name: if the new
                # title is just the ticker but we already have a real name, keep it.
                if title and title.upper() != str(ticker).upper():
                    company.title = title
                elif not company.title or company.title.upper() == str(ticker).upper():
                    company.title = title
            
            # Process History
            if df is not None and not df.empty:
                await session.execute(models.History.__table__.delete().where(models.History.cik == cik))
                
                history_objs = []
                for _, row in df.iterrows():
                    # Handle NaN values properly (YFinance doesn't have form/source_url)
                    source = row.get('form', 'Unknown')
                    if pd.isna(source): source = 'YFinance'
                    
                    source_url = row.get('source_url')
                    if pd.isna(source_url): source_url = None
                    
                    history_objs.append(models.History(
                        cik=cik,
                        date=row['end'].strftime('%Y-%m-%d'),
                        shares=int(row['val']) if row['val'] and not pd.isna(row['val']) else 0,
                        source=source,
                        source_url=source_url
                    ))
                session.add_all(history_objs)
               
                # Calculate Stats
                stats_dict = securities.calculate_changes(df)
                
                # Upsert Stats
                stmt = select(models.Stats).where(models.Stats.cik == cik)
                res = await session.execute(stmt)
                stats_obj = res.scalars().first()
                
                if not stats_obj:
                    stats_obj = models.Stats(cik=cik)
                    session.add(stats_obj)
                    
                stats_obj.latest_shares = stats_dict.get('latest_shares')
                stats_obj.change_pct_1q = stats_dict.get('change_pct_1q')
                stats_obj.last_updated = datetime.utcnow()
                stats_obj.last_date = stats_dict.get('last_date')
                
                for y in range(1, 11):
                    setattr(stats_obj, f'change_pct_{y}y', stats_dict.get(f'change_pct_{y}y'))
                    setattr(stats_obj, f'date_{y}y', stats_dict.get(f'date_{y}y'))
                    
                stats_obj.prev_shares_1y = stats_dict.get('prev_shares_1y')
                await session.commit() # Important: Commit stats here

            # Process Prices
            if prices_df is not None and not prices_df.empty:
                await session.execute(models.Price.__table__.delete().where(models.Price.cik == cik))
                price_objs = []
                for _, row in prices_df.iterrows():
                     price_objs.append(models.Price(
                         cik=cik,
                         date=row['date'].strftime('%Y-%m-%dT%H:%M:%S'),
                         price=row['price']
                     ))
                session.add_all(price_objs)

            await session.commit()
            logger.info(f"Basic sync complete for {ticker}")
            return True
        except Exception as e:
            logger.error(f"Basic sync failed for {ticker}: {e}")
            await session.rollback()
            return False


OFFERINGS_CACHE_FILE = "offerings_cache.json"


async def sync_company_data(ticker: str, cik: str = None):
    """
    Backward-compatible entrypoint used by routers/scheduler.
    """
    return await sync_company_stats(ticker=ticker, cik=cik)

def is_offering_cache_fresh(ticker):
    try:
        if not os.path.exists(OFFERINGS_CACHE_FILE): return False
        with open(OFFERINGS_CACHE_FILE, 'r') as f:
            cache = json.load(f)
        
        last_sync = cache.get(ticker)
        if not last_sync: return False
        
        last_sync_dt = datetime.fromisoformat(last_sync)
        if datetime.now() - last_sync_dt < timedelta(hours=24):
            return True
    except: return False
    return False

def update_offering_cache_timestamp(ticker):
    try:
        cache = {}
        if os.path.exists(OFFERINGS_CACHE_FILE):
             with open(OFFERINGS_CACHE_FILE, 'r') as f:
                cache = json.load(f)
        
        cache[ticker] = datetime.now().isoformat()
        
        with open(OFFERINGS_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception as e:
        logger.warning(f"Failed to update offering cache timestamp: {e}")


# Limit concurrent background syncs to avoid CPU/IO saturation
_sync_semaphore = asyncio.Semaphore(3)
_syncing_tickers = set()

async def sync_company_offerings(ticker: str, cik: str, force: bool = False, deep_scan: bool = False, limit: int = None):
    """
    Background task to perform the slow offering text scraping.
    If deep_scan is True, scans up to 50 filings (standard is 30).
    """
    # 1. Check Cache Freshness (Skip if < 24h, unless forced or deep_scan)
    if not force and not deep_scan and not limit and is_offering_cache_fresh(ticker):
        logger.info(f"Skipping offering sync for {ticker} (Cache valid < 24h)")
        return

    # Prevent multiple concurrent syncs for the same ticker
    if ticker in _syncing_tickers:
        logger.info(f"Sync already in progress for {ticker}. Skipping trigger.")
        return
        
    _syncing_tickers.add(ticker)
    semaphore_acquired = False

    try:
        await _sync_semaphore.acquire()
        semaphore_acquired = True
        logger.info(f"Starting offering sync for {ticker} (force={force})...")
        
        # 4. Get Offering Text (Slow phase - NO DB CONNECTION HELD)
        try:
            # CHECK: Halt if no longer tracked
            if not await is_ticker_tracked(cik):
                logger.info(f"🛑 Halting sync for {ticker}: No longer tracked in any portfolio.")
                return

            # Determine limit
            if limit: fetch_limit = limit
            elif deep_scan: fetch_limit = 50
            else: fetch_limit = 30
            
            ignore_filings = set()
            # --- PROCESS DEDUPLICATION OPTIMIZATION ---
            # Skip already-processed filings only in non-force mode.
            # Force mode should re-scan known filings to backfill missed/incorrect amounts.
            if not force:
                async with database.AsyncSessionLocal() as session:
                    try:
                        stmt = select(models.Offering.source_filing, models.Offering.source_url).where(models.Offering.cik == cik)
                        res = await session.execute(stmt)
                        rows = res.all()
                        for r in rows:
                            if r[0]:
                                ignore_filings.add(r[0])
                            if r[1]:
                                ignore_filings.add(r[1])
                        
                        if ignore_filings:
                            logger.info(f"Checking {len(ignore_filings)} known filings for {ticker} to avoid re-processing.")
                    except Exception as e:
                        logger.warning(f"Failed to fetch ignore list for {ticker}: {e}")
            else:
                logger.info(f"Force mode for {ticker}: reprocessing known filings to refresh extraction.")
            # ------------------------------------------

            # Define incremental save callback
            async def save_batch(batch_df):
                async with database.AsyncSessionLocal() as session:
                    try:
                        if batch_df is not None and not batch_df.empty:
                            # Fetch existing offerings (scoped to CIK)
                            stmt = select(models.Offering).where(models.Offering.cik == cik)
                            result = await session.execute(stmt)
                            existing_offerings = result.scalars().all()
                            
                            existing_map = {
                                (o.date, o.category, (o.source_url or o.source_filing)): o
                                for o in existing_offerings
                            }
                            
                            new_offerings = []
                            updated_count = 0
                            
                            for _, row in batch_df.iterrows():
                                date_str = row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date'])
                                status = row.get('status')
                                
                                # Fallback: Infer status from category if missing
                                if not status and row['category']:
                                    cat_lower = str(row['category']).lower()
                                    if any(x in cat_lower for x in ['atm', 'series', 'direct', 'sbc', 'employee', 'compensation', 'stock-based']):
                                        status = 'Active'
                                    elif any(x in cat_lower for x in ['shelf', 'convertible', 'warrant', 'safe']):
                                        status = 'Potential'
                                    else:
                                        status = 'Potential'

                                description = row.get('description')
                                amount = row.get('amount')
                                if pd.isna(amount): amount = None
                                
                                key = (date_str, row['category'], (row.get('source_url') or row['source_filing']))
                                
                                if key in existing_map:
                                    existing_obj = existing_map[key]
                                    needs_update = False
                                    
                                    if status and existing_obj.status != status:
                                        existing_obj.status = status
                                        needs_update = True
                                    if description and existing_obj.description != description:
                                        existing_obj.description = description
                                        needs_update = True
                                    
                                    new_amt = None
                                    if amount is not None:
                                        try:
                                            clean_amt = str(amount).replace('$', '').replace(',', '')
                                            new_amt = float(clean_amt)
                                        except: new_amt = None
        
                                    if new_amt is not None:
                                        if existing_obj.amount is None or abs((existing_obj.amount or 0) - new_amt) > 1:
                                            existing_obj.amount = new_amt
                                            if row.get('amount_text'):
                                                existing_obj.amount_text = row['amount_text']
                                            needs_update = True
                                        
                                    if needs_update:
                                        updated_count += 1
                                else:
                                    try:
                                        clean_amt = str(amount).replace('$', '').replace(',', '') if amount else None
                                        final_amount = float(clean_amt) if clean_amt else None
                                    except: final_amount = None
        
                                    new_obj = models.Offering(
                                        cik=cik,
                                        date=date_str,
                                        category=row['category'],
                                        text=row['text'],
                                        source_filing=row['source_filing'],
                                        source_url=row['source_url'],
                                        amount=final_amount,
                                        amount_text=row['amount_text'],
                                        status=status,
                                        description=description
                                    )
                                    session.add(new_obj)
                                    new_offerings.append(new_obj)
                                    # CRITICAL FIX: Add to existing_map to prevent duplicates within the same batch
                                    existing_map[key] = new_obj
                            
                            if new_offerings or updated_count > 0:
                                await session.commit()
                                logger.info(f"INCREMENTAL Sync {ticker}: Added {len(new_offerings)}, Updated {updated_count}")
                    except Exception as e:
                        logger.error(f"Incremental save failed: {e}")

            logger.info(f"Fetching offering text for {ticker} (limit={fetch_limit})...")
            # PASS deep_scan to securities module with callback AND ignore_filings
            offering_df = await securities.get_offering_text(cik, limit=fetch_limit, periodic_callback=save_batch, ignore_filings=ignore_filings)
            
            count = len(offering_df) if offering_df is not None and not offering_df.empty else 0
            logger.info(f"✅ Found {count} potential offerings for {ticker}")
            
            # Print for scanner follow-along
            if deep_scan and count > 0:
                print(f"  [AI] Extracted {count} instruments for {ticker}...")
        except Exception as e:
            logger.error(f"Offering extraction failed for {ticker}: {e}")
            return

        events_for_summary = []
        
        # 5. Process and update database (Short-lived session)
        async with database.AsyncSessionLocal() as session:
            try:
                if offering_df is not None and not offering_df.empty:
                    # Fetch existing offerings to avoid duplicates/churn
                    stmt = select(models.Offering).where(models.Offering.cik == cik)
                    result = await session.execute(stmt)
                    existing_offerings = result.scalars().all()
                    
                    # Create lookup key: date + category + source_filing
                    existing_map = {
                        (o.date, o.category, (o.source_url or o.source_filing)): o
                        for o in existing_offerings
                    }
                    
                    new_offerings = []
                    updated_count = 0
                    for _, row in offering_df.iterrows():
                        date_str = row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date'])
                        
                        # AI Data Check
                        status = row.get('status')
                        description = row.get('description')
                        amount = row.get('amount')
                        if pd.isna(amount): amount = None
                        
                        # Fallback: Infer status from category if missing
                        if not status and row['category']:
                            cat_lower = str(row['category']).lower()
                            if any(x in cat_lower for x in ['atm', 'series', 'direct', 'sbc', 'employee', 'compensation', 'stock-based']):
                                status = 'Active'
                            elif any(x in cat_lower for x in ['shelf', 'convertible', 'warrant', 'safe']):
                                status = 'Potential'
                            else:
                                status = 'Potential' # Default safe fallback
                        
                        # Try to find existing record by (date, category, filing)
                        key = (date_str, row['category'], (row.get('source_url') or row['source_filing']))
                        
                        if key in existing_map:
                            existing_obj = existing_map[key]
                            needs_update = False
                            
                            # AI Data Priority: Update if null or if deep_scan is active
                            if status and existing_obj.status != status:
                                existing_obj.status = status
                                needs_update = True
                            if description and existing_obj.description != description:
                                existing_obj.description = description
                                needs_update = True
                            
                            # CRITICAL: Update amount if null or if significantly different
                            new_amt = None
                            if amount is not None:
                                try:
                                    # Handle both string "$50,000" and float inputs
                                    clean_amt = str(amount).replace('$', '').replace(',', '')
                                    new_amt = float(clean_amt)
                                except: new_amt = None

                            if new_amt is not None:
                                if existing_obj.amount is None or abs((existing_obj.amount or 0) - new_amt) > 1:
                                    existing_obj.amount = new_amt
                                    if row.get('amount_text'):
                                        existing_obj.amount_text = row['amount_text']
                                    needs_update = True
                                
                            if needs_update:
                                updated_count += 1
                        else:
                            # Add new
                            try:
                                clean_amt = str(amount).replace('$', '').replace(',', '') if amount else None
                                final_amount = float(clean_amt) if clean_amt else None
                            except: final_amount = None

                            new_obj = models.Offering(
                                cik=cik,
                                date=date_str,
                                category=row['category'],
                                text=row['text'],
                                source_filing=row['source_filing'],
                                source_url=row['source_url'],
                                amount=final_amount,
                                amount_text=row['amount_text'],
                                status=status,
                                description=description
                            )
                            session.add(new_obj)
                            new_offerings.append(new_obj)
                    
                    if new_offerings or updated_count > 0:
                        await session.commit()
                        logger.info(f"Sync for {ticker}: Added {len(new_offerings)}, Updated {updated_count} with AI data")
                    else:
                        logger.info(f"No new or updated data for {ticker}")

            except Exception as e:
                logger.error(f"Offering database save failed for {ticker}: {e}")
                await session.rollback()
                return

        # --- GENERATE SUMMARY (Decoupled Phase) ---
        await generate_dilution_summary_from_db(ticker, cik)

        update_offering_cache_timestamp(ticker)
        logger.info(f"Offering sync complete for {ticker}")
    finally:
        if semaphore_acquired:
            _sync_semaphore.release()
        if ticker in _syncing_tickers:
            _syncing_tickers.remove(ticker)



# --- Natural-language summary helpers --------------------------------------
# Prose phrasing for category labels (plural = aggregate clauses; singular = a
# single most-recent event). Keeps the summary reading like a sentence instead
# of a robotic "category (amount)" dump.
_PROSE_PLURAL = {
    "ATM Offering": "at-the-market (ATM) sales",
    "Series / Direct": "registered direct / series offerings",
    "Shelf Offering": "shelf registration capacity",
    "Unit Rights": "unit rights",
    "Warrants / SAFEs": "outstanding warrants",
    "Convertible Note": "convertible notes",
    "Common Stock": "common-stock issuances",
    "Other": "other dilution events",
    "SBC / Stock-Based Comp": "stock-based compensation",
    "IPO": "IPO proceeds",
    "Rights Offering": "rights offerings",
}
_PROSE_SINGULAR = {
    "ATM Offering": "ATM sale",
    "Series / Direct": "registered direct offering",
    "Shelf Offering": "shelf registration",
    "Unit Rights": "unit right",
    "Warrants / SAFEs": "warrant issuance",
    "Convertible Note": "convertible note",
    "Common Stock": "common-stock issuance",
    "Other": "other dilution event",
    "SBC / Stock-Based Comp": "stock-based compensation grant",
    "IPO": "initial public offering",
    "Rights Offering": "rights offering",
}


def _sum_usd(v) -> str:
    if v is None:
        return None
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def _sum_shares(n) -> str:
    if not n:
        return None
    if n >= 1e9:
        return f"{n / 1e9:.2f}B shares"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M shares"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K shares"
    return f"{n:,.0f} shares"


def _parse_shares_from_text(text) -> float:
    import re
    if not text:
        return 0.0
    m = re.search(r"([\d,]{4,})\s+shares", text)
    return float(m.group(1).replace(",", "")) if m else 0.0


def _join_clauses(parts: list) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def generate_deterministic_dilution_summary(ticker: str, recent_offerings: list) -> str:
    """
    Build a concise, natural-language dilution summary (no LLM) from the last
    ~2 years of Offering rows. Aggregates by instrument type — so three separate
    registered-direct takedowns collapse into one "$17.2M of registered direct
    offerings" clause — and splits active (already issued) from potential
    (warrants / shelf / convertibles that could dilute later).
    """
    from collections import defaultdict

    if not recent_offerings:
        return "No material dilution activity detected in the company's recent SEC filings."

    def aggregate(items):
        by_cat = defaultdict(lambda: {"count": 0, "usd": 0.0, "shares": 0.0})
        total_usd = 0.0
        for o in items:
            cat = o.category or "Other"
            d = by_cat[cat]
            d["count"] += 1
            if o.amount:
                d["usd"] += o.amount
                total_usd += o.amount
            else:
                d["shares"] += _parse_shares_from_text(o.amount_text)
        return by_cat, total_usd

    def clauses(by_cat, limit=3):
        out = []
        ranked = sorted(
            by_cat.items(),
            key=lambda kv: (kv[1]["usd"], kv[1]["shares"], kv[1]["count"]),
            reverse=True,
        )
        for cat, d in ranked[:limit]:
            label = _PROSE_PLURAL.get(cat, (cat or "other instruments").lower())
            if d["usd"] > 0:
                out.append(f"{_sum_usd(d['usd'])} of {label}")
            elif d["shares"] > 0:
                out.append(f"{_sum_shares(d['shares'])} of {label}")
            else:
                n = d["count"]
                out.append(f"{n} {label}")
        return out

    def most_recent_phrase(o):
        label = _PROSE_SINGULAR.get(o.category, (o.category or "offering").lower())
        amt = _sum_usd(o.amount) if o.amount else _sum_shares(_parse_shares_from_text(o.amount_text))
        if amt and amt.startswith("$"):
            return f"a {amt} {label}"
        if amt:
            return f"a {label} for {amt}"
        return f"a {label}"

    # Split by status (set reliably by the extractor); offerings arrive sorted desc.
    active = [o for o in recent_offerings if o.status == "Active"]
    potential = [o for o in recent_offerings if o.status == "Potential"]

    sentences = []

    if active:
        by_cat, total = aggregate(active)
        n = len(active)
        lead = f"Over the past two years, {ticker} completed {n} active dilution event{'s' if n != 1 else ''}"
        if total > 0:
            lead += f", raising roughly {_sum_usd(total)}"
        cl = _join_clauses(clauses(by_cat))
        if cl:
            lead += f" — primarily {cl}"
        lead += f". The most recent was {most_recent_phrase(active[0])} on {active[0].date}."
        sentences.append(lead)

    if potential:
        by_cat, _ = aggregate(potential)
        cl = _join_clauses(clauses(by_cat))
        s = f"On the potential side, {ticker} carries {cl}" if cl else f"{ticker} also has outstanding potential dilution"
        s += ", which could dilute existing shareholders if warrants are exercised, notes convert, or shelf capacity is drawn down."
        sentences.append(s)

    if not sentences:
        return "No material active or potential dilution identified in the company's recent filings."

    return " ".join(sentences)


async def generate_dilution_summary_from_db(
    ticker: str,
    cik: str,
    force: bool = False,
    require_tracked: bool = True,
):
    """
    Independent task to (re)generate the summary using deterministic logic.
    Replaces the previous LLM-based approach for speed and reliability.
    """
    # Interactive portfolio work should stop once a ticker is removed. Batch
    # cron jobs explicitly process the preload universe without requiring a
    # portfolio row, so they opt out of this guard.
    if require_tracked and not await is_ticker_tracked(cik):
        logger.info(f"🛑 Halting summary for {ticker}: No longer tracked in any portfolio.")
        return

    # 1. Check if summary is still fresh (ignore if force=True)
    async with database.AsyncSessionLocal() as session:
        stmt_check = select(models.Stats).where(models.Stats.cik == cik)
        res_check = await session.execute(stmt_check)
        stats = res_check.scalars().first()
        
        if not force and stats and stats.dilution_summary and stats.last_updated:
            if datetime.utcnow() - stats.last_updated < timedelta(hours=24):
                logger.info(f"Summary for {ticker} is still fresh (<24h). Skipping regeneration.")
                return

    logger.info(f"Generating deterministic summary for {ticker}...")
    
    async with database.AsyncSessionLocal() as session:
        try:
            # Fetch latest 2 years of events
            stmt_recent = select(models.Offering).where(
                models.Offering.cik == cik,
                models.Offering.date >= (datetime.now() - timedelta(days=365*2)).strftime('%Y-%m-%d')
            ).order_by(models.Offering.date.desc())
            
            res_recent = await session.execute(stmt_recent)
            recent_offerings = res_recent.scalars().all()
            
            # --- DETERMINISTIC SUMMARY GENERATION ---
            summary_text = generate_deterministic_dilution_summary(ticker, recent_offerings)
            # ----------------------------------------
        
            # SAVE RESULT (Short session)
            # We need a fresh session or reuse current depending on logic. reusing current is fine here.
            stmt_stats = select(models.Stats).where(models.Stats.cik == cik)
            res_stats = await session.execute(stmt_stats)
            stats_obj = res_stats.scalars().first()
            
            if stats_obj:
                stats_obj.dilution_summary = summary_text
                stats_obj.last_updated = datetime.utcnow()
            else:
                stats_obj = models.Stats(cik=cik, dilution_summary=summary_text, last_updated=datetime.utcnow())
                session.add(stats_obj)
            
            await session.commit()
            logger.info(f"Successfully updated deterministic summary for {ticker}")
                
        except Exception as ex:
            logger.warning(f"Failed to generate summary text for {ticker}: {ex}")
