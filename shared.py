#!/usr/bin/env python3
"""
PocketRS Pro — Live Update Server
===================================
Runs on Railway.app (free tier)
Updates all 2000 NSE stocks every 1 minute during market hours
Uses Upstox Analytics Token (never expires — no daily refresh needed!)

Environment variables:
  UPSTOX_ANALYTICS_TOKEN  - Your Upstox analytics token (permanent)
  SUPABASE_URL            - Supabase project URL
  SUPABASE_SERVICE_KEY    - Supabase service role key
"""

import os
import gc
import csv
import sys
import time
import json
import re
import math
import random
import asyncio
import aiohttp
import logging
import boto3
import gzip
import xml.etree.ElementTree as ET
from botocore.config import Config as BotoConfig
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('pocketrs')

__all__ = [
    'ALL_STOCKS',
    'ANALYTICS_TOKEN',
    'BATCH_SIZE',
    'BotoConfig',
    'ET',
    'EXTRA_STOCKS',
    'FUNDAMENTALS_TTL',
    'INDEX_TRACKER',
    'IST',
    'MARKET_CLOSE_H',
    'MARKET_CLOSE_M',
    'MARKET_OPEN_H',
    'MARKET_OPEN_M',
    'MICROCAP',
    'MIDCAP',
    'NIFTY50',
    'NIFTY50_SEED_PRICES',
    'NIFTY_INDEX_CSV_URLS',
    'NIFTY_INSTRUMENT_KEY',
    'Optional',
    'R2_ACCESS_KEY_ID',
    'R2_ACCOUNT_ID',
    'R2_BUCKET_NAME',
    'R2_SECRET_ACCESS_KEY',
    'SECTOR_INDUSTRY_LOOKUP',
    'SECTOR_MAP',
    'SMALLCAP',
    'SUPABASE_KEY',
    'SUPABASE_URL',
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHAT_ID',
    'TELEGRAM_TOKEN',
    'UPDATE_INTERVAL',
    '_AI_PICKS_REFRESH_INTERVAL_SEC',
    '_AI_PICKS_TOP_N',
    '_ANN_NEGATIVE_PATTERNS',
    '_ANN_POSITIVE_PATTERNS',
    '_LAST_AI_PICKS_TS',
    '_LAST_FUNDAMENTALS_SYNC_TS',
    '_MONTH_NAMES',
    '_NSE_ANNOUNCEMENTS_HEADER_SETS',
    '_ORDER_CANCEL_PATTERNS',
    '_ORDER_WIN_PATTERNS',
    '_R2_STOCK_FIELDS',
    '_RESULTS_ANN_EXCLUDE',
    '_RESULTS_ANN_KEYWORDS',
    '_RESULTS_ATTEMPT_COOLDOWN_SEC',
    '_SCREENER_HEADER_SETS',
    '_XBRL_EPS_TAGS',
    '_XBRL_OTHER_INCOME_TAGS',
    '_XBRL_PAT_TAGS',
    '_XBRL_PBT_TAGS',
    '_XBRL_SALES_TAGS',
    '__all__',
    '_fetch_error_counts',
    '_fundamentals_debug_count',
    '_industry_endpoint_path',
    '_live_index_debug_count',
    '_live_nifty_debug_count',
    '_load_sector_industry_lookup',
    '_nse_announcements_debug_count',
    '_r2_client',
    '_r2_put_object_sync',
    '_r2_size_diagnostic_logged',
    '_r2_warned',
    '_results_attempt_times',
    '_upstox_fundamentals_debug_count',
    '_upstox_shareholding_debug_count',
    '_zero_chg_debug_count',
    'aiohttp',
    'asyncio',
    'boto3',
    'csv',
    'datetime',
    'ensure_fundamentals_table',
    'fetch_fundamentals_screener',
    'fetch_upstox_fundamentals',
    'fundamentals_cache',
    'gc',
    'get_industry',
    'get_sector',
    'gzip',
    'historical_cache',
    'history_dates_cache',
    'index_history_cache',
    'index_key_map',
    'instrument_key_map',
    'is_market_open',
    'json',
    'last_eod_refresh_date',
    'load_fundamentals_batch',
    'load_fundamentals_from_supabase',
    'load_instrument_master',
    'log',
    'logging',
    'math',
    'midcap_cache',
    'nifty_cache',
    'opens_cache',
    'os',
    'prev_hy_ht_state',
    'prev_squeeze_state',
    'random',
    're',
    'save_fundamentals_batch_to_db',
    'smallcap_cache',
    'sys',
    'time',
    'timedelta',
    'timezone',
    'upload_snapshot_to_r2'
]


# ── Config ────────────────────────────────────────────────────────────
ANALYTICS_TOKEN  = os.environ['UPSTOX_ANALYTICS_TOKEN']
SUPABASE_URL     = os.environ['SUPABASE_URL']
SUPABASE_KEY     = os.environ['SUPABASE_SERVICE_KEY']
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
UPDATE_INTERVAL  = 60          # seconds between updates
BATCH_SIZE       = 500         # Upstox supports 500 per bulk call
IST              = timezone(timedelta(hours=5, minutes=30))

# R2 (Cloudflare) — optional. All getenv() with empty defaults so the app
# starts up and scans normally even before these are configured; the
# upload function below just logs a warning once and skips itself if
# they're missing, rather than crashing the whole scan loop.
R2_ACCOUNT_ID        = os.getenv('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID     = os.getenv('R2_ACCESS_KEY_ID', '')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET_NAME       = os.getenv('R2_BUCKET_NAME', '')
_r2_client = None
_r2_warned = False
if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME:
    _r2_client = boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version='s3v4'),
        region_name='auto',
    )

# ── Telegram Bot ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

# Market hours IST
MARKET_OPEN_H, MARKET_OPEN_M   = 9, 15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30

# ── Math functions ────────────────────────────────────────────────────
# ── Classic chart pattern detection ─────────────────────────────────
# Heuristic, swing-point (fractal pivot) based — not exact textbook
# geometry, but a reasonable, tunable approximation. Runs off the same
# price history already in memory for every other signal here, so no
# new data source is needed. Validated against synthetic price series
# (double top/bottom, all three triangle types, head & shoulders, a
# bullish flag) before being wired in — not yet checked against real
# market data, so expect to tune the tolerance constants once real
# results come in.
# ── Sector map ────────────────────────────────────────────────────────
SECTOR_MAP = {
    "IT":            ["TCS","INFY","WIPRO","HCLTECH","TECHM","MPHASIS","PERSISTENT","COFORGE","LTTS","KPITTECH","TATAELXSI"],
    "Private Bank":  ["HDFCBANK","ICICIBANK","KOTAKBANK","AXISBANK","INDUSINDBK","BANDHANBNK","FEDERALBNK","IDFCFIRSTB","RBLBANK","AUBANK","YESBANK"],
    "PSU Bank":      ["SBIN","PNB","CANBK","BANKBARODA","UNIONBANK","BANKINDIA"],
    "Defence":       ["HAL","BEL","BDL","MAZDOCK","COCHINSHIP","BEML","MIDHANI","GRSE",
                       "DATAPATTNS","PARAS","ZENTEC","APOLLO"],
    "NBFC":          ["BAJFINANCE","BAJAJFINSV","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","AAVAS","HOMEFIRST","LICHSGFIN","PNBHOUSING","CANFINHOME"],
    "Auto":          ["MARUTI","TMPV","M&M","BAJAJ-AUTO","HEROMOTOCO","TVSMOTOR","EICHERMOT","BOSCHLTD","MOTHERSON","ESCORTS"],
    "Pharma":        ["SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","LUPIN","AUROPHARMA","BIOCON","ALKEM","GLENMARK","IPCALAB","MANKIND","JUBLPHARMA"],
    "FMCG":          ["HINDUNILVR","ITC","NESTLEIND","DABUR","MARICO","COLPAL","EMAMILTD","GODREJCP","TATACONSUM"],
    "Energy":        ["RELIANCE","ONGC","BPCL","IOC","HINDPETRO","GAIL","PETRONET","IGL","MGL","ATGL"],
    "Metals":        ["JSWSTEEL","TATASTEEL","HINDALCO","COALINDIA","VEDL","NMDC","MOIL"],
    "Infra/Capital": ["LT","SIEMENS","ABB","BHEL","CUMMINSIND","THERMAX","HAVELLS"],
    "Cement":        ["ULTRACEMCO","GRASIM","SHREECEM","AMBUJACEM","ACC","JKCEMENT","RAMCOCEM"],
    "Consumer":      ["TITAN","ASIANPAINT","BERGEPAINT","PIDILITIND","VOLTAS","CROMPTON"],
    "Telecom":       ["BHARTIARTL","IDEA","TATACOMM","RAILTEL","HFCL","STLTECH"],
    "Realty":        ["DLF","GODREJPROP","OBEROIRLTY","PRESTIGE","BRIGADE","PHOENIXLTD","SOBHA","LODHA"],
    "Healthcare":    ["APOLLOHOSP","FORTIS","MAXHEALTH","METROPOLIS","THYROCARE","LALPATHLAB","NH","ASTERDM"],
    "Insurance":     ["SBILIFE","HDFCLIFE","ICICIPRULI","LICI","GICRE","STARHEALTH"],
    "Internet":      ["ETERNAL","NYKAA","PAYTM","POLICYBZR","INDIAMART","JUSTDIAL","RATEGAIN","IXIGO"],
    "Travel":        ["IRCTC","EASEMYTRIP","THOMASCOOK"],
    "Exchange":      ["BSE","CDSL","CAMS","MCX","ANGELONE"],
}

