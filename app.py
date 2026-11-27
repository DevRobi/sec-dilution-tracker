import streamlit as st
import logging
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import time
import os
from streamlit_option_menu import option_menu

# Local modules
import db_manager
import data_fetcher

import threading
from streamlit.runtime.scriptrunner import add_script_run_ctx

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set Finnhub API Key if not already present
if 'FINNHUB_API_KEY' not in os.environ:
    os.environ.setdefault('FINNHUB_API_KEY', '')  # set it in .env

if 'POLYGON_API_KEY' not in os.environ:
    os.environ.setdefault('POLYGON_API_KEY', '')  # set it in .env

st.set_page_config(page_title="Share Buyback & Dilution Tracker", layout="wide")

# Initialize DB on first run
if 'db_init' not in st.session_state:
    db_manager.init_db()
    st.session_state['db_init'] = True

# --- BACKGROUND SYNC STATE ---
if 'sync_queue' not in st.session_state:
    st.session_state['sync_queue'] = []
if 'sync_active' not in st.session_state:
    st.session_state['sync_active'] = False
if 'sync_progress' not in st.session_state:
    st.session_state['sync_progress'] = {"current": 0, "total": 0, "status": ""}

def background_worker():
    """Worker thread to process the sync queue."""
    while st.session_state['sync_queue']:
        st.session_state['sync_active'] = True
        ticker = st.session_state['sync_queue'].pop(0)
        
        st.session_state['sync_progress']['status'] = f"Updating {ticker}..."
        st.session_state['sync_progress']['current'] += 1
        
        try:
            # We don't want the background thread to use status_text or UI elements
            # Just do the data fetching and DB work
            update_data_for_ticker(ticker, silent=True)
        except Exception as e:
            logger.error(f"Background sync error for {ticker}: {e}")
            
        time.sleep(0.5) # Rate limit kindness
        
    st.session_state['sync_active'] = False
    st.session_state['sync_progress'] = {"current": 0, "total": 0, "status": "Sync Complete"}

def start_background_sync(tickers):
    if not st.session_state['sync_active']:
        unique_tickers = list(set(tickers))
        st.session_state['sync_queue'] = unique_tickers
        st.session_state['sync_progress'] = {"current": 0, "total": len(unique_tickers), "status": "Starting..."}
        
        thread = threading.Thread(target=background_worker)
        add_script_run_ctx(thread)
        thread.start()


# Authentication State
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False