def _load_sector_industry_lookup() -> dict:
    """One-time load of the static sector/industry lookup table (built
    from a broad market-cap sweep, ~2,355 symbols after dropping indices/
    ETFs) into memory at import time. This is a far more reliable source
    than fetch_fundamentals_screener()'s live scrape — that scraper has a
    documented ~98% blank-response rate (Railway IP rate-limiting) and,
    as of the last review, its sector/industry regex patterns don't even
    match screener.in's current markup, so it was never actually
    populating 'industry' in practice. This file needs periodic manual
    refreshes (sector/industry classifications drift slowly, so this
    doesn't need to be live), but the coverage is dramatically better
    than SECTOR_MAP's ~150 hand-curated symbols, and it's zero extra API
    calls or scraping.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'sector_industry_lookup.csv')
    lookup = {}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sym = (row.get('symbol') or '').strip()
                if sym:
                    lookup[sym] = {
                        'industry': (row.get('industry') or '').strip() or None,
                        'sector':   (row.get('sector') or '').strip() or None,
                    }
        log.info(f"✅ Loaded sector/industry lookup: {len(lookup)} symbols from {path}")
    except Exception as e:
        log.warning(f"Sector/industry lookup load failed ({path}): {e} — falling back to SECTOR_MAP + live fetch only")
    return lookup

SECTOR_INDUSTRY_LOOKUP = _load_sector_industry_lookup()


SECTOR_INDUSTRY_LOOKUP = _load_sector_industry_lookup()

# ── Upstox API ────────────────────────────────────────────────────────
NIFTY50   = ["RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK","INFY","SBIN","HINDUNILVR","ITC","LT","KOTAKBANK","HCLTECH","AXISBANK","BAJFINANCE","MARUTI","ASIANPAINT","SUNPHARMA","TITAN","ULTRACEMCO","NESTLEIND","WIPRO","NTPC","POWERGRID","TECHM","TMPV","ADANIENT","ADANIPORTS","ONGC","BAJAJFINSV","JSWSTEEL","TATASTEEL","COALINDIA","HINDALCO","M&M","DRREDDY","CIPLA","EICHERMOT","DIVISLAB","BPCL","GRASIM","INDUSINDBK","APOLLOHOSP","BAJAJ-AUTO","HEROMOTOCO","TVSMOTOR","SHREECEM","BRITANNIA","VEDL","BEL","NTPC"]
MIDCAP    = ["MPHASIS","PERSISTENT","COFORGE","LTTS","TATAELXSI","BANDHANBNK","FEDERALBNK","IDFCFIRSTB","RBLBANK","AUBANK","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","AAVAS","ESCORTS","AUROPHARMA","LUPIN","BIOCON","ALKEM","GLENMARK","IPCALAB","EMAMILTD","GODREJCP","NMDC","MOIL","PRESTIGE","BRIGADE","PHOENIXLTD","SOBHA","LODHA","METROPOLIS","THYROCARE","LALPATHLAB","NH","ASTERDM","STARHEALTH","MCX","ANGELONE","EASEMYTRIP","RATEGAIN"]
SMALLCAP  = ["DELTACORP","GMRINFRA","IDEA","SUZLON","UNITECH","DISHTV","JPASSOCIAT","PVR","INDIABULL","KOLTEPATIL","LEMONTREE","THOMASCOOK","JUSTDIAL","IXIGO","ALOKTEXT","RADICO","HEIDELBERG","BIRLACORPN","JKCEMENT","RAMCOCEM","HFCL","STLTECH","TEJAS","ROUTE","RAILTEL","NSDL","CANFINHOME","APTUS","HOMEFIRST","REPCO","SPANDANA","CREDITACC","SATIN"]

MICROCAP = [
  "MTAR","TDPOWERSYS","STLTECH","SANSERA","ASTRAMICRO","SOUTHBANK","UJJIVANSFB",
  "KTKBANK","SURYODAY","ESAFSFB","SAFARI","ANANTRAJ","HIKAL","KPIL","NUVOCO",
  "ORIENTELEC","POLYMED","RAJRATAN","SBFC","SENCO","SHOPERSTOP","SMLISUZU",
  "STOVEKRAFT","SUPRAJIT","IPCALAB","FLUOROCHEM","GABRIEL","GHCL","GNFC",
  "GRINDWELL","GSFC","HARDWYN","HATSUN","HINDCOPPER","HOEC","HONASA","IGPL",
  "INTELLECT","IRCON","IRFC","ISEC","JUBLFOOD","JYOTHYLAB","KALYANKJIL",
  "KANSAINER","KARURVYSYA","KRBL","LUXIND","MAYURUNIQ","MIDHANI","MINDAIND",
  "MOLDTKPAC","MONTECARLO","MPSLTD","MRPL","NAVINFLUOR","NOCIL","NUCLEUS",
  "OLECTRA","OMAXE","PAISALO","PCJEWELLER","PIIND","POLYCAB","POWERMECH",
  "PRINCEPIPE","PRSMJOHNSN","PURVA","QUICKHEAL","RAJESHEXPO","RAYMOND",
  "REDINGTON","RELAXO","REPCO","RITES","ROSSARI","RUPA","RVNL","SADBHAV",
  "SAKSOFT","SANDHAR","SAREGAMA","SASKEN","SEQUENT","SHAKTIPUMP","SHILPAMED",
  "SHOPERSTOP","SHREDIGIT","SKIPPER","SNOWMAN","SOLARA","SONACOMS","SOTL",
  "SPANDANA","SPENCERS","STAR","STCINDIA","STEELCITY","SUDARSCHEM","SUMICHEM",
  "SUNTV","SUPRAJIT","SUPREMEIND","SYNCOMF","TALBROAUTO","TARSONS","TASTYBITE",
  "TEAMLEASE","TEXRAIL","THANGAMAYL","TIRUMALCHM","TITAGARH","TMVFINANCE",
  "TORNTPOWER","TRIGYN","TRIVENI","TTKHLTCARE","TTKPRESTIG","TVTODAY","UFLEX",
  "UNIENTER","UTTAMSUGAR","V2RETAIL","VAIBHAVGBL","VARROC","VENKEYS","VESUVIUS",
  "VGUARD","VIMTALABS","VINDHYATEL","VIPIND","VOLTAMP","VRLLOG","VSTIND",
  "VSTL","WABCOINDIA","WEIZMANIND","WELCORP","WONDERLA","XCHANGING","ZENTEC",
  "ZEEMEDIA","ZYDUSLIFE","NRBBEARING","NILKAMAL","NESCO","NETWORK18","NELCO",
  "NDTV","NCLIND","NOCIL","NAUKRI","NAGAFERT","MTNL","MONARCH","METROBRAND",
  "MEDANTA","MASTEK","MARATHON","MASFIN","MANINFRA","MAHASTEEL","LGBBROSLTD"
]


# Popular NSE stocks not in major indices — PSU, Defence, Mid/Small caps
EXTRA_STOCKS = [
    # Defence PSU
    "GRSE","BDL","HAL","BEL","MIDHANI","BEML","COCHINSHIP","MAZAGON",
    # PSU Banks/Finance  
    "BANKBARODA","PNB","UNIONBANK","CANARABANK","INDIANB","IOB","CENTRALBK",
    # PSU Energy/Infra
    "NHPC","SJVN","IRFC","RVNL","IRCON","NBCC","HUDCO","RAILTEL",
    # Popular midcap/smallcap
    "SHAKTIPUMP","ELECON","GPIL","JYOTICNC","PNCINFRA","KNRCON",
    "HGINFRA","AHLUCONT","CAPACITE","WELCORP","RAMCOCEM","DALBHARAT",
    "JKCEMENT","NUVOCO","HEIDELBERG","BIRLACORPN","ORIENTCEM",
    # Auto ancillary
    "SUPRAJIT","LUMAXTECH","SANDHAR","ENDURANCE","SUBROS","UCALFUEL",
    # Chemicals
    "DEEPAKFERT","GNFC","GSFC","RASHTRIYA","CHAMBAL","COROMANDEL",
    # Textiles  
    "GRASIM","VARDHMAN","RAYMOND","ARVIND","WELSPUNIND","TRIDENT",
    # Pharma
    "IPCALAB","AJANTPHARM","NATCOPHARM","GRANULES","SOLARA","AARTI",
]

ALL_STOCKS = list(dict.fromkeys(NIFTY50 + MIDCAP + SMALLCAP + MICROCAP + EXTRA_STOCKS))

# ── Official index constituent lists (fetched live at startup) ────────
# The hardcoded NIFTY50/MIDCAP/SMALLCAP/MICROCAP arrays above are small
# fallback samples. At startup we replace them with the real, current
# official lists published by niftyindices.com. If that fetch fails for
# any reason, we silently keep using the hardcoded fallback so the app
# never breaks.
NIFTY_INDEX_CSV_URLS = {
    'NIFTY50':   'https://niftyindices.com/IndexConstituent/ind_nifty50list.csv',
    'MIDCAP150': 'https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv',
    'SMALLCAP250': 'https://niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv',
    'MICROCAP250': 'https://niftyindices.com/IndexConstituent/ind_niftymicrocap250_list.csv',
}

# Popular NSE stocks not in major indices — PSU, Defence, Mid/Small caps
EXTRA_STOCKS = [
    # Defence PSU
    "GRSE","BDL","HAL","BEL","MIDHANI","BEML","COCHINSHIP","MAZAGON",
    # PSU Banks/Finance  
    "BANKBARODA","PNB","UNIONBANK","CANARABANK","INDIANB","IOB","CENTRALBK",
    # PSU Energy/Infra
    "NHPC","SJVN","IRFC","RVNL","IRCON","NBCC","HUDCO","RAILTEL",
    # Popular midcap/smallcap
    "SHAKTIPUMP","ELECON","GPIL","JYOTICNC","PNCINFRA","KNRCON",
    "HGINFRA","AHLUCONT","CAPACITE","WELCORP","RAMCOCEM","DALBHARAT",
    "JKCEMENT","NUVOCO","HEIDELBERG","BIRLACORPN","ORIENTCEM",
    # Auto ancillary
    "SUPRAJIT","LUMAXTECH","SANDHAR","ENDURANCE","SUBROS","UCALFUEL",
    # Chemicals
    "DEEPAKFERT","GNFC","GSFC","RASHTRIYA","CHAMBAL","COROMANDEL",
    # Textiles  
    "GRASIM","VARDHMAN","RAYMOND","ARVIND","WELSPUNIND","TRIDENT",
    # Pharma
    "IPCALAB","AJANTPHARM","NATCOPHARM","GRANULES","SOLARA","AARTI",
]

ALL_STOCKS = list(dict.fromkeys(NIFTY50 + MIDCAP + SMALLCAP + MICROCAP + EXTRA_STOCKS))

_SCREENER_HEADER_SETS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.screener.in/",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/",
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "Referer": "https://www.screener.in/",
    },
]


# Cache fundamentals to avoid re-fetching every minute
fundamentals_cache: dict = {}  # sym -> {market_cap, pe, roe, eps, debt_eq, promoter, fetched_at}
FUNDAMENTALS_TTL = 30 * 24 * 3600  # refresh monthly (data changes quarterly; monthly is a safe margin without re-fetching on every restart)

_fundamentals_debug_count = 0  # caps detailed per-request diagnostic logging
_upstox_fundamentals_debug_count = 0  # caps raw-response logging for the new Upstox fundamentals API
_upstox_shareholding_debug_count = 0  # separate budget so share-holdings isn't starved by key-ratios logging
_live_nifty_debug_count = 0  # caps raw-response logging for the live Nifty price fetch
_live_index_debug_count = 0  # caps raw-response logging for the live all-indices price fetch
_industry_endpoint_path = None  # remembered once a working fundamentals industry path is found
_fetch_error_counts: dict = {}  # exception-type name -> count, reset per load_fundamentals_batch call,
# aggregated (not logged per-call) so a systemic failure shows up as one
# clear summary line instead of thousands of repeated log entries

_NSE_ANNOUNCEMENTS_HEADER_SETS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/118.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK",
    },
]
_nse_announcements_debug_count = 0  # caps raw-response logging while verifying the real field shape

# ── Supabase client ───────────────────────────────────────────────────
# Exact set of fields the frontend's transformStockRow() actually reads
# (src/lib/db.js) — cross-referenced directly from that function's body,
# not guessed. The R2 snapshot was coming out ~5.5x larger than the
# equivalent Supabase read (6.05MB vs ~1.1MB) because it was uploading
# each stock's full internal `processed` dict as-is, including fields
# the frontend never touches. R2's cost is $0 regardless of size, but a
# smaller file still means a faster first-load for every user each time
# the CDN cache needs refreshing.
_R2_STOCK_FIELDS = frozenset({
    'sym','rs','rs_tv','rs_nifty50','rs_midcap','rs_smallcap','rs_microcap','rs_sector',
    'last_price','chg_pct','high_52w','sector','industry','chg_w_pct','chg_m_pct',
    'stop_loss','target',
    'in_nifty50','in_midcap','in_smallcap','in_microcap','rvol','ibv_signal',
    'is_resistance_breakout','is_52wh_breakout','resistance_r1',
    'is_cup_handle_breakout','has_cup_pattern','cup_depth_pct',
    'is_guppy_bullish_crossover','is_guppy_bearish_crossover','is_guppy_compressed',
    'vol_signal','rs_line_new_high','rs_line_trend','rs_line_value','is_s2_new_entry',
    'market_cap','pe','roe','eps','debt_eq','promoter',
    'eps_qoq','eps_yoy','sales_qoq','sales_yoy','opm_pct','opm_trend',
    'eps_growth_streak','fii_pct','fii_trend','dii_pct','dii_trend',
    'promoter_trend','peg_ratio',
    'fundamental_score','fundamental_label',
    'rs_hist','rs_trend','rs_slope',
    'is_pp','pp_hist','pp_count_10d','pp_vol_ratio','ma10','ma50',
    'is_hy','hy_pct','volume','hy_hist',
    'is_ht','ht_pct','ht_hist','ibv_hist',
    'near_ema9','ema9','pct_from_ema9',
    'near_ema21','ema21','pct_from_ema21',
    'near_ema50','ema50','pct_from_ema50',
    'near_52wl','pct_from_52wl','low_52w','crossed_ema5','pp_volume_52wl',
    'is_52wl_signal','ema5',
    'is_weak_rs','weak_chg_1d','weak_chg_5d','weak_vol_spike',
    'in_squeeze','squeeze_fired','bb_width_pct','squeeze_days',
    'is_vcp','vcp_stage','vcp_fired','vcp_contractions',
    'last_updated','scan_type',
})

_r2_size_diagnostic_logged = False

# ── Market hours check ────────────────────────────────────────────────
# ── Squeeze fire state tracking ──────────────────────────────────────
# Track which stocks were firing last scan — only alert on NEW fires
# Format: {sym: {'bb': bool, 'vcp': bool}}
prev_squeeze_state: dict = {}

# ── HY/HT volume-climax fire state tracking ──────────────────────────
# Same "only alert on the transition into firing" pattern as squeeze
# state above, tracked separately so a stock that stays HY/HT for
# several scans in a row (common — these are daily volume-vs-history
# ratios, not instantaneous events) only triggers one notification at
# the moment it turns on, not every single scan while it's true.
# Format: {sym: {'hy': bool, 'ht': bool}}
prev_hy_ht_state: dict = {}


historical_cache: dict = {}   # sym -> {prices, volumes, highs, lows}
history_dates_cache: dict = {}  # sym -> [dates] — parallel to historical_cache,
# tracked separately since RS calc doesn't need dates but incremental merges do
opens_cache: dict = {}  # sym -> [opens] — parallel to historical_cache, tracked
# separately since RS/PP/signal calc doesn't need Open prices, only the
# persisted stock_full_history table (for candlestick charts) does
last_eod_refresh_date: Optional[str] = None  # IST date string — ensures the
# expensive EOD refresh (full Yahoo re-fetch + fundamentals) runs only ONCE
# per day, not on every single scan cycle while the market stays closed.
nifty_cache: dict = {}        # {'prices': [...]} — Nifty index daily closes for TV RS calc
midcap_cache: dict = {}       # {'prices': [...]} — synthetic Midcap 150 index
smallcap_cache: dict = {}     # {'prices': [...]} — synthetic Smallcap 250 index

NIFTY_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"  # Upstox key for Nifty 50 index

# All indices to track on the Index Dashboard page
# Key = display name, value = Upstox instrument key
INDEX_TRACKER = {
    "Nifty 50":       "NSE_INDEX|Nifty 50",
    "Nifty Next 50":  "NSE_INDEX|Nifty Next 50",
    "Nifty 500":      "NSE_INDEX|Nifty 500",
    "Bank Nifty":     "NSE_INDEX|Nifty Bank",
    "IT":             "NSE_INDEX|Nifty IT",
    "Pharma":         "NSE_INDEX|Nifty Pharma",
    "Auto":           "NSE_INDEX|Nifty Auto",
    "FMCG":           "NSE_INDEX|Nifty FMCG",
    "Metal":          "NSE_INDEX|Nifty Metal",
    "Realty":         "NSE_INDEX|Nifty Realty",
    "Energy":         "NSE_INDEX|Nifty Energy",
    "Defence":            "NSE_INDEX|Nifty India Defence",
    "Financial Services": "NSE_INDEX|Nifty Fin Service",
    "PSU Bank":           "NSE_INDEX|Nifty PSU Bank",
    "Private Bank":       "NSE_INDEX|Nifty Pvt Bank",
    "PSE":                "NSE_INDEX|Nifty PSE",
    "Media":              "NSE_INDEX|Nifty Media",
    "Infrastructure":     "NSE_INDEX|Nifty Infra",
    "Healthcare":         "NSE_INDEX|Nifty Healthcare",
    "Consumer Durables":  "NSE_INDEX|Nifty Consr Durable",
    "Oil & Gas":          "NSE_INDEX|Nifty Oil & Gas",
    "Chemicals":          "NSE_INDEX|Nifty Chemicals",
    "Commodities":        "NSE_INDEX|Nifty Commodities",
    "MNC":                "NSE_INDEX|Nifty MNC",
    "Consumption":        "NSE_INDEX|Nifty India Consumption",
    "Manufacturing":      "NSE_INDEX|Nifty India Manufacturing",
    "CPSE":               "NSE_INDEX|Nifty CPSE",
    "Digital":            "NSE_INDEX|Nifty India Digital",
    "EV & New Age Auto":  "NSE_INDEX|Nifty EV & New Age Automotive",
    "Tourism":            "NSE_INDEX|Nifty India Tourism",
    "Capital Markets":    "NSE_INDEX|Nifty Capital Markets",
    "Housing":            "NSE_INDEX|Nifty Housing",
    "Railways":           "NSE_INDEX|Nifty India Railways PSU",
    "Internet":           "NSE_INDEX|Nifty India Internet",
    "Rural":              "NSE_INDEX|Nifty Rural",
    "Services":           "NSE_INDEX|Nifty Services Sector",
    "REITs & InvITs":     "NSE_INDEX|Nifty REITs & InvITs",
    "Mobility":           "NSE_INDEX|Nifty Mobility",
    "Infra & Logistics":  "NSE_INDEX|Nifty India Infrastructure & Logistics",
    "Transport & Logistics": "NSE_INDEX|Nifty Transportation & Logistics",
    "IPO":                "NSE_INDEX|Nifty IPO",
}

# Cache for all index historical data
index_history_cache: dict = {}  # name -> {prices, volumes}

# ── Seeded Nifty 50 history (2020-2025, 1492 days) ────────────────────
# Uploaded from NSE bhavcopy CSVs — used to bootstrap RS accuracy
NIFTY50_SEED_PRICES = [12182.5, 12282.2, 12226.65, 11993.05, 12052.95, 12025.35, 12215.9, 12256.8, 12329.55, 12362.3, 12343.3, 12355.5, 12352.35, 12224.55, 12169.85, 12106.9, 12180.35, 12248.25, 12119.0, 12055.8, 12129.5, 12035.8, 11962.1, 11661.85, 11707.9, 11979.65, 12089.15, 12137.95, 12098.35, 12031.5, 12107.9, 12201.2, 12174.65, 12113.45, 12045.8, 11992.5, 12125.9, 12080.85, 11829.4, 11797.9, 11678.5, 11633.3, 11201.75, 11132.75, 11303.3, 11251.0, 11269.0, 10989.45, 10451.45, 10458.4, 9590.15, 9955.2, 9197.4, 8967.05, 8468.8, 8263.45, 8745.45, 7610.25, 7801.05, 8317.85, 8641.45, 8660.25, 8281.1, 8597.75, 8253.8, 8083.8, 8792.2, 8748.75, 9111.9, 8993.85, 8925.3, 8992.8, 9266.75, 9261.85, 8981.45, 9187.3, 9313.9, 9154.4, 9282.3, 9380.9, 9553.35, 9859.9, 9293.5, 9205.6, 9270.9, 9199.05, 9251.5, 9239.2, 9196.55, 9383.55, 9142.75, 9136.85, 8823.25, 8879.1, 9066.55, 9106.25, 9039.25, 9029.05, 9314.95, 9490.1, 9580.3, 9826.15, 9979.1, 10061.55, 10029.1, 10142.15, 10167.45, 10046.65, 10116.15, 9902.0, 9972.9, 9813.7, 9914.0, 9881.15, 10091.65, 10244.4, 10311.2, 10471.0, 10305.3, 10288.9, 10383.0, 10312.4, 10302.1, 10430.05, 10551.7, 10607.35, 10763.65, 10799.65, 10705.75, 10813.45, 10768.05, 10802.7, 10607.35, 10618.2, 10739.95, 10901.7, 11022.2, 11162.25, 11132.6, 11215.45, 11194.15, 11131.8, 11300.55, 11202.85, 11102.15, 11073.45, 10891.6, 11095.25, 11101.65, 11200.15, 11214.05, 11270.15, 11322.5, 11308.4, 11300.45, 11178.4, 11247.1, 11385.35, 11408.4, 11312.2, 11371.6, 11466.45, 11472.25, 11549.6, 11559.25, 11647.6, 11387.5, 11470.25, 11535.0, 11527.45, 11333.85, 11355.05, 11317.35, 11278.0, 11449.25, 11464.45, 11440.05, 11521.8, 11604.55, 11516.1, 11504.95, 11250.55, 11153.65, 11131.85, 10805.55, 11050.25, 11227.55, 11222.4, 11247.55, 11416.95, 11503.35, 11662.4, 11738.85, 11834.6, 11914.2, 11930.95, 11934.5, 11971.05, 11680.35, 11762.45, 11873.05, 11896.8, 11937.65, 11896.45, 11930.35, 11767.75, 11889.4, 11729.6, 11670.8, 11642.4, 11669.15, 11813.5, 11908.5, 12120.3, 12263.55, 12461.05, 12631.1, 12749.15, 12690.8, 12719.95, 12780.25, 12874.2, 12938.25, 12771.7, 12859.05, 12926.45, 13055.15, 12858.4, 12987.0, 12968.95, 13109.05, 13113.75, 13133.9, 13258.55, 13355.75, 13392.95, 13529.1, 13478.3, 13513.85, 13558.15, 13567.85, 13682.7, 13740.7, 13760.55, 13328.4, 13466.3, 13601.1, 13749.25, 13873.2, 13932.6, 13981.95, 13981.75, 14018.5, 14132.9, 14199.5, 14146.25, 14137.35, 14347.25, 14484.75, 14563.45, 14564.85, 14595.6, 14433.7, 14281.3, 14521.15, 14644.7, 14590.35, 14371.9, 14238.9, 13967.5, 13817.55, 13634.6, 14281.2, 14647.85, 14789.95, 14895.65, 14924.25, 15115.8, 15109.3, 15106.5, 15173.3, 15163.3, 15314.7, 15313.45, 15208.9, 15118.95, 14981.75, 14675.7, 14707.8, 14982.0, 15097.35, 14529.15, 14761.55, 14919.1, 15245.6, 15080.75, 14938.1, 14956.2, 15098.4, 15174.8, 15030.95, 14929.5, 14910.45, 14721.3, 14557.85, 14744.0, 14736.4, 14814.75, 14549.4, 14324.9, 14507.3, 14845.1, 14690.7, 14867.35, 14637.8, 14683.5, 14819.05, 14873.8, 14834.85, 14310.8, 14504.8, 14581.45, 14617.85, 14359.45, 14296.4, 14406.15, 14341.35, 14485.0, 14653.05, 14864.55, 14894.9, 14631.1, 14634.15, 14496.5, 14617.85, 14724.8, 14823.15, 14942.35, 14850.75, 14696.5, 14677.8, 14923.15, 15108.1, 15030.15, 14906.05, 15175.3, 15197.7, 15208.45, 15301.45, 15337.85, 15435.65, 15582.8, 15574.85, 15576.2, 15690.35, 15670.25, 15751.65, 15740.1, 15635.35, 15737.75, 15799.35, 15811.85, 15869.25, 15767.55, 15691.4, 15683.35, 15746.5, 15772.75, 15686.95, 15790.45, 15860.35, 15814.7, 15748.45, 15721.5, 15680.0, 15722.2, 15834.35, 15818.25, 15879.65, 15727.9, 15689.8, 15692.6, 15812.35, 15853.95, 15924.2, 15923.4, 15752.4, 15632.1, 15824.05, 15856.05, 15824.45, 15746.45, 15709.4, 15778.45, 15763.05, 15885.15, 16130.75, 16258.8, 16294.6, 16238.2, 16258.25, 16280.1, 16282.25, 16364.4, 16529.1, 16563.05, 16614.6, 16568.85, 16450.5, 16496.45, 16624.6, 16634.65, 16636.9, 16705.2, 16931.05, 17132.2, 17076.25, 17234.15, 17323.6, 17377.8, 17362.1, 17353.5, 17369.25, 17355.3, 17380.0, 17519.45, 17629.5, 17585.15, 17396.9, 17562.0, 17546.65, 17822.95, 17853.2, 17855.1, 17748.6, 17711.3, 17618.15, 17532.05, 17691.25, 17822.3, 17646.0, 17790.35, 17895.2, 17945.95, 17991.95, 18161.75, 18338.55, 18477.05, 18418.75, 18266.6, 18178.1, 18114.9, 18125.4, 18268.4, 18210.95, 17857.25, 17671.65, 17929.65, 17888.95, 17829.2, 17916.8, 18068.55, 18044.25, 18017.2, 17873.6, 18102.75, 18109.45, 17999.2, 17898.65, 17764.8, 17416.55, 17503.35, 17415.05, 17536.25, 17026.45, 17053.95, 16983.2, 17166.9, 17401.65, 17196.7, 16912.25, 17176.7, 17469.75, 17516.85, 17511.3, 17368.25, 17324.9, 17221.4, 17248.4, 16985.2, 16614.2, 16770.85, 16955.45, 17072.6, 17003.75, 17086.25, 17233.25, 17213.6, 17203.95, 17354.05, 17625.7, 17805.25, 17925.25, 17745.9, 17812.7, 18003.3, 18055.75, 18212.35, 18257.8, 18255.75, 18308.1, 18113.05, 17938.4, 17757.0, 17617.15, 17149.1, 17277.95, 17110.15, 17101.95, 17339.85, 17576.85, 17780.0, 17560.2, 17516.3, 17213.6, 17266.75, 17463.8, 17605.85, 17374.75, 16842.8, 17352.45, 17322.2, 17304.6, 17276.3, 17206.65, 17092.2, 17063.25, 16247.95, 16658.4, 16793.9, 16605.95, 16498.05, 16245.35, 15863.15, 16013.45, 16345.35, 16594.9, 16630.45, 16871.3, 16663.0, 16975.35, 17287.05, 17117.6, 17315.5, 17245.65, 17222.75, 17153.0, 17222.0, 17325.3, 17498.25, 17464.75, 17670.45, 18053.4, 17957.4, 17807.65, 17639.55, 17784.35, 17674.95, 17530.3, 17475.65, 17173.65, 16958.65, 17136.55, 17392.6, 17171.95, 16953.95, 17200.8, 17038.4, 17245.05, 17102.55, 17069.1, 16677.6, 16682.65, 16411.25, 16301.85, 16240.05, 16167.1, 15808.0, 15782.15, 15842.3, 16259.3, 16240.3, 15809.4, 16266.15, 16214.7, 16125.15, 16025.8, 16170.15, 16352.45, 16661.4, 16584.55, 16522.75, 16628.0, 16584.3, 16569.55, 16416.35, 16356.25, 16478.1, 16201.8, 15774.4, 15732.1, 15692.15, 15360.6, 15293.5, 15350.15, 15638.8, 15413.3, 15556.65, 15699.25, 15832.05, 15850.2, 15799.1, 15780.25, 15752.05, 15835.35, 15810.85, 15989.8, 16132.9, 16220.6, 16216.0, 16058.3, 15966.65, 15938.65, 16049.2, 16278.5, 16340.55, 16520.85, 16605.25, 16719.45, 16631.0, 16483.85, 16641.8, 16929.6, 17158.25, 17340.05, 17345.45, 17388.15, 17382.0, 17397.5, 17525.1, 17534.75, 17659.0, 17698.15, 17825.25, 17944.25, 17956.5, 17758.45, 17490.7, 17577.5, 17604.95, 17522.45, 17558.9, 17312.9, 17759.3, 17542.8, 17539.45, 17665.8, 17655.6, 17624.4, 17798.75, 17833.35, 17936.35, 18070.05, 18003.75, 17877.4, 17530.85, 17622.25, 17816.25, 17718.35, 17629.8, 17327.35, 17016.3, 17007.4, 16858.6, 16818.1, 17094.35, 16887.35, 17274.3, 17331.8, 17314.65, 17241.0, 16983.55, 17123.6, 17014.35, 17185.7, 17311.8, 17486.95, 17512.25, 17563.95, 17576.3, 17730.75, 17656.35, 17736.95, 17786.8, 18012.2, 18145.4, 18082.85, 18052.7, 18117.15, 18202.8, 18157.0, 18028.2, 18349.7, 18329.15, 18403.4, 18409.65, 18343.9, 18307.65, 18159.95, 18244.2, 18267.25, 18484.1, 18512.75, 18562.75, 18618.05, 18758.35, 18812.5, 18696.1, 18701.05, 18642.75, 18560.5, 18609.35, 18496.6, 18497.15, 18608.0, 18660.3, 18414.9, 18269.0, 18420.45, 18385.3, 18199.1, 18127.35, 17806.8, 18014.6, 18132.3, 18122.5, 18191.0, 18105.3, 18197.45, 18232.55, 18042.95, 17992.15, 17859.45, 18101.2, 17914.15, 17895.7, 17858.2, 17956.6, 17894.85, 18053.3, 18165.35, 18107.85, 18027.65, 18118.55, 18118.3, 17891.95, 17604.35, 17648.95, 17662.15, 17616.3, 17610.4, 17854.05, 17764.6, 17721.5, 17871.7, 17893.45, 17856.5, 17770.9, 17929.85, 18015.85, 18035.85, 17944.2, 17844.6, 17826.7, 17554.3, 17511.25, 17465.8, 17392.7, 17303.95, 17450.9, 17321.9, 17594.35, 17711.45, 17754.4, 17589.6, 17412.9, 17154.3, 17043.3, 16972.15, 16985.6, 17100.05, 16988.4, 17107.5, 17151.9, 17076.9, 16945.05, 16985.7, 16951.7, 17080.7, 17359.75, 17398.05, 17557.05, 17599.15, 17624.05, 17722.3, 17812.4, 17828.0, 17706.85, 17660.15, 17618.75, 17624.45, 17624.05, 17743.4, 17769.25, 17813.6, 17915.05, 18065.0, 18147.65, 18089.85, 18255.8, 18069.0, 18264.4, 18265.95, 18315.1, 18297.0, 18314.8, 18398.85, 18286.5, 18181.75, 18129.95, 18203.4, 18314.4, 18348.0, 18285.4, 18321.15, 18499.35, 18598.65, 18633.85, 18534.4, 18487.75, 18534.1, 18593.85, 18599.0, 18726.4, 18634.55, 18563.4, 18601.5, 18716.15, 18755.9, 18688.1, 18826.0, 18755.45, 18816.7, 18856.85, 18771.25, 18665.5, 18691.2, 18817.4, 18972.1, 19189.05, 19322.55, 19389.0, 19398.5, 19497.3, 19331.8, 19355.9, 19439.4, 19384.3, 19413.75, 19564.5, 19711.45, 19749.25, 19833.15, 19979.15, 19745.0, 19672.35, 19680.6, 19778.3, 19659.9, 19646.05, 19753.8, 19733.55, 19526.55, 19381.65, 19517.0, 19597.3, 19570.85, 19632.55, 19543.1, 19428.3, 19434.55, 19465.0, 19365.25, 19310.15, 19393.6, 19396.45, 19444.0, 19386.7, 19265.8, 19306.05, 19342.65, 19347.45, 19253.8, 19435.3, 19528.8, 19574.9, 19611.05, 19727.05, 19819.95, 19996.35, 19993.2, 20070.0, 20103.1, 20192.35, 20133.3, 19901.4, 19742.35, 19674.25, 19674.55, 19664.7, 19716.45, 19523.55, 19638.3, 19528.75, 19436.1, 19545.75, 19653.5, 19512.35, 19689.85, 19811.35, 19794.0, 19751.05, 19731.75, 19811.5, 19671.1, 19624.7, 19542.65, 19281.75, 19122.15, 18857.25, 19047.25, 19140.9, 19079.6, 18989.15, 19133.25, 19230.6, 19411.75, 19406.7, 19443.5, 19395.3, 19425.35, 19525.55, 19443.55, 19675.45, 19765.2, 19731.8, 19694.0, 19783.4, 19811.85, 19802.0, 19794.7, 19889.7, 20096.6, 20133.15, 20267.9, 20686.8, 20855.1, 20937.7, 20901.15, 20969.4, 20997.1, 20906.4, 20926.35, 21182.7, 21456.65, 21418.65, 21453.1, 21150.15, 21255.05, 21349.4, 21441.35, 21654.75, 21778.7, 21731.4, 21741.9, 21665.8, 21517.35, 21658.6, 21710.8, 21513.0, 21544.85, 21618.7, 21647.2, 21894.55, 22097.45, 22032.3, 21571.95, 21462.25, 21622.4, 21571.8, 21238.8, 21453.95, 21352.6, 21737.6, 21522.1, 21725.7, 21697.45, 21853.8, 21771.7, 21929.4, 21930.5, 21717.95, 21782.5, 21616.05, 21743.25, 21840.05, 21910.75, 22040.7, 22122.25, 22196.95, 22055.05, 22217.45, 22212.7, 22122.05, 22198.35, 21951.15, 21982.8, 22338.75, 22378.4, 22405.6, 22356.3, 22474.05, 22493.55, 22332.65, 22335.7, 21997.7, 22146.65, 22023.35, 22055.7, 21817.45, 21839.1, 22011.95, 22096.75, 22004.7, 22123.65, 22326.9, 22462.0, 22453.3, 22434.65, 22514.65, 22513.7, 22666.3, 22642.75, 22753.8, 22519.4, 22272.5, 22147.9, 21995.85, 22147.0, 22336.4, 22368.0, 22402.4, 22570.35, 22419.95, 22643.4, 22604.85, 22648.2, 22475.85, 22442.7, 22302.5, 22302.5, 21957.5, 22055.2, 22104.05, 22217.85, 22200.55, 22403.85, 22466.1, 22502.0, 22529.05, 22597.8, 22967.65, 22957.1, 22932.45, 22888.15, 22704.7, 22488.65, 22530.7, 23263.9, 21884.5, 22620.35, 22821.4, 23290.15, 23259.2, 23264.85, 23322.95, 23398.9, 23465.6, 23557.9, 23516.0, 23567.0, 23501.1, 23537.85, 23721.3, 23868.8, 24044.5, 24010.6, 24141.95, 24123.85, 24286.5, 24302.15, 24323.85, 24320.55, 24433.2, 24324.45, 24315.95, 24502.15, 24586.7, 24613.0, 24800.85, 24530.9, 24509.25, 24479.05, 24413.5, 24406.1, 24834.85, 24836.1, 24857.3, 24951.15, 25010.9, 24717.7, 24055.6, 23992.55, 24297.5, 24117.0, 24367.5, 24347.0, 24139.0, 24143.75, 24541.15, 24572.65, 24698.85, 24770.2, 24811.5, 24823.15, 25010.6, 25017.75, 25052.35, 25151.95, 25235.9, 25278.7, 25279.85, 25198.7, 25145.1, 24852.15, 24936.4, 25041.1, 24918.45, 25388.9, 25356.5, 25383.75, 25418.55, 25377.55, 25415.8, 25790.95, 25939.05, 25940.4, 26004.15, 26216.05, 26178.95, 25810.85, 25796.9, 25250.1, 25014.6, 24795.75, 25013.15, 24981.95, 24998.45, 24964.25, 25127.95, 25057.35, 24971.3, 24749.85, 24854.05, 24781.1, 24472.1, 24435.5, 24399.4, 24180.8, 24339.15, 24466.85, 24340.85, 24205.35, 24304.35, 23995.35, 24213.3, 24484.05, 24199.35, 24148.2, 24141.3, 23883.45, 23559.05, 23532.7, 23453.8, 23518.5, 23349.9, 23907.25, 24221.9, 24194.5, 24274.9, 23914.15, 24131.1, 24276.05, 24457.15, 24467.45, 24708.4, 24677.8, 24619.0, 24610.05, 24641.8, 24548.7, 24768.3, 24668.25, 24336.0, 24198.85, 23951.7, 23587.5, 23753.45, 23727.65, 23750.2, 23813.4, 23644.9, 23644.8, 23742.9, 24188.65, 24004.75, 23616.05, 23707.9, 23688.95, 23526.5, 23431.5, 23085.95, 23176.05, 23213.2, 23311.8, 23203.2, 23344.75, 23024.65, 23155.35, 23205.35, 23092.2, 22829.15, 22957.25, 23163.1, 23249.5, 23508.4, 23482.15, 23361.05, 23739.25, 23696.3, 23603.35, 23559.95, 23381.6, 23071.8, 23045.25, 23031.4, 22929.25, 22959.5, 22945.3, 22932.9, 22913.15, 22795.9, 22553.35, 22547.55, 22545.05, 22124.7, 22119.3, 22082.65, 22337.3, 22544.7, 22552.5, 22460.3, 22497.9, 22470.5, 22397.2, 22508.75, 22834.3, 22907.6, 23190.65, 23350.4, 23658.35, 23668.65, 23486.85, 23591.95, 23519.35, 23165.7, 23332.35, 23250.1, 22904.45, 22161.6, 22535.85, 22399.15, 22828.55, 23328.55, 23437.2, 23851.65, 24125.55, 24167.25, 24328.95, 24246.7, 24039.35, 24328.5, 24335.95, 24334.2, 24346.7, 24461.15, 24379.6, 24414.4, 24273.8, 24008.0, 24924.7, 24578.35, 24666.9, 25062.1, 25019.8, 24945.45, 24683.9, 24813.45, 24609.7, 24853.15, 25001.15, 24826.2, 24752.45, 24833.6, 24750.7, 24716.6, 24542.5, 24620.2, 24750.9, 25003.05, 25103.2, 25104.25, 25141.4, 24888.2, 24718.6, 24946.5, 24853.4, 24812.05, 24793.25, 25112.4, 24971.9, 25044.35, 25244.75, 25549.0, 25637.8, 25517.05, 25541.8, 25453.4, 25405.3, 25461.0, 25461.3, 25522.5, 25476.1, 25355.25, 25149.85, 25082.3, 25195.8, 25212.05, 25111.45, 24968.4, 25090.7, 25060.9, 25219.9, 25062.1, 24837.0, 24680.9, 24821.1, 24855.05, 24768.35, 24565.35, 24722.75, 24649.55, 24574.2, 24596.15, 24363.3, 24585.05, 24487.4, 24619.35, 24631.3, 24876.95, 24980.65, 25050.55, 25083.75, 24870.1, 24967.75, 24712.05, 24500.9, 24426.85, 24625.05, 24579.6, 24715.05, 24734.3, 24741.0, 24773.15, 24868.6, 24973.1, 25005.5, 25114.0, 25069.2, 25239.1, 25330.25, 25423.6, 25327.05, 25202.35, 25169.5, 25056.9, 24890.85, 24654.7, 24634.9, 24611.1, 24836.3, 24894.25, 25077.65, 25108.3, 25046.15, 25181.8, 25285.35, 25227.35, 25145.5, 25323.55, 25585.3, 25709.85, 25843.15, 25868.6, 25891.4, 25795.15, 25966.05, 25936.2, 26053.9, 25877.85, 25722.1, 25763.35, 25597.65, 25509.7, 25492.3, 25574.35, 25694.95, 25875.8, 25879.15, 25910.05, 26013.45, 25910.05, 26052.65, 26192.15, 26068.15, 25959.5, 25884.8, 26205.3, 26215.55, 26202.95, 26175.75, 26032.2, 25986.0, 26033.75, 26186.45, 25960.55, 25839.65, 25758.0, 25898.55, 26046.95, 26027.3, 25860.1, 25818.55, 25815.55, 25966.4, 26172.4, 26177.15, 26142.1, 26042.3, 25942.1, 25938.85, 26129.6]

# ── Full 2yr history → Supabase (all stocks, at startup) ──────────────
instrument_key_map: dict = {} # sym -> full instrument key (e.g. NSE_EQ|INE002A01018)
index_key_map: dict = {}     # normalized index name -> instrument key (e.g. NSE_INDEX|...) —
                              # built alongside instrument_key_map in load_instrument_master,
                              # replaces guessing text variants for thematic sector indices

# ── Main scan function ────────────────────────────────────────────────
# ============================================================
# AI Best Picks — composite technical+fundamental scoring, with an
# AI-generated (or free templated) rationale for the top candidates.
# Recomputed at most once per _AI_PICKS_REFRESH_INTERVAL_SEC from
# inside run_scan, since `processed` already has every technical AND
# fundamental field merged per stock by the time that function
# finishes — no extra fetching needed here.
# ============================================================
_LAST_AI_PICKS_TS = 0.0
_zero_chg_debug_count = 0  # reset each run_scan cycle — caps zero-chg diagnostic logging
_LAST_FUNDAMENTALS_SYNC_TS = 0.0
_AI_PICKS_REFRESH_INTERVAL_SEC = 3600  # ranking + rationale refresh at most hourly
_AI_PICKS_TOP_N = 30

# ── Announcement enrichment + 1-month history backfill ──────────────────
_ANN_POSITIVE_PATTERNS = [
    'award of order', 'work order', 'purchase order', 'order worth', 'order valued',
    'bagged', 'bagging', 'receiving of order', 'receipt of order', 'receiving of contract',
    'receipt of contract', 'secures order', 'secured order', 'wins order', 'won order', 'new order',
    'letter of intent', 'contract awarded', 'awarded a contract', 'received an order',
    'order received from', 'capacity expansion', 'commercial production', 'usfda',
    'approval received', 'patent granted', 'buyback', 'bonus issue', 'stock split',
    'reduced to nil', 'in favour of the company', 'in favor of the company',
    'settled in favour', 'demand quashed', 'preferential allotment completed',
]
_ANN_NEGATIVE_PATTERNS = [
    'resignation of', 'resigned', 'penalty', 'show cause', 'gst demand', 'tax demand',
    'demand order', 'search and seizure', 'default', 'downgrade', 'fire at', 'accident at',
    'plant shutdown', 'suspension of operations', 'insolvency', 'nclt admission',
    'fraud', 'auditor has resigned', 'pledge of shares', 'shares pledged',
    'disqualified', 'sebi order against', 'debarred',
    'cancellation of order', 'cancellation of work order', 'cancellation of purchase order',
    'order cancelled', 'work order cancelled', 'purchase order cancelled',
    'cancellation of contract', 'contract cancelled', 'termination of contract',
    'contract terminated', 'termination of work order', 'work order terminated',
    'rescission of', 'order rescinded', 'contract rescinded', 'order withdrawn',
    'withdrawal of order', 'loss of order', 'order lost', 'annulment of',
]
# Order-win phrases (_ORDER_WIN_PATTERNS below) can appear inside the text of
# an order CANCELLATION too — e.g. "Cancellation of Work Order by Reliance
# Industries Limited" contains "work order". Any row matching one of these
# cancel/termination phrases must never be treated as an order win, no matter
# what else it contains. Kept in sync with _ANN_NEGATIVE_PATTERNS' cancel
# terms above (a superset — this one doesn't need the resignation/penalty/etc
# entries that are irrelevant to order-win detection).
_ORDER_CANCEL_PATTERNS = [
    'cancellation of order', 'cancellation of work order', 'cancellation of purchase order',
    'order cancelled', 'work order cancelled', 'purchase order cancelled',
    'cancellation of contract', 'contract cancelled', 'termination of contract',
    'contract terminated', 'termination of work order', 'work order terminated',
    'rescission of', 'order rescinded', 'contract rescinded', 'order withdrawn',
    'withdrawal of order', 'loss of order', 'order lost', 'annulment of',
]

_ORDER_WIN_PATTERNS = [
    'award of order', 'work order', 'purchase order', 'order worth', 'order valued',
    'bagged', 'bagging', 'receiving of order', 'receipt of order', 'receiving of contract',
    'receipt of contract', 'secures order', 'secured order', 'wins order', 'won order', 'new order',
    'letter of intent', 'contract awarded', 'awarded a contract', 'received an order',
    'order received from',
]

# ── Results-from-announcement: the actual intent here is "when a
# results-type announcement lands, go read that stock's numbers" —
# not a separate scheduled poll against NSE's results-list feed, which
# turned out to only reliably support single-day queries and made
# discovery unnecessarily fragile. Announcements already carry the
# reporting period in plain English ("...for the quarter ended June
# 30, 2026"), and the announcements feed itself has been reliable all
# session, so use it as the trigger and only call the per-symbol
# numbers endpoint (which was never the broken part) directly.
_results_attempt_times = {}  # (symbol, period_ended) -> unix timestamp of last attempt,
                              # shared between this announcement-driven trigger and
                              # _results_loop below so neither re-attempts a fetch the
                              # other just tried. Confirmed necessary via production log:
                              # without it, a still-unpublished quarter's numbers get
                              # re-requested every single 5-min cycle indefinitely.
_RESULTS_ATTEMPT_COOLDOWN_SEC = 1800  # 30 min between retries of the same (symbol, period)
_RESULTS_ANN_KEYWORDS = ['financial result', 'quarterly result', 'results for the quarter',
                         'unaudited results', 'audited results']
_RESULTS_ANN_EXCLUDE = ['newspaper publication', 'newspaper advertisement', 'transcript', 'press release',
                        'investor meet', 'con. call', 'con call', 'conference call']
_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

# ── Direct XBRL parsing — a company's real filed numbers, structured,
# available the moment the filing lands, rather than waiting on NSE's
# separate results-comparision API to sync (confirmed via production
# log: that sync can lag many hours behind the public announcement —
# COFORGE's June-2026 numbers stayed unavailable there for 7+ hours
# after the press release went public with the same figures). The
# announcements feed only gives a hasXbrl boolean and the PDF link, not
# the XBRL file itself — the real link lives in the results-LIST feed's
# 'xbrl' field (see fetch_nse_financial_results), so this cross-
# references that feed by symbol+period first.
# Candidate XBRL element local-names (namespace-agnostic — matched by
# tag name only, ignoring the namespace prefix, since different filers
# can use different taxonomy namespaces for conceptually the same
# concept). These are best-effort guesses at standard Ind-AS/SEBI XBRL
# taxonomy names; logged verbatim on first use so they can be
# verified/corrected against a real filing, same pattern used for
# every other NSE field-mapping in this file.
_XBRL_SALES_TAGS = ['RevenueFromOperations', 'Revenue', 'TotalIncome', 'IncomeFromOperations']
_XBRL_OTHER_INCOME_TAGS = ['OtherIncome']
_XBRL_PBT_TAGS = ['ProfitBeforeExceptionalItemsAndTax', 'ProfitBeforeTax',
                  'ProfitLossBeforeExceptionalItemsAndTax', 'ProfitLossBeforeTax',
                  'ProfitBeforeTaxAndExceptionalItems']
_XBRL_PAT_TAGS = ['ProfitLossForPeriod', 'ProfitLoss', 'NetProfitLoss',
                  'ProfitLossForPeriodFromContinuingOperations']
_XBRL_EPS_TAGS = ['BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations',
                  'BasicEarningsPerShare', 'BasicEPS']

# ── NSE quarterly financial results (structured, no PDF parsing) ────────
# ── Main loop ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    asyncio.run(main())


# ══ SHARED FUNCTIONS (used by both services) ══

def _load_sector_industry_lookup() -> dict:
    """One-time load of the static sector/industry lookup table (built
    from a broad market-cap sweep, ~2,355 symbols after dropping indices/
    ETFs) into memory at import time. This is a far more reliable source
    than fetch_fundamentals_screener()'s live scrape — that scraper has a
    documented ~98% blank-response rate (Railway IP rate-limiting) and,
    as of the last review, its sector/industry regex patterns don't even
    match screener.in's current markup, so it was never actually
    populating 'industry' in practice. This file needs periodic manual
    refreshes (sector/industry classifications drift slowly, so this
    doesn't need to be live), but the coverage is dramatically better
    than SECTOR_MAP's ~150 hand-curated symbols, and it's zero extra API
    calls or scraping.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'sector_industry_lookup.csv')
    lookup = {}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sym = (row.get('symbol') or '').strip()
                if sym:
                    lookup[sym] = {
                        'industry': (row.get('industry') or '').strip() or None,
                        'sector':   (row.get('sector') or '').strip() or None,
                    }
        log.info(f"✅ Loaded sector/industry lookup: {len(lookup)} symbols from {path}")
    except Exception as e:
        log.warning(f"Sector/industry lookup load failed ({path}): {e} — falling back to SECTOR_MAP + live fetch only")
    return lookup

async def ensure_fundamentals_table(session: aiohttp.ClientSession,
                                     retries: int = 6, delay: float = 10.0) -> bool:
    """Same self-healing pattern as ensure_full_history_table — see that
    function for why the retry loop is needed (PostgREST schema cache lag
    after creating a table via the SQL Editor)."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    last_status = None
    last_body = ""
    for attempt in range(1, retries + 1):
        try:
            async with session.get(
                f"{SUPABASE_URL}/rest/v1/stock_fundamentals?select=sym&limit=1",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    log.info("✅ stock_fundamentals table OK"
                              + (f" (after {attempt} attempt(s))" if attempt > 1 else ""))
                    return True
                last_status = r.status
                last_body = await r.text()
        except Exception as e:
            last_status = None
            last_body = str(e)
        if attempt < retries:
            log.warning(f"stock_fundamentals not ready yet (attempt {attempt}/{retries}, "
                        f"status={last_status}) — retrying in {delay:.0f}s…")
            await asyncio.sleep(delay)

    log.error("❌ stock_fundamentals table MISSING or misconfigured (after retries)!")
    log.error(f"   status={last_status} body={last_body[:200]}")
    log.error("   → Go to Supabase SQL Editor and run:")
    log.error("   create table if not exists public.stock_fundamentals (")
    log.error("     sym text primary key,")
    log.error("     market_cap numeric, pe numeric, roe numeric,")
    log.error("     eps numeric, debt_eq numeric, promoter numeric,")
    log.error("     eps_qoq numeric, eps_yoy numeric, sales_qoq numeric, sales_yoy numeric,")
    log.error("     opm_pct numeric, opm_trend numeric, eps_growth_streak int,")
    log.error("     fii_pct numeric, fii_trend numeric, dii_pct numeric, dii_trend numeric,")
    log.error("     promoter_trend numeric, peg_ratio numeric,")
    log.error("     fetched_at timestamptz")
    log.error("   );")
    log.error("   → If the table already exists from before, instead run:")
    log.error("   alter table public.stock_fundamentals")
    log.error("     add column if not exists eps_qoq numeric,")
    log.error("     add column if not exists eps_yoy numeric,")
    log.error("     add column if not exists sales_qoq numeric,")
    log.error("     add column if not exists sales_yoy numeric,")
    log.error("     add column if not exists opm_pct numeric,")
    log.error("     add column if not exists opm_trend numeric,")
    log.error("     add column if not exists eps_growth_streak int,")
    log.error("     add column if not exists fii_pct numeric,")
    log.error("     add column if not exists fii_trend numeric,")
    log.error("     add column if not exists dii_pct numeric,")
    log.error("     add column if not exists dii_trend numeric,")
    log.error("     add column if not exists promoter_trend numeric,")
    log.error("     add column if not exists peg_ratio numeric;")
    return False

async def fetch_fundamentals_screener(session: aiohttp.ClientSession, sym: str, debug: bool = False) -> dict:
    """
    Scrape fundamental data from Screener.in company page.
    Free, no auth needed. Returns two families of data:
    - Snapshot ratios: market_cap, pe, roe, eps, debt_eq, promoter
    - Trend data (from the Quarterly Results + Shareholding Pattern tables):
      eps_qoq/eps_yoy (earnings growth — the core CANSLIM signal),
      sales_qoq/sales_yoy, opm_pct/opm_trend (margin + direction),
      eps_growth_streak (consecutive quarters of EPS growth),
      fii_pct/fii_trend, dii_pct/dii_trend, promoter_trend, peg_ratio

    NOTE: ~98% of fetches were coming back blank in earlier testing — far
    too high to be individual page-structure mismatches, more consistent
    with Screener.in rate-limiting/blocking requests from Railway's
    datacenter IP. A single hardcoded User-Agent with minimal headers is
    itself a bot-detection signal, so this rotates between several
    realistic full browser header sets (UA + Accept-Language + sec-ch-ua
    etc.) to look more like normal traffic.
    """
    url = f"https://www.screener.in/company/{sym}/consolidated/"
    headers = random.choice(_SCREENER_HEADER_SETS)
    result = {
        'market_cap': None, 'pe': None, 'roe': None, 'eps': None, 'debt_eq': None, 'promoter': None,
        'eps_qoq': None, 'eps_yoy': None, 'sales_qoq': None, 'sales_yoy': None,
        'opm_pct': None, 'opm_trend': None, 'eps_growth_streak': None,
        'fii_pct': None, 'fii_trend': None, 'dii_pct': None, 'dii_trend': None,
        'promoter_trend': None, 'peg_ratio': None, 'industry': None,
        'shares_outstanding': None,
    }
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if sym == 'TARSONS' or debug:
                log.info(f"  🔍 {sym} fetch: url={url}, status={r.status}")
            if r.status == 404:
                # Try standalone (non-consolidated)
                url2 = f"https://www.screener.in/company/{sym}/"
                async with session.get(url2, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    if sym == 'TARSONS' or debug:
                        log.info(f"  🔍 {sym} fallback fetch: url={url2}, status={r2.status}")
                    if r2.status != 200:
                        return result
                    html = await r2.text()
            elif r.status != 200:
                if r.status in (429, 503):
                    log.warning(f"  ⚠️ {sym}: status {r.status} — looks like rate-limiting/blocking, not a normal error")
                if debug:
                    log.info(f"  🔍 {sym}: non-200/404 status ({r.status}), returning blank result")
                return result
            else:
                html = await r.text()

        if sym == 'TARSONS' or debug:
            log.info(f"  🔍 {sym} html length={len(html)}, "
                     f"has_market_cap_text={'Market Cap' in html}, "
                     f"has_eps_row={'EPS in Rs' in html}, "
                     f"has_captcha_or_challenge={'captcha' in html.lower() or 'cloudflare' in html.lower() or 'cf-' in html.lower()}, "
                     f"html_snippet={html[:200]!r}")

        import re

        def extract_ratio(label: str, html: str) -> str:
            # Screener renders ratios like: <li>...<span class="name">Market Cap</span><span class="nowrap">₹ 1,234 Cr.</span>
            pattern = rf'{re.escape(label)}.*?<span[^>]*nowrap[^>]*>(.*?)</span>'
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                return re.sub(r'<[^>]+>', '', m.group(1)).strip()
            return None

        def parse_number(s: str):
            if not s:
                return None
            s = s.replace('₹', '').replace('%', '').replace(',', '').replace('Cr.','').replace('Cr','').strip()
            try:
                return float(s)
            except Exception:
                return None

        result['market_cap'] = parse_number(extract_ratio('Market Cap', html))
        result['pe']         = parse_number(extract_ratio('Stock P/E', html))
        result['roe']        = parse_number(extract_ratio('ROE', html))
        result['eps']        = parse_number(extract_ratio('EPS', html))
        result['debt_eq']    = parse_number(extract_ratio('Debt to equity', html))

        # Shares outstanding = market cap ÷ Screener's own "Current Price"
        # snapshot at scrape time. This is the key to keeping market cap
        # and P/E fresh WITHOUT re-scraping daily: shares outstanding
        # barely changes (only on a fresh issue/buyback), so once known,
        # run_scan can recompute market_cap = shares_outstanding × TODAY's
        # live price every single cycle — effectively daily-fresh, at
        # zero extra Screener.in requests. Only re-derived when this
        # monthly scrape actually runs.
        scrape_price = parse_number(extract_ratio('Current Price', html))
        if result['market_cap'] and scrape_price and scrape_price > 0:
            result['shares_outstanding'] = round(result['market_cap'] / scrape_price, 6)

        # Sector/Industry — Screener.in's page is already being fetched
        # above for the ratios; this just extracts more from the same
        # HTML already in hand, no extra request. Screener typically
        # links to /market/sector-name/ style URLs near the peer
        # comparison table or company header — tries several known
        # patterns since the exact markup isn't verifiable from this
        # environment (screener.in isn't reachable to test against
        # directly). Logs what it found (or didn't) for the first few
        # calls so this can be diagnosed against real output.
        sector_industry_patterns = [
            r'/market/sector/[^"]*"[^>]*>([^<]+)</a>',
            r'/market/industry/[^"]*"[^>]*>([^<]+)</a>',
            r'"industry"\s*:\s*"([^"]+)"',
            r'"sector"\s*:\s*"([^"]+)"',
            r'Sector\s*:?\s*</span>\s*<span[^>]*>([^<]+)</span>',
            r'Industry\s*:?\s*</span>\s*<span[^>]*>([^<]+)</span>',
        ]
        for pat in sector_industry_patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                candidate = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                if candidate and len(candidate) < 60:  # sanity: real sector/industry names are short
                    result['industry'] = candidate
                    break
        if (sym == 'TARSONS' or debug) and not result['industry']:
            log.info(f"  🔍 {sym}: no sector/industry pattern matched Screener.in HTML — "
                     f"page structure may have changed, none of the {len(sector_industry_patterns)} "
                     f"candidate patterns hit")

        # Compare against the static CSV lookup whenever Screener's live
        # scrape actually succeeds — not to auto-fix anything (learned the
        # hard way this session that automated reclassification without
        # verification can itself introduce new errors, e.g. genuine real-
        # estate developers vs. generic EPC contractors sharing the same
        # 'Construction' label), just to surface disagreements for human
        # review. Loose case-insensitive substring check in either
        # direction, since Screener's wording won't exactly match the
        # CSV's (e.g. 'Realty' vs 'Real Estate') even when they agree.
        if result['industry']:
            static = SECTOR_INDUSTRY_LOOKUP.get(sym, {})
            static_sector = (static.get('sector') or '').lower()
            static_industry = (static.get('industry') or '').lower()
            live = result['industry'].lower()
            if static_sector or static_industry:
                agrees = ((static_sector and (live in static_sector or static_sector in live)) or
                          (static_industry and (live in static_industry or static_industry in live)))
                if not agrees:
                    log.info(f"  ⚖️  Sector disagreement — {sym}: CSV says sector={static.get('sector')!r} "
                             f"industry={static.get('industry')!r}, Screener.in says {result['industry']!r}")

        def extract_row_series(label: str, html: str) -> list:
            """Pull all data-cell values from a Screener table row (Quarterly
            Results / Shareholding Pattern), oldest-to-newest as Screener
            lists them left-to-right. Returns a list with None for any
            cell that isn't a plain number (e.g. a '+' expand button cell)."""
            pattern = rf'<td[^>]*>\s*{re.escape(label)}.*?</td>(.*?)</tr>'
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if not m:
                return []
            cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.DOTALL)
            vals = []
            for c in cells:
                clean = re.sub(r'<[^>]+>', '', c).strip()
                clean = clean.replace(',', '').replace('%', '').replace('₹', '').strip()
                try:
                    vals.append(float(clean))
                except Exception:
                    vals.append(None)
            return vals

        def pct_change(curr, prev):
            if curr is None or prev is None or prev == 0:
                return None
            return round((curr - prev) / abs(prev) * 100, 2)

        def point_diff(curr, prev):
            if curr is None or prev is None:
                return None
            return round(curr - prev, 2)

        def last_two_valid(series):
            """Return (latest, previous) skipping any None cells."""
            valid = [v for v in series if v is not None]
            if len(valid) < 2:
                return (valid[-1] if valid else None, None)
            return (valid[-1], valid[-2])

        def growth_streak(series):
            """Consecutive quarters of growth, counting back from latest."""
            valid = [v for v in series if v is not None]
            streak = 0
            for i in range(len(valid) - 1, 0, -1):
                if valid[i] > valid[i-1]:
                    streak += 1
                else:
                    break
            return streak

        # Quarterly Results table — the core CANSLIM earnings-acceleration data
        eps_series   = extract_row_series('EPS in Rs', html)
        sales_series = extract_row_series('Sales', html)
        opm_series   = extract_row_series('OPM %', html)

        eps_latest, eps_prev = last_two_valid(eps_series)
        sales_latest, sales_prev = last_two_valid(sales_series)
        opm_latest, opm_prev = last_two_valid(opm_series)

        result['eps_qoq'] = pct_change(eps_latest, eps_prev)
        valid_eps = [v for v in eps_series if v is not None]
        result['eps_yoy'] = pct_change(valid_eps[-1], valid_eps[-5]) if len(valid_eps) >= 5 else None

        result['sales_qoq'] = pct_change(sales_latest, sales_prev)
        valid_sales = [v for v in sales_series if v is not None]
        result['sales_yoy'] = pct_change(valid_sales[-1], valid_sales[-5]) if len(valid_sales) >= 5 else None

        result['opm_pct']   = opm_latest
        result['opm_trend'] = point_diff(opm_latest, opm_prev)
        result['eps_growth_streak'] = growth_streak(eps_series)

        # PEG ratio — cheap growth vs expensive growth. Only meaningful
        # when earnings are actually growing (a negative/zero YoY growth
        # makes PEG uninterpretable, so leave it None in that case).
        if result['pe'] and result['eps_yoy'] and result['eps_yoy'] > 0:
            result['peg_ratio'] = round(result['pe'] / result['eps_yoy'], 2)

        # Shareholding Pattern table — promoter/FII/DII holding + trend
        promoter_series = extract_row_series('Promoters', html)
        fii_series      = extract_row_series('FIIs', html)
        dii_series      = extract_row_series('DIIs', html)

        prom_latest, prom_prev = last_two_valid(promoter_series)
        fii_latest, fii_prev   = last_two_valid(fii_series)
        dii_latest, dii_prev   = last_two_valid(dii_series)

        if prom_latest is not None:
            result['promoter'] = prom_latest  # supersedes the old first-match extraction below if found
        result['promoter_trend'] = point_diff(prom_latest, prom_prev)
        result['fii_pct']    = fii_latest
        result['fii_trend']  = point_diff(fii_latest, fii_prev)
        result['dii_pct']    = dii_latest
        result['dii_trend']  = point_diff(dii_latest, dii_prev)

        # Fallback promoter extraction (original method) if the table-row
        # approach above didn't find anything — some older Screener page
        # layouts use a different structure for this section.
        if result['promoter'] is None:
            prom_m = re.search(r'Promoters?\s*</td>\s*<td[^>]*>([\d.]+)%?</td>', html, re.IGNORECASE)
            if prom_m:
                result['promoter'] = float(prom_m.group(1))
            else:
                prom_m2 = re.search(r'"promoters":\s*([\d.]+)', html, re.IGNORECASE)
                if prom_m2:
                    result['promoter'] = float(prom_m2.group(1))

    except Exception as e:
        _fetch_error_counts[f'screener_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'screener_{type(e).__name__}', 0) + 1
        if sym == 'TARSONS' or debug:
            log.info(f"  🔍 {sym} fetch raised exception: {type(e).__name__}: {e}")
    return result

async def fetch_upstox_fundamentals(session: aiohttp.ClientSession, sym: str, isin: str,
                                     debug: bool = False) -> Optional[dict]:
    """
    Fetch fundamentals from Upstox's official Company Fundamentals API
    instead of scraping Screener.in. This is a proper authenticated API
    call (same analytics token already used for market quotes) — no
    bot-detection/rate-limiting risk at all, since it's not scraping.

    Uses two endpoints:
    - /v2/fundamentals/{isin}/key-ratios — confirmed real shape (from
      debug logging against live data) is NOT flat keys but a list of
      {"name": "P/E", "company_value": "21.46", "sector_value": "21.45"}
      entries. Confirmed available ratio names seen so far: P/E, P/B,
      ROA, ROE, ROCE, EV/EBITDA, Quick Ratio. Market Cap/EPS/Debt-Equity
      do NOT appear in this endpoint's response — those still come from
      the Screener.in fallback (merged in, not replaced — see
      load_fundamentals_batch's fetch_one_fundamentals).
    - /v2/fundamentals/{isin}/share-holdings — Promoter/FII/DII % + trend
      (shape not yet confirmed — debug logging below will show it on
      the next run, since key-ratios calls exhausted the previous
      shared debug budget before any share-holdings response got logged)
    """
    global _upstox_fundamentals_debug_count, _upstox_shareholding_debug_count
    headers = {"Authorization": f"Bearer {ANALYTICS_TOKEN}", "Accept": "application/json"}
    result = {
        'market_cap': None, 'pe': None, 'roe': None, 'eps': None, 'debt_eq': None, 'promoter': None,
        'eps_qoq': None, 'eps_yoy': None, 'sales_qoq': None, 'sales_yoy': None,
        'opm_pct': None, 'opm_trend': None, 'eps_growth_streak': None,
        'fii_pct': None, 'fii_trend': None, 'dii_pct': None, 'dii_trend': None,
        'promoter_trend': None, 'peg_ratio': None, 'industry': None,
        'shares_outstanding': None,
    }
    got_any = False

    def parse_num(v):
        """Upstox returns ratio values as strings like '21.46' or '14.59%'."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace('%', '').replace(',', '').strip())
        except Exception:
            return None

    async def get_with_retry(url):
        """GET with one retry + backoff specifically for 429 (rate-limit)
        responses — Upstox's fundamentals endpoints do have a real rate
        limit (confirmed: hundreds of 429s once concurrency went up),
        unlike Screener.in this is a normal, well-behaved API limit, not
        adversarial blocking, so a short backoff and retry is the
        appropriate fix rather than treating it as a hard failure."""
        for attempt in (1, 2):
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 429 and attempt == 1:
                    await asyncio.sleep(1.5 + random.uniform(0, 1.5))
                    continue
                if r.status == 200:
                    return r.status, await r.json()
                return r.status, None
        return 429, None

    try:
        status, data = await get_with_retry(f"https://api.upstox.com/v2/fundamentals/{isin}/key-ratios")
        if debug:
            log.info(f"  🔍 {sym} ({isin}) Upstox key-ratios: status={status}")
        if status == 200:
            if debug and _upstox_fundamentals_debug_count < 8:
                _upstox_fundamentals_debug_count += 1
                log.info(f"  🔍 {sym} key-ratios raw response: {json.dumps(data)[:1500]}")
            items = data.get('data', [])
            if isinstance(items, dict):
                items = [items]
            ratio_map = {}
            for item in items or []:
                if isinstance(item, dict) and item.get('name'):
                    ratio_map[item['name']] = parse_num(item.get('company_value'))
            if ratio_map:
                got_any = True
                result['pe']  = ratio_map.get('P/E') or ratio_map.get('PE')
                result['roe'] = ratio_map.get('ROE')
                # Market Cap/EPS/Debt-Equity aren't in this endpoint's
                # response (confirmed against real data) — left None
                # here so the Screener.in fallback fills them in.
            else:
                _fetch_error_counts['upstox_key_ratios_empty_200'] = \
                    _fetch_error_counts.get('upstox_key_ratios_empty_200', 0) + 1
        else:
            key = f'upstox_key_ratios_status_{status}'
            _fetch_error_counts[key] = _fetch_error_counts.get(key, 0) + 1
    except Exception as e:
        _fetch_error_counts[f'upstox_key_ratios_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'upstox_key_ratios_{type(e).__name__}', 0) + 1
        if debug:
            log.info(f"  🔍 {sym} Upstox key-ratios exception: {type(e).__name__}: {e}")

    try:
        status, data = await get_with_retry(f"https://api.upstox.com/v2/fundamentals/{isin}/share-holdings")
        if debug:
            log.info(f"  🔍 {sym} ({isin}) Upstox share-holdings: status={status}")
        if status == 200:
            if debug and _upstox_shareholding_debug_count < 8:
                _upstox_shareholding_debug_count += 1
                log.info(f"  🔍 {sym} share-holdings raw response: {json.dumps(data)[:1500]}")

            # Confirmed real shape: {"data": [{"category": "promoters",
            # "history": [{"value": 25.08, "period": "Mar 2026"}, ...]},
            # {"category": "fii", ...}, {"category": "other_dii", ...},
            # {"category": "mutual_funds", ...}, {"category":
            # "retail_and_other", ...}]} — history is ordered NEWEST
            # FIRST. There's no single "dii" category; Upstox splits
            # domestic institutional holders into other_dii +
            # mutual_funds, so DII% here is their sum (matching the
            # conventional FII/DII/Promoter/Retail breakdown).
            items = data.get('data', [])
            cat_history = {}
            for entry in items or []:
                if isinstance(entry, dict) and entry.get('category'):
                    cat_history[entry['category']] = entry.get('history') or []

            def latest_prev(hist):
                vals = [parse_num(h.get('value')) for h in hist if isinstance(h, dict)]
                vals = [v for v in vals if v is not None]
                latest = vals[0] if len(vals) >= 1 else None
                prev   = vals[1] if len(vals) >= 2 else None
                return latest, prev

            prom_latest, prom_prev = latest_prev(cat_history.get('promoters', []))
            fii_latest,  fii_prev  = latest_prev(cat_history.get('fii', []))
            dii1_latest, dii1_prev = latest_prev(cat_history.get('other_dii', []))
            dii2_latest, dii2_prev = latest_prev(cat_history.get('mutual_funds', []))

            if prom_latest is not None:
                got_any = True
                result['promoter'] = prom_latest
                if prom_prev is not None:
                    result['promoter_trend'] = round(prom_latest - prom_prev, 2)
            if fii_latest is not None:
                got_any = True
                result['fii_pct'] = fii_latest
                if fii_prev is not None:
                    result['fii_trend'] = round(fii_latest - fii_prev, 2)
            if dii1_latest is not None or dii2_latest is not None:
                got_any = True
                dii_latest = (dii1_latest or 0) + (dii2_latest or 0)
                result['dii_pct'] = round(dii_latest, 2)
                if dii1_prev is not None or dii2_prev is not None:
                    dii_prev = (dii1_prev or 0) + (dii2_prev or 0)
                    result['dii_trend'] = round(dii_latest - dii_prev, 2)
        else:
            key = f'upstox_share_holdings_status_{status}'
            _fetch_error_counts[key] = _fetch_error_counts.get(key, 0) + 1
    except Exception as e:
        _fetch_error_counts[f'upstox_share_holdings_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'upstox_share_holdings_{type(e).__name__}', 0) + 1
        if debug:
            log.info(f"  🔍 {sym} Upstox share-holdings exception: {type(e).__name__}: {e}")

    # Industry classification. company-profile returned 404 for EVERY
    # stock (confirmed via error summary — endpoint path guess was wrong),
    # so this now self-heals across candidate paths, remembering whichever
    # works. Confirmed fallback: the competitors endpoint's response
    # includes a "sector" field with industry-grade values (e.g.
    # "Refineries") — used if no profile path resolves.
    global _industry_endpoint_path
    try:
        candidate_paths = ([_industry_endpoint_path] if _industry_endpoint_path
                           else ['company-profile', 'profile', 'company_profile', 'overview'])
        found = False
        for path in candidate_paths:
            status, data = await get_with_retry(f"https://api.upstox.com/v2/fundamentals/{isin}/{path}")
            if status == 200 and data:
                if debug and _upstox_fundamentals_debug_count < 8:
                    log.info(f"  🔍 {sym} {path} raw response: {json.dumps(data)[:1200]}")
                payload = data.get('data', data)
                if isinstance(payload, list):
                    payload = payload[0] if payload else {}
                if isinstance(payload, dict):
                    for k in ('industry', 'industry_name', 'basic_industry', 'sector', 'sector_industry'):
                        v = payload.get(k)
                        if v and isinstance(v, str):
                            result['industry'] = v.strip()
                            got_any = True
                            found = True
                            if _industry_endpoint_path != path:
                                _industry_endpoint_path = path
                                log.info(f"  ✅ Industry endpoint resolved: /{path}")
                            break
            if found:
                break
        if not found and not _industry_endpoint_path:
            # Fallback: competitors endpoint carries the company's peers'
            # sector — the FIRST competitor's sector is the same industry
            # bucket as the company itself (peers share it by definition).
            status, data = await get_with_retry(f"https://api.upstox.com/v2/fundamentals/{isin}/competitors")
            if status == 200 and data:
                items = data.get('data', [])
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    v = items[0].get('sector')
                    if v and isinstance(v, str):
                        result['industry'] = v.strip()
                        got_any = True
            elif status != 200:
                key = f'upstox_industry_all_paths_status_{status}'
                _fetch_error_counts[key] = _fetch_error_counts.get(key, 0) + 1
    except Exception as e:
        _fetch_error_counts[f'upstox_industry_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'upstox_industry_{type(e).__name__}', 0) + 1

    return result if got_any else None

def get_industry(sym: str) -> Optional[str]:
    """Distinct 'Industry' value (finer-grained than Sector) shown
    alongside Sector in the Index/Sector tables. Prefers the live
    Upstox/Screener-fetched value when present (it can be more current
    than the static sheet), falling back to the static
    SECTOR_INDUSTRY_LOOKUP table otherwise."""
    live = fundamentals_cache.get(sym, {}).get('industry')
    if live:
        return live
    return SECTOR_INDUSTRY_LOOKUP.get(sym, {}).get('industry')

def get_sector(sym: str) -> str:
    for sector, stocks in SECTOR_MAP.items():
        if sym in stocks:
            return sector
    # SECTOR_MAP is hand-curated and only covers a subset of the ~2,400
    # tracked stocks. For anything outside it, prefer the static
    # SECTOR_INDUSTRY_LOOKUP table (reliable, ~2,355 symbols, loaded once
    # at startup) over the live Upstox/Screener-fetched 'industry' field
    # in fundamentals_cache, which is sparse and frequently unpopulated —
    # see _load_sector_industry_lookup() for why. Still fall back to
    # fundamentals_cache as a last resort for any symbol not in either
    # static source, since it costs nothing to check.
    static = SECTOR_INDUSTRY_LOOKUP.get(sym, {}).get('sector')
    if static:
        return static
    auto = fundamentals_cache.get(sym, {}).get('industry')
    if auto:
        return auto
    return "Other"

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    open_time  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_time <= now <= close_time

async def load_fundamentals_batch(session: aiohttp.ClientSession, symbols: list):
    """Fetch fundamentals for a batch of symbols, respecting TTL cache."""
    global _fetch_error_counts
    _fetch_error_counts.clear()  # in-place (not reassignment) so cross-file readers via 'from shared import *' see the reset too
    now = time.time()
    DATA_FIELDS = ('market_cap', 'pe', 'roe', 'eps', 'debt_eq', 'promoter',
                   'eps_qoq', 'eps_yoy', 'sales_qoq', 'sales_yoy', 'opm_pct',
                   'opm_trend', 'eps_growth_streak', 'fii_pct', 'fii_trend',
                   'dii_pct', 'dii_trend', 'promoter_trend', 'peg_ratio', 'industry')
    def is_blank_cache(sym):
        c = fundamentals_cache.get(sym)
        return c is not None and all(c.get(f) is None for f in DATA_FIELDS)

    def missing_mcap_cache(sym):
        # Same one-time catch-up as load_fundamentals_from_supabase — a
        # cached row that has SOME data but no market_cap predates the
        # Screener.in fallback for that field and would otherwise wait
        # out the full 30-day TTL.
        c = fundamentals_cache.get(sym)
        return c is not None and c.get('market_cap') is None and not is_blank_cache(sym)

    to_fetch = [
        sym for sym in symbols
        if sym not in fundamentals_cache
        or is_blank_cache(sym)  # scrape failed last time — don't wait out the TTL
        or missing_mcap_cache(sym)
        or (now - fundamentals_cache[sym].get('fetched_at', 0)) > FUNDAMENTALS_TTL
    ]
    if not to_fetch:
        return

    log.info(f"  Fetching fundamentals for {len(to_fetch)} stocks (Upstox API primary, Screener.in fallback)…")
    if 'TARSONS' in to_fetch:
        log.info(f"  🔍 TARSONS is in this batch's to_fetch list (position {to_fetch.index('TARSONS')}/{len(to_fetch)})")
    elif 'TARSONS' in symbols:
        cached = fundamentals_cache.get('TARSONS', {})
        log.info(f"  🔍 TARSONS NOT in to_fetch (already cached, not stale) — cached data: {cached}")

    def isin_for(sym):
        key = instrument_key_map.get(sym, '')
        return key.split('|')[1] if '|' in key else None

    async def fetch_one_fundamentals(sym, debug):
        isin = isin_for(sym)
        upstox_data = await fetch_upstox_fundamentals(session, sym, isin, debug=debug) if isin else None
        if upstox_data is not None:
            # Upstox's key-ratios endpoint NEVER returns Market Cap, EPS,
            # or Debt-Equity (confirmed against real responses) — those
            # three fields are always None here regardless of how
            # successful the Upstox call was. A previous version of this
            # function deliberately skipped the Screener.in fallback for
            # just these 3 fields (to reduce scraping load), but since
            # Upstox succeeds for most stocks, that meant Market Cap was
            # blank for the MAJORITY of the scanner, not a rare edge case
            # — confirmed via a live screenshot showing ~80% of rows with
            # a populated P/E (from Upstox) but no Market Cap. Market Cap
            # in particular is used throughout the app (filters, order-
            # size tagging, Best Picks scoring), so it's worth the extra
            # Screener.in call whenever it's the one still missing —
            # same scraping load the app already ran before that
            # optimization, just re-enabled for the fields Upstox can't
            # provide at all.
            if not upstox_data.get('industry') or upstox_data.get('market_cap') is None:
                screener_data = await fetch_fundamentals_screener(session, sym, debug=debug)
                if screener_data.get('industry') and not upstox_data.get('industry'):
                    upstox_data['industry'] = screener_data['industry']
                for f in ('market_cap', 'eps', 'debt_eq', 'shares_outstanding'):
                    if upstox_data.get(f) is None and screener_data.get(f) is not None:
                        upstox_data[f] = screener_data[f]
            return upstox_data
        return await fetch_fundamentals_screener(session, sym, debug=debug)


    global _fundamentals_debug_count
    # Confirmed via the error-type summary: BATCH=20 (x2 endpoints per
    # stock = ~40 concurrent requests) was hitting Upstox's own rate
    # limit hard — hundreds of 429s per run. Reduced to ease pressure;
    # combined with the 429-retry-with-backoff in get_with_retry above,
    # this should recover most of what was previously rate-limited.
    BATCH = 8
    fetched = 0
    rows_to_save: list = []
    for i in range(0, len(to_fetch), BATCH):
        batch = to_fetch[i:i+BATCH]
        debug_this_batch = _fundamentals_debug_count < 10
        results = await asyncio.gather(*[
            fetch_one_fundamentals(sym, debug_this_batch) for sym in batch
        ])
        for sym, data in zip(batch, results):
            data['fetched_at'] = now
            fundamentals_cache[sym] = data
            if any(v is not None for k, v in data.items() if k != 'fetched_at'):
                fetched += 1
            elif debug_this_batch:
                _fundamentals_debug_count += 1
            if sym == 'TARSONS':
                log.info(f"  🔍 TARSONS scrape result: {data}")
            rows_to_save.append({
                'sym': sym, **{k: v for k, v in data.items() if k != 'fetched_at'},
                'fetched_at': datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            })
        # Shorter jittered delay now that most requests go through the
        # real Upstox API (fast, authenticated, no blocking risk) rather
        # than scraping — this mainly just paces whatever subset falls
        # back to Screener.in for symbols Upstox couldn't resolve.
        await asyncio.sleep(0.4 + random.uniform(0, 0.6))

        # Persist incrementally — a slow scrape (8-15+ min for the full
        # universe) shouldn't lose everything if the process restarts
        # partway through.
        if len(rows_to_save) >= 100:
            batch_rows, rows_to_save[:] = rows_to_save[:], []
            await save_fundamentals_batch_to_db(session, batch_rows)

    if rows_to_save:
        await save_fundamentals_batch_to_db(session, rows_to_save)

    log.info(f"  Fundamentals loaded: {fetched}/{len(to_fetch)} stocks")
    if _fetch_error_counts:
        summary = ', '.join(f"{k}={v}" for k, v in sorted(_fetch_error_counts.items(), key=lambda x: -x[1]))
        log.info(f"  📋 Fetch outcome breakdown: {summary}")

async def load_fundamentals_from_supabase(session: aiohttp.ClientSession) -> list:
    """
    Load previously-fetched fundamentals straight from Supabase — zero
    Screener.in requests. Same optimization as load_all_history_from_supabase:
    without this, fundamentals_cache (pure in-memory) was wiped on every
    restart, forcing a full ~2385-stock re-scrape (at ~5 stocks/sec, that's
    8-15+ minutes) gated behind a once-per-day flag — so a restart mid-fetch
    meant most stocks never got fundamentals until the NEXT calendar day.

    Streams page-by-page (same reasoning as load_all_history_from_supabase)
    rather than accumulating every page before parsing any of it.

    Returns the list of symbols that are missing or past the TTL.
    """
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    PAGE = 1000
    offset = 0

    now = time.time()
    loaded = 0
    blank = 0
    stale_or_missing: list = []
    found_syms: set = set()
    DATA_FIELDS = ('market_cap', 'pe', 'roe', 'eps', 'debt_eq', 'promoter',
                   'eps_qoq', 'eps_yoy', 'sales_qoq', 'sales_yoy', 'opm_pct',
                   'opm_trend', 'eps_growth_streak', 'fii_pct', 'fii_trend',
                   'dii_pct', 'dii_trend', 'promoter_trend', 'peg_ratio', 'industry')

    while True:
        try:
            page_headers = {**headers, "Range": f"{offset}-{offset + PAGE - 1}"}
            async with session.get(
                f"{SUPABASE_URL}/rest/v1/stock_fundamentals"
                f"?select=sym,market_cap,pe,roe,eps,debt_eq,promoter,"
                f"eps_qoq,eps_yoy,sales_qoq,sales_yoy,opm_pct,opm_trend,eps_growth_streak,industry,"
                f"fii_pct,fii_trend,dii_pct,dii_trend,promoter_trend,peg_ratio,shares_outstanding,fetched_at",
                headers=page_headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status not in (200, 206):
                    log.warning(f"load_fundamentals_from_supabase page failed: status={r.status}")
                    break
                page = await r.json()
        except Exception as e:
            log.error(f"load_fundamentals_from_supabase error: {e}")
            break

        for row in page:
            sym = row.get('sym')
            if not sym:
                continue
            found_syms.add(sym)
            fetched_at_str = row.get('fetched_at')
            fetched_at_ts = 0.0
            if fetched_at_str:
                try:
                    fetched_at_ts = datetime.fromisoformat(fetched_at_str.replace('Z', '+00:00')).timestamp()
                except Exception:
                    fetched_at_ts = 0.0
            fundamentals_cache[sym] = {
                'market_cap': row.get('market_cap'), 'pe': row.get('pe'), 'roe': row.get('roe'),
                'eps': row.get('eps'), 'debt_eq': row.get('debt_eq'), 'promoter': row.get('promoter'),
                'eps_qoq': row.get('eps_qoq'), 'eps_yoy': row.get('eps_yoy'),
                'sales_qoq': row.get('sales_qoq'), 'sales_yoy': row.get('sales_yoy'),
                'opm_pct': row.get('opm_pct'), 'opm_trend': row.get('opm_trend'),
                'eps_growth_streak': row.get('eps_growth_streak'),
                'fii_pct': row.get('fii_pct'), 'fii_trend': row.get('fii_trend'),
                'dii_pct': row.get('dii_pct'), 'dii_trend': row.get('dii_trend'),
                'promoter_trend': row.get('promoter_trend'), 'peg_ratio': row.get('peg_ratio'),
                'industry': row.get('industry'),
                'shares_outstanding': row.get('shares_outstanding'),
                'fetched_at': fetched_at_ts,
            }
            loaded += 1
            is_blank = all(row.get(f) is None for f in DATA_FIELDS)
            if is_blank:
                blank += 1
            # A row that's entirely blank means the scrape failed to extract
            # anything useful (Screener.in rate-limit/block, page structure
            # mismatch, etc.) — that's fundamentally different from "we
            # successfully confirmed this stock has no data," so retry it
            # regardless of how fresh the fetched_at timestamp is. Without
            # this, a stock unlucky enough to get blocked once stays blank
            # for a full 7-day TTL window before ever being retried.
            #
            # Separately: a row can be non-blank (has pe/roe from Upstox)
            # but still missing market_cap specifically — that was the
            # norm for most stocks before fetch_one_fundamentals started
            # falling back to Screener.in for market_cap too. Without this
            # check, those rows look "fresh" (they have SOME data) and
            # would sit with a blank market_cap for up to the full 30-day
            # TTL before ever being retried under the fixed logic. This is
            # a one-time catch-up condition — once market_cap is filled,
            # the row no longer matches it.
            missing_mcap = (not is_blank) and row.get('market_cap') is None
            if is_blank or missing_mcap or (now - fetched_at_ts) > FUNDAMENTALS_TTL:
                stale_or_missing.append(sym)

        if len(page) < PAGE:
            break
        offset += PAGE

    missing_entirely = [s for s in ALL_STOCKS if s not in found_syms]
    stale_or_missing.extend(missing_entirely)

    log.info(f"📊 Loaded {loaded} stocks' fundamentals from Supabase (0 Screener.in requests) — "
             f"{blank} are blank (all fields None — scrape failed, will retry), "
             f"{len(stale_or_missing)} total need fetching (blank, missing, or "
             f">{FUNDAMENTALS_TTL//86400}d stale)")
    gc.collect()
    return stale_or_missing

async def load_instrument_master(session: aiohttp.ClientSession):
    """Fetch Upstox instrument master to get correct instrument keys."""
    global instrument_key_map, ALL_STOCKS
    log.info("Fetching instrument master from Upstox…")
    try:
        # Primary: Analytics API instrument list
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60),
            headers={"Accept-Encoding": "gzip"}) as r:
            if r.status == 200:
                import gzip, io
                content = await r.read()
                try:
                    data = json.loads(gzip.decompress(content))
                except Exception:
                    data = json.loads(content)

                # Build sym -> instrument_key map for EQ stocks AND ETFs.
                # CONFIRMED ROOT CAUSE (user report + investigation): this
                # was EQ-only, so any ETF symbol (NV20, HDFCBSE500, and
                # others named like index-tracking ETFs) got NO instrument
                # key at all — meaning even the Upstox historical-candle
                # fallback we built for Yahoo-failing symbols could never
                # work for them, since that fallback depends entirely on
                # instrument_key_map having a real entry.
                #
                # SEPARATE BUG FOUND while investigating a different
                # missing-stock report: the old code did
                # .replace('-EQ','').replace('EQ','') — that second
                # blanket replace strips 'EQ' from ANYWHERE in the
                # symbol, not just the '-EQ' suffix Upstox actually adds.
                # Any real ticker containing 'EQ' as a substring (not at
                # the end) would get silently corrupted. Fixed to only
                # strip a genuine trailing '-EQ'.
                for item in data:
                    raw_sym = item.get('trading_symbol', '')
                    sym = raw_sym[:-3] if raw_sym.endswith('-EQ') else raw_sym
                    itype = item.get('instrument_type', '')
                    exch = item.get('exchange', '')
                    key = item.get('instrument_key', '')
                    if exch == 'NSE' and itype in ('EQ', 'ETF') and sym and key:
                        instrument_key_map[sym] = key

                log.info(f"✅ Instrument master loaded: {len(instrument_key_map)} EQ+ETF instruments")
                # Diagnostic for tracking down specific missing-stock
                # reports (e.g. COFORGE silently dropping out of the
                # live scan for days with no error) — logs whether a
                # short watchlist of previously-reported symbols made it
                # into the map this run, without spamming the log for
                # the full ~2400-symbol universe.
                _watch_syms = ['COFORGE']
                _missing_watch = [s for s in _watch_syms if s not in instrument_key_map]
                if _missing_watch:
                    log.warning(f"⚠️ Symbols missing from instrument_key_map this run: {_missing_watch}")
                else:
                    log.info(f"  ✓ Watchlist symbols present in instrument_key_map: {_watch_syms}")

                # ── Index key lookup — replaces guessing text variants
                # ('Nifty EV & New Age Automotive' vs 'NIFTY EV AND NEW
                # AGE AUTOMOTIVE' etc.) against the historical-candle
                # endpoint, which was confirmed failing for ~10 thematic
                # sector indices (Digital, EV & New Age Auto, Tourism,
                # Capital Markets, Railways, Internet, Services, REITs &
                # InvITs, Infra & Logistics, Transport & Logistics — all
                # silently falling back to Nifty as their RS benchmark).
                # This same already-fetched NSE.json.gz file includes
                # NSE_INDEX entries with the real, confirmed-correct key
                # — no more guessing needed for any index actually
                # present here. Normalized (lowercase, whitespace/&/and
                # collapsed) so 'Nifty EV & New Age Automotive' and
                # 'NIFTY EV AND NEW AGE AUTOMOTIVE' match the same entry
                # regardless of which exact casing/wording Upstox uses.
                def _norm_index_name(s):
                    s = (s or '').lower().strip()
                    s = s.replace('&', 'and')
                    s = re.sub(r'\s+', ' ', s)
                    return s

                index_key_map.clear()
                _index_debug_sample = []
                for item in data:
                    itype = item.get('instrument_type', '')
                    seg = item.get('segment', '')
                    key = item.get('instrument_key', '')
                    name = item.get('name') or item.get('trading_symbol', '')
                    if (itype == 'INDEX' or seg == 'NSE_INDEX') and name and key:
                        index_key_map[_norm_index_name(name)] = key
                        if len(_index_debug_sample) < 15:
                            _index_debug_sample.append({'name': name, 'key': key, 'type': itype, 'segment': seg})
                log.info(f"  📇 Index key lookup built: {len(index_key_map)} indices "
                         f"(sample: {json.dumps(_index_debug_sample[:8])})")


                # Update ALL_STOCKS to only include stocks we have keys for
                if len(instrument_key_map) > 100:
                    ALL_STOCKS[:] = list(instrument_key_map.keys())  # in-place: from-import-* copies in live_scan.py/fundamentals_worker.py only see mutations, not reassignments
                    log.info(f"📊 Updated stock list: {len(ALL_STOCKS)} stocks")
                return True
    except Exception as e:
        log.warning(f"Instrument master fetch failed: {e} — trying alternative…")

    # Fallback: try the CSV format
    try:
        url2 = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
        async with session.get(url2, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status == 200:
                import gzip, csv, io
                content = gzip.decompress(await r.read()).decode('utf-8')
                reader = csv.DictReader(io.StringIO(content))
                for row in reader:
                    if row.get('exchange') == 'NSE' and row.get('instrument_type') in ('EQ', 'ETF'):
                        sym = row.get('trading_symbol', '').replace('-EQ', '')
                        key = row.get('instrument_key', '')
                        if sym and key:
                            instrument_key_map[sym] = key
                log.info(f"✅ CSV master loaded: {len(instrument_key_map)} EQ+ETF instruments")
                if len(instrument_key_map) > 100:
                    ALL_STOCKS[:] = list(instrument_key_map.keys())  # in-place: from-import-* copies in live_scan.py/fundamentals_worker.py only see mutations, not reassignments
                return True
    except Exception as e:
        log.warning(f"CSV master also failed: {e} — using symbol-based keys")

    # Last resort: build keys from symbol names (may not always work)
    log.warning("Using symbol-based instrument keys as fallback")
    for sym in ALL_STOCKS:
        instrument_key_map[sym] = f"NSE_EQ|{sym}"
    return False

async def save_fundamentals_batch_to_db(session: aiohttp.ClientSession, rows: list):
    """Upsert fundamentals rows into Supabase — same chunked pattern as
    save_full_history_batch_to_db, though these rows are tiny so a larger
    chunk size is fine here."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/stock_fundamentals?on_conflict=sym"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    CHUNK = 200
    chunks = [rows[i:i+CHUNK] for i in range(0, len(rows), CHUNK)]
    sem = asyncio.Semaphore(5)
    uploaded = 0

    async def upload(chunk):
        nonlocal uploaded
        async with sem:
            try:
                async with session.post(url, headers=headers, json=chunk,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status in (200, 201, 204):
                        uploaded += len(chunk)
                    else:
                        text = await r.text()
                        log.warning(f"stock_fundamentals upsert failed: {r.status} {text[:150]}")
            except Exception as e:
                log.error(f"stock_fundamentals upsert error: {e}")

    await asyncio.gather(*[upload(c) for c in chunks])
    log.info(f"  💾 Uploaded {uploaded}/{len(rows)} fundamentals rows to Supabase")

def _r2_put_object_sync(key: str, body: bytes, content_type: str, cache_seconds: int):
    """The actual blocking boto3 call — only ever invoked inside
    asyncio.to_thread() below, never directly on the event loop.
    Moved here from live_scan.py (2026-08-01) so fundamentals_worker.py
    can also upload snapshots (e.g. announcements) to R2, not just
    live_scan.py's stock snapshot."""
    _r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl=f'public, max-age={cache_seconds}',
    )

async def upload_snapshot_to_r2(key: str, data, cache_seconds: int = 60):
    """Uploads a JSON snapshot to R2 for the frontend to read directly
    instead of querying Supabase — same data, but served from Cloudflare's
    CDN cache to everyone requesting it within cache_seconds of each other,
    instead of every single user triggering their own database read.

    Deliberately best-effort: any failure here (R2 down, not configured
    yet, network hiccup) only logs a warning and returns — it must NEVER
    interrupt or fail the scan loop, since Supabase remains the real
    source of truth regardless of whether this succeeds. The frontend
    also falls back to querying Supabase directly if the R2 file is
    missing or stale, so a failed upload here degrades gracefully rather
    than breaking anything.

    Shared (2026-08-01) between live_scan.py (stock snapshots) and
    fundamentals_worker.py (announcement snapshots) - same mechanism,
    different keys."""
    global _r2_warned
    if _r2_client is None:
        if not _r2_warned:
            log.warning("⚠️ R2 not configured (R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/"
                        "R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME) — skipping snapshot "
                        "upload, frontend will keep reading from Supabase directly")
            _r2_warned = True
        return
    try:
        body = json.dumps(data, default=str).encode('utf-8')
        await asyncio.to_thread(_r2_put_object_sync, key, body, 'application/json', cache_seconds)
        log.info(f"  ☁️ Uploaded {key} to R2 ({len(body)/1024:.1f} KB)")
    except Exception as e:
        log.warning(f"⚠️ R2 upload failed for {key}: {e}")