# Custom CSS for Professional Terminal Design
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    .stApp {
        background-color: #0E0E10;
        color: #EDF1F5;
    }

    /* Minimal Navigation */
    .nav-container {
        border-bottom: 1px solid #1F1F22;
        padding: 0.5rem 1rem;
        background: #0E0E10;
    }
    
    /* Terminal Header */
    .hero-container {
        padding: 4rem 1rem;
        text-align: left;
        margin-bottom: 2rem;
        border-bottom: 1px solid #1F1F22;
    }
    
    .hero-title {
        font-size: 3rem;
        font-weight: 700;
        letter-spacing: -1.5px;
        color: #FFFFFF;
        margin-bottom: 0.5rem;
    }
    
    .hero-subtitle {
        font-size: 1.1rem;
        color: #8E9196;
        max-width: 600px;
        font-weight: 400;
    }
    
    /* Feature Grid */
    .feature-card {
        padding: 1.5rem;
        border: 1px solid #1F1F22;
        border-radius: 4px;
        background: transparent;
        transition: border-color 0.2s;
    }
    
    .feature-card:hover {
        border-color: #10B981; /* Emerald */
    }

    .feature-icon {
        font-size: 1.25rem;
        color: #10B981;
        margin-bottom: 1rem;
        display: block;
    }
    
    .feature-title {
        font-size: 1rem;
        font-weight: 600;
        color: #FFFFFF;
        margin-bottom: 0.5rem;
    }

    .feature-text {
        font-size: 0.9rem;
        color: #8E9196;
    }
    
    /* Buttons - Solid & Precise */
    .stButton>button {
        border-radius: 4px;
        font-weight: 500;
        height: 2.5rem;
        transition: all 0.1s;
    }
    
    .stButton>button[kind="primary"] {
        background-color: #10B981;
        color: #FFFFFF;
        border: none;
    }

    .stButton>button:hover {
        opacity: 0.9;
    }
    
    /* Global Overrides */
    [data-testid="stSidebar"] { display: none; }
    
    [data-testid="stHeader"] {
        background: #0E0E10;
        border-bottom: 1px solid #1F1F22;
    }

    [data-testid="stMetricValue"] {
        font-family: 'Inter', sans-serif;
        font-weight: 600;
        font-size: 1.5rem !important;
        color: #FFFFFF !important;
    }

    [data-testid="stMetricLabel"] {
        color: #8E9196 !important;
        font-size: 0.8rem !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    
    h1, h2, h3 { color: #FFFFFF; }

    /* Table & Dataframe */
    .stDataFrame {
        border: 1px solid #1F1F22;
        border-radius: 4px;
    }

    div[data-testid="stExpander"] {
        border: 1px solid #1F1F22;
        background: transparent;
        border-radius: 4px;
    }
    </style>
    """, unsafe_allow_html=True)

# Helper Functions
def format_percentage(val):
    if pd.isna(val):
        return "N/A"
    return f"{val:.2f}%"

def set_chart_options(fig):
    """
    Applies standard 6m, 1y, 2y, 5y range selectors to a Plotly figure.
    Sets default range to 2 years.
    """
    two_years_ago = pd.Timestamp.now() - pd.DateOffset(years=2)
    
    fig.update_xaxes(
        rangeselector=dict(
            buttons=list([
                dict(count=6, label="   6m   ", step="month", stepmode="backward"),
                dict(count=1, label="   1y   ", step="year", stepmode="backward"),
                dict(count=2, label="   2y   ", step="year", stepmode="backward"),
                dict(count=5, label="   5y   ", step="year", stepmode="backward")
            ]),
            font=dict(size=14, family="Inter, sans-serif", weight="bold"),  # Wider and bolder
            bgcolor="rgba(31, 31, 34, 0.8)",
            activecolor="#10B981"
        ),
        range=[two_years_ago, pd.Timestamp.now()], # Default to 2y
        rangeslider=dict(visible=False),
        type="date"
    )

def get_separate_charts(cik, ticker, show_shares, show_mcap, show_price):
    """
    Generates three separate Plotly figures for Shares Outstanding, Market Cap, and Share Price.
    Returns a dictionary of figures.
    """
    shares_df = db_manager.get_history(cik)
    prices_df = db_manager.get_prices(cik)
    
    charts = {}
    
    # Limit data to last 5 years max (User requested no 10y needed)
    five_years_ago = pd.Timestamp.now() - pd.DateOffset(years=5)
    
    # Common cleaning & Filtering
    if not shares_df.empty:
        shares_df['date'] = pd.to_datetime(shares_df['date'])
        shares_df = shares_df[shares_df['date'] >= five_years_ago].sort_values('date')
        
    if not prices_df.empty:
        prices_df['date'] = pd.to_datetime(prices_df['date'])
        prices_df = prices_df[prices_df['date'] >= five_years_ago].sort_values('date')

    # 1. Shares Outstanding (Bar Chart with Monthly Frequency)
    if show_shares and not shares_df.empty:
        # Filter for the 5y window
        shares_plot_df = shares_df[shares_df['date'] >= five_years_ago].copy()
        
        if not shares_plot_df.empty:
            # Monthly Resampling and Forward-Fill to avoid gaps
            # Create a monthly range from the first point to today
            start_date = shares_plot_df['date'].min().replace(day=1)
            end_date = pd.Timestamp.now().normalize()
            
            monthly_range = pd.date_range(start=start_date, end=end_date, freq='MS')
            monthly_df = pd.DataFrame({'date': monthly_range})
            
            # Merge and ffill
            shares_monthly = pd.merge(monthly_df, shares_plot_df[['date', 'shares']], on='date', how='left')
            # Also merge actual points to ensure they are included if not on Month Start
            shares_monthly = pd.concat([shares_monthly, shares_plot_df[['date', 'shares']]]).sort_values('date').drop_duplicates('date')
            shares_monthly['shares'] = shares_monthly['shares'].ffill()
            
            # Filter again for the 5y window to be sure, and only take monthly points for the bars
            # to keep it clean, but include the very latest point
            final_plot_df = shares_monthly[
                (shares_monthly['date'].dt.day == 1) | 
                (shares_monthly['date'] == shares_monthly['date'].max())
            ].copy()

            fig_shares = go.Figure()
            
            # Use go.Bar with a fixed width (approx 25 days)
            bar_width_ms = 25 * 24 * 60 * 60 * 1000
            
            fig_shares.add_trace(go.Bar(
                x=final_plot_df['date'],
                y=final_plot_df['shares'],
                name='Shares',
                marker_color='#1f77b4',
                width=bar_width_ms,
                opacity=0.8,
                hovertemplate="<b>Date</b>: %{x|%b %Y}<br><b>Shares</b>: %{y:,.0f}<extra></extra>"
            ))
            
            fig_shares.update_layout(
                title=f'{ticker} Shares Outstanding (Monthly)',
                xaxis_title='Date',
                yaxis_title='Shares',
                template="plotly_white",
                hovermode="x unified",
                bargap=0.1
            )
            
            set_chart_options(fig_shares)
            charts['shares'] = fig_shares

    # 2. Market Cap (Line Chart)
    if show_mcap and not prices_df.empty and not shares_df.empty:
        # Needs merge
        if 'date' in prices_df.columns:
             prices_indexed = prices_df.set_index('date').sort_index()
        else:
             prices_indexed = prices_df.copy()
             
        if 'date' in shares_df.columns:
             shares_indexed = shares_df.set_index('date').sort_index()
        else:
             shares_indexed = shares_df.copy()
        
        df = prices_indexed.join(shares_indexed['shares'], how='left')
        df['shares'] = df['shares'].ffill()
        df['market_cap'] = df['price'] * df['shares']
        df = df.dropna(subset=['market_cap'])
        
        if not df.empty:
            fig_mcap = px.line(
                df,
                x=df.index,
                y='market_cap',
                title=f'{ticker} Market Cap',
                labels={'index': 'Date', 'market_cap': 'Market Cap ($)'},
                template="plotly_white"
            )
            fig_mcap.update_traces(line_color='#2ca02c') # Green
            fig_mcap.update_layout(hovermode="x unified")
            set_chart_options(fig_mcap)
            charts['mcap'] = fig_mcap

    # 3. Share Price (Line Chart)
    if show_price and not prices_df.empty:
        fig_price = px.line(
            prices_df,
            x='date',
            y='price',
            title=f'{ticker} Share Price',
            labels={'date': 'Date', 'price': 'Price ($)'},
            template="plotly_white"
        )
        fig_price.update_traces(line_color='#ff7f0e') # Orange
        fig_price.update_layout(hovermode="x unified")
        set_chart_options(fig_price)
        charts['price'] = fig_price
    
    return charts

@st.cache_data(ttl=3600)
def get_cached_live_metrics(ticker):
    """
    Fetches live metrics from Polygon/Finnhub and caches them for 1 hour.
    Includes a circuit breaker for Finnhub 403 errors to avoid UI freezing.
    """
    # Initialize circuit breaker state in session
    if 'finnhub_disabled' not in st.session_state:
        st.session_state.finnhub_disabled = False

    polygon_key = os.getenv('POLYGON_API_KEY')
    poly_details = None
    if polygon_key and polygon_key != "YOUR_POLYGON_KEY_HERE":
        try:
            poly_details = data_fetcher.get_ticker_details_polygon(ticker, polygon_key)
        except Exception as e:
            logger.warning(f"Polygon details fetch failed for {ticker}: {e}")

    # Check for Finnhub circuit breaker
    price = None
    if not st.session_state.finnhub_disabled:
        try:
            # We wrap the internal call to detect 403
            finnhub_key = os.getenv('FINNHUB_API_KEY')
            if finnhub_key and finnhub_key != "YOUR_API_KEY_HERE":
                url = "https://finnhub.io/api/v1/quote"
                params = {'symbol': ticker, 'token': finnhub_key}
                r = requests.get(url, params=params, timeout=2) # Shorter timeout for UI
                if r.status_code == 403:
                    st.session_state.finnhub_disabled = True
                    logger.error("Finnhub returned 403. Disabling Finnhub for this session.")
                elif r.status_code == 200:
                    data = r.json()
                    if 'c' in data and data['c'] != 0:
                        price = float(data['c'])
        except Exception as e:
            logger.warning(f"Finnhub circuit breaker check failed: {e}")

    # Fallback to general price fetch (Polygon/YF) if Finnhub failed/was skipped
    if price is None:
        price = data_fetcher.get_current_price(ticker)
    
    # Use Polygon market cap if available, otherwise calculate
    poly_mcap = poly_details.get('market_cap') if poly_details else None
    poly_shares = poly_details.get('weighted_shares_outstanding') if poly_details else None
    
    return {
        "price": price,
        "poly_mcap": poly_mcap,
        "poly_shares": poly_shares
    }

# --- Live Metrics Helper ---
def _render_live_metrics(ticker, latest_shares, change_1q, change_1y, change_5y):
    """
    Renders live metrics for company search.
    """
    metrics = get_cached_live_metrics(ticker)
    price = metrics["price"]
    poly_mcap = metrics["poly_mcap"]
    poly_shares = metrics["poly_shares"]

    # Row 1: Live Data
    col1, col2, col3 = st.columns(3)
    
    # Display shares: Prefer Polygon's precise weighted shares if available
    shares_to_show = poly_shares if poly_shares else latest_shares
    col1.metric("Shares Outstanding", f"{shares_to_show:,.0f}" if pd.notna(shares_to_show) else "N/A")
    
    if price:
        col2.metric("Share Price", f"${price:,.2f}")
    else:
        col2.metric("Share Price", "N/A")
        
    if poly_mcap:
        col3.metric("Market Cap", f"${poly_mcap:,.0f}")
    elif price and pd.notna(shares_to_show):
        mcap = price * shares_to_show
        col3.metric("Market Cap", f"${mcap:,.0f}")
    else:
        col3.metric("Market Cap", "N/A")
    
    # Row 2: Historical Changes
    c1, c2, c3 = st.columns(3)
    def safe_metric(col, label, val):
        col.metric(label, f"{val:.2f}%" if pd.notna(val) else "N/A", delta_color="inverse")
        
    safe_metric(c1, "QoQ Change in Shares Outstanding", change_1q)
    safe_metric(c2, "YoY Change in Shares Outstanding", change_1y)
    safe_metric(c3, "5-Year Change in Shares Outstanding", change_5y)

# Create fragment if supported
if hasattr(st, "fragment"):
    display_live_metrics = st.fragment(run_every=10)(_render_live_metrics)
else:
    display_live_metrics = _render_live_metrics

def render_company_analysis(cik, ticker, title, latest_shares, change_1q, change_1y, change_5y, key_suffix=""):
    """
    Renders the full detailed analysis for a single company.
    Used in both My Portfolio (inside expander) and Company Search (standalone).
    """
    st.subheader(f"{title} ({ticker})")
    
    # Force Refresh Button
    if st.button("🔄 Force SEC Refresh", key=f"refresh_{ticker}_{cik}_{key_suffix}"):
        with st.spinner(f"Updating data for {ticker}..."):
            update_data_for_ticker(ticker)
            st.rerun()

    # 1. Live Metrics
    display_live_metrics(
        ticker,
        latest_shares,
        change_1q,
        change_1y,
        change_5y
    )
    
    st.divider()
    
    # 2. Main Charts (Shares, Mcap, Price - Always Displayed)
    show_shares = True
    show_mcap = True
    show_price = True
            
    allowed_charts = get_separate_charts(cik, ticker, show_shares, show_mcap, show_price)

    # Config for cleaner charts
    chart_config = {'displayModeBar': False, 'responsive': True}

    if 'shares' in allowed_charts:
        st.plotly_chart(allowed_charts['shares'], use_container_width=True, config=chart_config, key=f"chart_shares_{ticker}_{cik}_{key_suffix}")
    
    if 'mcap' in allowed_charts:
        st.plotly_chart(allowed_charts['mcap'], use_container_width=True, config=chart_config, key=f"chart_mcap_{ticker}_{cik}_{key_suffix}")
        
    if 'price' in allowed_charts:
        st.plotly_chart(allowed_charts['price'], use_container_width=True, config=chart_config, key=f"chart_price_{ticker}_{cik}_{key_suffix}")
    
    if not allowed_charts:
        st.info("Chart data unavailable.")



    st.markdown("#### Historical Share Counts")
    history_df = db_manager.get_history(cik)
    
    if not history_df.empty:
        # Limit to last 5 years as requested
        five_years_ago = pd.Timestamp.now() - pd.DateOffset(years=5)
        history_df['date'] = pd.to_datetime(history_df['date'])
        history_df = history_df[history_df['date'] >= five_years_ago]
        
    if not history_df.empty:
        # Columns in SQLite history table: cik, date, shares, source
        # Note: 'date' column in DB corresponds to 'end' in fetching logic
        #       'shares' column in DB corresponds to 'val' in fetching logic
        
        # Check available columns to be safe
        available_cols = history_df.columns.tolist()
        
        # Map DB columns to Display columns
        # We need 'date', 'shares', 'source'
        
        # Filter and Rename
        # If 'end' or 'val' are present (from raw fetch), use them. 
        # If 'date' and 'shares' (from DB), use them.
        
        if 'date' in available_cols and 'shares' in available_cols:
            display_cols = ['date', 'shares', 'source']
            table_df = history_df[display_cols].copy()
            table_df.columns = ['Date', 'Shares Outstanding', 'Source']
        elif 'end' in available_cols and 'val' in available_cols:
            # Fallback if we passed raw DF instead of DB DF
            col_map = {'end': 'Date', 'val': 'Shares Outstanding', 'source': 'Source'}
            # Ensure source exists
            if 'source' not in history_df.columns:
                 history_df['source'] = 'Unknown'
                 
            table_df = history_df[['end', 'val', 'source']].rename(columns=col_map)
        else:
             st.error(f"Unexpected data format. Available columns: {available_cols}")
             return

        table_df['Date'] = pd.to_datetime(table_df['Date']).dt.date
        table_df['Shares Outstanding'] = table_df['Shares Outstanding'].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "N/A")
        
        table_df = table_df.sort_values('Date', ascending=False)
        
        st.dataframe(table_df, width="stretch", hide_index=True, key=f"hist_table_{ticker}_{cik}_{key_suffix}")
    else:
        st.write("No historical data available.")

    # 5. Offering Text Section (Enhanced)
    st.divider()
    st.markdown("#### Capital Raising & Offerings")
    st.caption("Auto-scraped from recent filings (10-K, 8-K, Prospectus) detecting Convertibles, ATM, and Shelf Offerings.")
    
    offerings_df = db_manager.get_offerings(cik)
    if not offerings_df.empty:
        # Format for display
        display_df = offerings_df.copy()
        
        # Format date
        display_df['date'] = pd.to_datetime(display_df['date']).dt.strftime('%Y-%m-%d')
        
        # Format amount
        def format_amount(row):
            amount = row.get('amount', None)
            if pd.notna(amount) and amount > 0:
                if amount >= 1_000_000_000:
                    return f"${amount / 1_000_000_000:.1f}B"
                elif amount >= 1_000_000:
                    return f"${amount / 1_000_000:.1f}M"
                else:
                    return f"${amount:,.0f}"
            else:
                # Try to show amount_text if available
                amount_text = row.get('amount_text', None)
                if pd.notna(amount_text) and amount_text:
                    return amount_text
                return "—"
        
        display_df['Amount'] = display_df.apply(format_amount, axis=1)
        
        # Prepare display columns
        display_cols = ['category', 'date', 'Amount', 'source_filing']
        table_df = display_df[display_cols].copy()
        table_df.columns = ['Type', 'Date', 'Amount', 'Filing']
        
        # Sort by date descending
        table_df = table_df.sort_values('Date', ascending=False)
        
        # Display table
        st.dataframe(
            table_df,
            width="stretch",
            hide_index=True,
            key=f"offerings_table_{ticker}_{cik}_{key_suffix}"
        )
        
        # Optional: Show details in expanders
        with st.expander("📄 View Full Text Details"):
            for idx, row in offerings_df.iterrows():
                date_str = pd.to_datetime(row['date']).strftime('%Y-%m-%d')
                st.markdown(f"**{row['category']}** ({date_str} - {row['source_filing']})")
                st.markdown(f"_{row['text']}_")
                st.divider()
    else:
        st.info("No offering text detected in recent filings.")


def update_data_for_ticker(ticker_input, silent=False):
    """
    Fetches data for a single ticker on demand.
    Returns True if successful, False otherwise.
    """
    ticker_input = ticker_input.upper().strip()
    status_text = None
    if not silent:
        status_text = st.empty()
        status_text.text(f"Searching for {ticker_input}...")
    
    # 1. Get all tickers map (Using cached version to avoid redundant network calls)
    companies = get_master_ticker_list()
    if not companies:
        if status_text: status_text.error("Failed to fetch master ticker list.")
        return False
        
    # 2. Find CIK
    target_comp = None
    for comp in companies:
        if comp['ticker'] == ticker_input:
            target_comp = comp
            break
            
    if not target_comp:
        if status_text: status_text.error(f"Ticker {ticker_input} not found in SEC database.")
        return False
        
    # 3. Fetch Data
    cik = str(target_comp['cik_str']).zfill(10)
    db_manager.save_companies([target_comp]) # Ensure company info is in DB
    
    if status_text: status_text.text(f"Fetching data for {ticker_input} (CIK: {cik})...")
    
    # Use combined SEC + YFinance data for shares
    df = data_fetcher.get_share_history_combined(cik, ticker_input)
    
    # Also fetch Financials (SBC, Buybacks)
    financials_df = data_fetcher.get_financial_data(cik)
    if not financials_df.empty:
        db_manager.save_financials(cik, financials_df)
    
    # Fetch Price History
    prices_df = data_fetcher.get_price_history(ticker_input)
    if not prices_df.empty:
        db_manager.save_prices(cik, prices_df)
    
    # Fetch Offering Text
    offering_text_df = data_fetcher.get_offering_text(cik)
    if not offering_text_df.empty:
        db_manager.save_offerings(cik, offering_text_df)
    
    if df is not None and not df.empty:
        stats = data_fetcher.calculate_changes(df)
        db_manager.save_stats(cik, stats)
        db_manager.save_history(cik, df)
        if status_text:
            status_text.success(f"Successfully updated data for {ticker_input}")
            time.sleep(0.1)
            status_text.empty()
        return True
    else:
        if status_text: status_text.error(f"No share data found for {ticker_input} in XBRL filings.")
        return False

@st.cache_data(ttl=3600)
def get_master_ticker_list():
    return data_fetcher.get_tickers()

# --- LANDING PAGE & AUTH ---
def render_landing_page():
    st.markdown("""
        <div class="hero-container">
            <h1 class="hero-title">Terminal Intelligence</h1>
            <p class="hero-subtitle">The professional interface for monitoring institutional share count shifts and dilution tracking.</p>
        </div>
    """, unsafe_allow_html=True)
    
    f1, f2, f3 = st.columns(3)
    
    with f1:
        st.markdown("""
            <div class="feature-card">
                <span class="feature-icon">▣</span>
                <div class="feature-title">Portfolio Analysis</div>
                <div class="feature-text">High-precision tracking of ownership changes and capital structure shifts.</div>
            </div>
        """, unsafe_allow_html=True)

    with f2:
        st.markdown("""
            <div class="feature-card">
                <span class="feature-icon">▦</span>
                <div class="feature-title">Dilution Engine</div>
                <div class="feature-text">Real-time detection of ATM, S-8, and convertible notes from raw SEC filings.</div>
            </div>
        """, unsafe_allow_html=True)

    with f3:
        st.markdown("""
            <div class="feature-card">
                <span class="feature-icon">▤</span>
                <div class="feature-title">Buyback Tracking</div>
                <div class="feature-text">Quantitative assessment of repurchase program efficiency and actual value return.</div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 3rem;'></div>", unsafe_allow_html=True)
    
    a1, a2, a3 = st.columns([1, 1.5, 1])
    with a2:
        render_auth_form()

def render_auth_form():
    with st.container(border=True):
        tab_login, tab_signup = st.tabs(["Login", "Sign Up"])
        
        with tab_login:
            user_input = st.text_input("Username or Email", key="login_user")
            pass_input = st.text_input("Password", type="password", key="login_pass")
            if st.button("Log In", width="stretch", type="primary"):
                if not user_input or not pass_input:
                    st.error("Please enter both credentials.")
                else:
                    success, user_info = db_manager.authenticate_user(user_input, pass_input)
                    if success:
                        st.session_state['logged_in'] = True
                        st.session_state['user'] = user_info
                        st.session_state['active_tab'] = "My Portfolio"
                        st.query_params["tab"] = "My Portfolio"
                        st.rerun()
                    else:
                        st.error("Invalid username/email or password.")
            
            st.markdown("<p style='text-align: center; margin: 1rem 0; color: #8b949e;'>OR</p>", unsafe_allow_html=True)
            
            # Google Login Placeholder
            if st.button("Continue with Google", width="stretch"):
                st.session_state['logged_in'] = True
                st.session_state['user'] = {"username": "google_user", "full_name": "Google User"}
                st.session_state['active_tab'] = "My Portfolio"
                st.query_params["tab"] = "My Portfolio"
                st.rerun()

        with tab_signup:
            new_name = st.text_input("Full Name", key="signup_name")
            new_email = st.text_input("Email", key="signup_email")
            new_user = st.text_input("Username", key="signup_user")
            new_pass = st.text_input("Password", type="password", key="signup_pass")
            
            if st.button("Create Account", width="stretch", type="primary"):
                if not new_user or not new_pass or not new_email:
                    st.error("Username, Email, and Password are required.")
                else:
                    success, message = db_manager.add_user(new_user, new_email, new_pass, new_name)
                    if success:
                        st.session_state['logged_in'] = True
                        st.session_state['user'] = {"username": new_user, "full_name": new_name}
                        st.session_state['active_tab'] = "My Portfolio"
                        st.query_params["tab"] = "My Portfolio"
                        st.rerun()
                    else:
                        st.error(message)

# --- MAIN APP NAVIGATION ---
# Derive current state
is_logged_in = st.session_state.get('logged_in', False)

if is_logged_in:
    nav_options = ["My Portfolio", "Company Search"]
    icons = ["briefcase", "search"]
    user_display = st.session_state.get('user', {}).get('full_name') or st.session_state.get('user', {}).get('username') or "User"
else:
    nav_options = ["Home", "Company Search"]
    icons = ["house", "search"]

# Determine active tab
# Logic: Query Params > Session State > nav_options[0]
active_tab = st.query_params.get("tab") or st.session_state.get('active_tab') or nav_options[0]

# Ensure the tab is valid for current nav options (Home is gone after login)
if active_tab not in nav_options:
    active_tab = nav_options[0]

st.session_state['active_tab'] = active_tab

try:
    default_idx = nav_options.index(active_tab)
except ValueError:
    default_idx = 0

# Render Navigation component
# We use a dynamic KEY based on login status to force a component reset when options change
selected_nav = option_menu(
    menu_title=None,
    options=nav_options,
    icons=icons,
    menu_icon="cast",
    default_index=default_idx,
    orientation="horizontal",
    key=f"main_navbar_{is_logged_in}", 
    styles={
        "container": {"padding": "0!important", "background-color": "#0E0E10", "border-bottom": "1px solid #1F1F22"},
        "icon": {"color": "#10B981", "font-size": "14px"}, 
        "nav-link": {"font-size": "14px", "text-align": "left", "margin":"0px", "--hover-color": "#1F1F22", "color": "#8E9196", "font-weight": "500"},
        "nav-link-selected": {"background-color": "transparent", "color": "#FFFFFF", "border-bottom": "2px solid #10B981"},
    }
)

# Update state if changed by user click
if selected_nav and selected_nav != active_tab:
    st.session_state['active_tab'] = selected_nav
    st.query_params["tab"] = selected_nav
    st.rerun()

if is_logged_in:
    st.sidebar.markdown(f"### Hello, {user_display}! 👋")
    if st.sidebar.button("Log Out"):
        st.session_state['logged_in'] = False
        st.session_state['user'] = None
        st.session_state['active_tab'] = "Home"
        st.query_params["tab"] = "Home"
        st.rerun()


# --- PROCESSED TICKER CACHE ---
@st.cache_data(ttl=3600)
def get_processed_tickers():
    """
    Returns search_options and ticker_map.
    Optimized to minimize caching overhead.
    """
    master_list = get_master_ticker_list()
    if not master_list:
        return [], {}
        
    search_options = []
    ticker_map = {}
    for comp in master_list:
        ticker = comp['ticker']
        label = f"{ticker} - {comp['title']}"
        search_options.append(label)
        ticker_map[label] = comp
    
    search_options.sort()
    return search_options, ticker_map

# --- NAVIGATION LOGIC ---
if active_tab == "Home":
    render_landing_page()

elif active_tab == "My Portfolio":
    st.header("My Portfolio Tracking")
    
    # Portfolio Management
    conn = db_manager.sqlite3.connect(db_manager.DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS portfolio (ticker TEXT PRIMARY KEY)')
    conn.commit()
    
    search_options, ticker_map = get_processed_tickers()
    if search_options:
        
        col_add, col_manage = st.columns([2, 1])
        with col_add:
            if 'add_selectbox_key' not in st.session_state:
                st.session_state.add_selectbox_key = 0
                
            selected_add = st.selectbox(
                "Add Ticker to Portfolio", 
                options=search_options,
                index=None,
                placeholder="Type to search & add...",
                key=f"add_ticker_selectbox_{st.session_state.add_selectbox_key}"
            )
            
            if selected_add:
                target_comp = ticker_map[selected_add]
                new_ticker = target_comp['ticker']
                df_all_stats = db_manager.get_all_stats()
                ticker_has_stats = not df_all_stats.empty and new_ticker in df_all_stats['ticker'].values
                
                if not ticker_has_stats:
                    with st.spinner(f"Fetching data for {new_ticker}..."):
                        success = update_data_for_ticker(new_ticker)
                        if not success:
                            st.error(f"Could not find data for {new_ticker}")
                            new_ticker = None
                
                if new_ticker:
                    try:
                        add_conn = db_manager.sqlite3.connect(db_manager.DB_PATH)
                        add_conn.execute("INSERT OR IGNORE INTO portfolio (ticker) VALUES (?)", (new_ticker,))
                        add_conn.commit()
                        add_conn.close()
                        st.success(f"Added {new_ticker} to portfolio")
                        st.session_state.add_selectbox_key += 1
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding: {e}")

    portfolio_tickers = [row[0] for row in conn.execute("SELECT ticker FROM portfolio").fetchall()]
    conn.close()
    
    if not portfolio_tickers:
        st.info("Your portfolio is empty. Add stocks to track their share count changes.")
    else:
        df_all = db_manager.get_all_stats()
        portfolio_df = df_all[df_all['ticker'].isin(portfolio_tickers)].copy()

        # 3. Handle Stale or Missing Data (Background Auto-Refresh)
        tickers_to_refresh = []
        
        # Check for missing crucial dates
        cols_to_check = ['date_1y']
        if 'date_2y' in portfolio_df.columns: cols_to_check.append('date_2y')
        
        for index, row in portfolio_df.iterrows():
            ticker = row['ticker']
            
            # Check for missing stats
            is_missing = False
            for col in cols_to_check:
                if pd.isna(row.get(col)):
                    is_missing = True
                    break
            
            # Check for staleness (TTL: 24 hours)
            is_stale = False
            last_updated_str = row.get('last_updated')
            if last_updated_str:
                try:
                    last_updated = datetime.fromisoformat(last_updated_str)
                    if datetime.now() - last_updated > timedelta(hours=24):
                        is_stale = True
                except:
                    is_stale = True # Corrupt date, refresh to be safe
            else:
                is_stale = True # No update record
            
            if is_missing or is_stale:
                tickers_to_refresh.append(ticker)
        
        tickers_to_refresh = list(set(tickers_to_refresh))
        
        if tickers_to_refresh and not st.session_state['sync_active']:
            start_background_sync(tickers_to_refresh)
        
        missing_stats = set(portfolio_tickers) - set(portfolio_df['ticker'].tolist())
        if missing_stats:
            st.warning(f"Waiting for data for: {', '.join(missing_stats)}.")

        if portfolio_df.empty:
             st.warning("No data found for portfolio tickers.")
        else:
            st.subheader("Portfolio Overview")
            st.caption("Select a row below to view detailed analysis and charts.")
            
            cols_to_fetch = ['ticker', 'title', 'latest_shares', 'change_pct_1q', 'date_1q', 'change_pct_1y', 'date_1y', 'change_pct_5y', 'last_date']
            for y in range(2, 11):
                col_name = f'change_pct_{y}y'
                if col_name in portfolio_df.columns and col_name not in cols_to_fetch:
                    cols_to_fetch.append(col_name)
            
            display_df = portfolio_df[cols_to_fetch].copy().reset_index(drop=True)
            display_df['latest_shares'] = display_df['latest_shares'].apply(lambda x: f"{x:,.0f}" if pd.notna(x) else "N/A")
            
            # Ensure numeric types for styling
            numeric_cols = ['change_pct_1q', 'change_pct_1y', 'change_pct_5y']
            for y in range(2, 11): numeric_cols.append(f'change_pct_{y}y')
            for col in numeric_cols:
                if col in display_df.columns:
                    display_df[col] = pd.to_numeric(display_df[col], errors='coerce')

            # Formatting Display Columns
            def get_quarter_str(date_val):
                if pd.isna(date_val): return "?"
                try:
                    d = pd.to_datetime(date_val)
                    return f"Q{(d.month-1)//3 + 1} '{d.year % 100}"
                except: return "?"

            def get_year_str(date_val):
                if pd.isna(date_val): return "?"
                try: return str(pd.to_datetime(date_val).year)
                except: return "?"
            
            display_df['QoQ_Display'] = display_df.apply(lambda row: 
                f"{row['change_pct_1q']:.2f}% ({get_quarter_str(row.get('date_1q'))} → {get_quarter_str(row['last_date'])})"
                if pd.notna(row.get('change_pct_1q')) else "N/A", axis=1)

            display_df['YoY_Display'] = display_df.apply(lambda row: 
                f"{row['change_pct_1y']:.2f}% ({get_year_str(row.get('date_1y'))} → {get_year_str(row['last_date'])})"
                if pd.notna(row.get('change_pct_1y')) else "N/A", axis=1)

            # Row Styling
            def color_row(row):
                styles = [''] * len(row.index)
                def get_color(val): return "#ff4b4b" if val > 0 else "#3dd56d" if val < 0 else ""
                def parse_pct_str(s):
                    try: return float(s.split('%')[0])
                    except: return None

                if 'QoQ_Display' in row.index:
                    val = parse_pct_str(row['QoQ_Display'])
                    if val is not None:
                        color = get_color(val)
                        if color: styles[row.index.get_loc('QoQ_Display')] = f'color: {color}'

                if 'YoY_Display' in row.index:
                    val = parse_pct_str(row['YoY_Display'])
                    if val is not None:
                        color = get_color(val)
                        if color: styles[row.index.get_loc('YoY_Display')] = f'color: {color}'

                for col_name in row.index:
                    if col_name.startswith('change_pct_'):
                         val = row[col_name]
                         if pd.notna(val) and isinstance(val, (int, float)):
                              color = get_color(val)
                              if color: styles[row.index.get_loc(col_name)] = f'color: {color}'
                return styles

            styled_df = display_df.style.apply(color_row, axis=1)
            
            col_config = {
                "ticker": "Ticker", "title": "Company", "latest_shares": st.column_config.TextColumn("Shares"),
                "QoQ_Display": st.column_config.TextColumn("QoQ (Period)"),
                "YoY_Display": st.column_config.TextColumn("YoY (Period)"),
                "change_pct_5y": st.column_config.NumberColumn("5Y", format="%.2f%%"),
                "last_date": "Last Updated"
            }
            for y in range(2, 11):
                if y != 5: col_config[f'change_pct_{y}y'] = st.column_config.NumberColumn(f"{y}Y", format="%.2f%%")
            
            col_order = ["ticker", "title", "latest_shares", "QoQ_Display", "YoY_Display", "change_pct_5y", "last_date"]
            
            # Selection Handling
            if 'portfolio_selected_ticker' not in st.session_state:
                st.session_state.portfolio_selected_ticker = None
                
            event = st.dataframe(
                styled_df, 
                column_config=col_config, 
                column_order=col_order, 
                width="stretch", 
                hide_index=True, 
                on_select="rerun", 
                selection_mode="single-row", 
                key="portfolio_table"
            )
            
            # Sync selection to session state
            if len(event.selection.rows) > 0:
                selected_row_idx = event.selection.rows[0]
                st.session_state.portfolio_selected_ticker = display_df.iloc[selected_row_idx]['ticker']
            
            # Action: Show details for selected if any selection exists (persisted)
            if st.session_state.portfolio_selected_ticker:
                selected_ticker = st.session_state.portfolio_selected_ticker
                
                # Check if selected ticker is still in portfolio_df (it might have been removed)
                match_df = portfolio_df[portfolio_df['ticker'] == selected_ticker]
                if not match_df.empty:
                    # Show Remove Button for selected
                    if st.button(f"🗑️ Remove {selected_ticker}", type="primary"):
                        remove_conn = db_manager.sqlite3.connect(db_manager.DB_PATH)
                        remove_conn.execute("DELETE FROM portfolio WHERE ticker = ?", (selected_ticker,))
                        remove_conn.commit()
                        remove_conn.close()
                        st.session_state.portfolio_selected_ticker = None # Clear state
                        st.success(f"Removed: {selected_ticker}")
                        st.rerun()
                    
                    # 2. Detail Analysis (Selective)
                    st.divider()
                    st.subheader("Detailed Analysis")
                    row_data = match_df.iloc[0]
                    render_company_analysis(
                        row_data['cik'], 
                        selected_ticker, 
                        row_data['title'], 
                        row_data['latest_shares'], 
                        row_data.get('change_pct_1q'), 
                        row_data.get('change_pct_1y'), 
                        row_data.get('change_pct_5y'), 
                        key_suffix="port"
                    )
                else:
                    # Ticker was removed or not found
                    st.session_state.portfolio_selected_ticker = None

elif active_tab == "Company Search":
    st.header("Individual Company Analysis")
    search_options, ticker_map = get_processed_tickers()
    if search_options:
        if 'search_selection' not in st.session_state:
            st.session_state.search_selection = None
            
        selected_option = st.selectbox(
            "Search Company (Type Ticker or Name)", 
            options=search_options, 
            index=search_options.index(st.session_state.search_selection) if st.session_state.search_selection in search_options else None, 
            placeholder="Type to search...",
            key="search_selectbox"
        )
        st.session_state.search_selection = selected_option

        if selected_option:
            target_comp = ticker_map[selected_option]
            selected_ticker = target_comp['ticker']
            df_existing = db_manager.get_all_stats()
            comp_row_df = df_existing[df_existing['ticker'] == selected_ticker] if not df_existing.empty else pd.DataFrame()
            
            if comp_row_df.empty:
                with st.spinner(f"Initial setup for {selected_ticker}..."):
                    if update_data_for_ticker(selected_ticker):
                        df_existing = db_manager.get_all_stats()
                        comp_row_df = df_existing[df_existing['ticker'] == selected_ticker]
            
            if not comp_row_df.empty:
                comp_row = comp_row_df.iloc[0]
                cik = comp_row['cik']
                
                # Database-First: Render immediately. 
                # Users can use the "Force SEC Refresh" button inside if they want new filings.
                render_company_analysis(cik, selected_ticker, comp_row['title'], comp_row['latest_shares'], comp_row['change_pct_1q'], comp_row['change_pct_1y'], comp_row['change_pct_5y'], key_suffix="search")
            else:
                st.warning("Data unavailable.")
