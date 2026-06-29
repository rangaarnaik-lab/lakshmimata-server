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
import sys
import time
import json
import math
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('pocketrs')

# ── Config ────────────────────────────────────────────────────────────
ANALYTICS_TOKEN  = os.environ['UPSTOX_ANALYTICS_TOKEN']
SUPABASE_URL     = os.environ['SUPABASE_URL']
SUPABASE_KEY     = os.environ['SUPABASE_SERVICE_KEY']
UPDATE_INTERVAL  = 60          # seconds between updates
BATCH_SIZE       = 500         # Upstox supports 500 per bulk call
IST              = timezone(timedelta(hours=5, minutes=30))

# Market hours IST
MARKET_OPEN_H, MARKET_OPEN_M   = 9, 15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30

# ── Math functions ────────────────────────────────────────────────────
def ema(prices: list, n: int) -> Optional[float]:
    if len(prices) < n:
        return None
    k = 2 / (n + 1)
    e = sum(prices[:n]) / n
    for p in prices[n:]:
        e = p * k + e * (1 - k)
    return round(e, 2)

def ema_arr(prices: list, n: int) -> list:
    result = [None] * len(prices)
    if len(prices) < n:
        return result
    k = 2 / (n + 1)
    e = sum(prices[:n]) / n
    result[n-1] = round(e, 2)
    for i in range(n, len(prices)):
        e = prices[i] * k + e * (1 - k)
        result[i] = round(e, 2)
    return result

def sma(prices: list, n: int) -> Optional[float]:
    if len(prices) < n:
        return None
    return sum(prices[-n:]) / n

def calc_rs_raw(prices: list, end_idx: int = None) -> Optional[float]:
    end = end_idx if end_idx is not None else len(prices) - 1
    if end < 60:
        return None
    last = prices[end]
    p63  = prices[max(0, end - 63)]
    p126 = prices[max(0, end - 126)]
    p189 = prices[max(0, end - 189)]
    p252 = prices[max(0, end - 252)]
    return (
        0.4 * ((last - p63)  / p63)  +
        0.2 * ((p63  - p126) / p126) +
        0.2 * ((p126 - p189) / p189) +
        0.2 * ((p189 - p252) / p252)
    )

def percentile_rank(values: list, val: float) -> int:
    below = sum(1 for v in values if v < val)
    return min(99, max(1, round((below / len(values)) * 99) + 1))

def rs_slope(hist: list) -> dict:
    valid = [v for v in hist if v is not None]
    if len(valid) < 4:
        return {'trend': 'flat', 'slope': 0.0}
    n = len(valid)
    x_mean = (n - 1) / 2
    y_mean = sum(valid) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(valid))
    den = sum((x - x_mean) ** 2 for x in range(n))
    slope = round(num / den, 2) if den else 0.0
    trend = 'improving' if slope > 1.5 else 'declining' if slope < -1.5 else 'flat'
    return {'trend': trend, 'slope': slope}

def detect_pp(prices: list, volumes: list) -> dict:
    n = len(prices)
    result = {
        'is_pp': False, 'pp_hist': [False]*10, 'pp_count_10d': 0,
        'vol_ratio': 0.0, 'ma10': None, 'ma50': None
    }
    if n < 12:
        return result

    def is_pp_at(idx):
        if idx < 11:
            return False
        today, yesterday = prices[idx], prices[idx-1]
        if today <= yesterday:
            return False
        ma10 = sma(prices[:idx+1], 10)
        ma50 = sma(prices[:idx+1], min(50, idx+1))
        if not ma10 or not ma50:
            return False
        if not (today > ma10 and today < ma10 * 1.08 and today > ma50):
            return False
        p10 = prices[idx-10:idx]
        v10 = volumes[idx-10:idx]
        max_down = max((v10[i] for i in range(1, len(p10)) if p10[i] < p10[i-1]), default=0)
        if max_down == 0:
            max_down = sum(v10) / len(v10)
        return volumes[idx] > max_down

    pp_hist = [is_pp_at(n - 1 - d) for d in range(9, -1, -1)]
    result['pp_hist']      = pp_hist
    result['pp_count_10d'] = sum(pp_hist)
    result['is_pp']        = pp_hist[-1]
    result['ma10']         = sma(prices, 10)
    result['ma50']         = sma(prices, 50)

    # Vol ratio
    p10 = prices[n-11:n-1]
    v10 = volumes[n-11:n-1]
    max_down = max((v10[i] for i in range(1, len(p10)) if p10[i] < p10[i-1]), default=0)
    if max_down == 0:
        max_down = sum(v10) / len(v10) if v10 else 1
    result['vol_ratio'] = round(volumes[n-1] / max_down, 2) if max_down > 0 else 0.0
    return result

def detect_52wl(prices: list, volumes: list) -> dict:
    n = len(prices)
    empty = {
        'near_52wl': False, 'pct_from_52wl': 999, 'low_52w': 0, 'high_52w': 0,
        'crossed_ema5': False, 'pp_volume': False, 'ema5': None,
        'is_signal': False
    }
    if n < 260:
        return empty
    today, yesterday = prices[n-1], prices[n-2]
    low52  = min(prices[-252:])
    high52 = max(prices[-252:])
    pct    = round((today - low52) / low52 * 100, 2)
    near   = pct <= 15
    ea     = ema_arr(prices, 5)
    e5t, e5y = ea[n-1], ea[n-2]
    crossed = e5y is not None and e5t is not None and yesterday <= e5y and today > e5t
    p10 = prices[n-11:n-1]
    v10 = volumes[n-11:n-1]
    max_down = max((v10[i] for i in range(1, len(p10)) if p10[i] < p10[i-1]), default=0)
    if max_down == 0:
        max_down = sum(v10) / len(v10) if v10 else 1
    pp_vol = today > yesterday and volumes[n-1] > max_down
    return {
        'near_52wl':   near,
        'pct_from_52wl': pct,
        'low_52w':     round(low52, 2),
        'high_52w':    round(high52, 2),
        'crossed_ema5': crossed,
        'pp_volume':   pp_vol,
        'ema5':        e5t,
        'is_signal':   near and crossed and pp_vol,
    }

def detect_weak_rs(prices: list, volumes: list, rs: int, threshold: float = 8.0) -> dict:
    n = len(prices)
    if n < 6:
        return {'is_weak_rs': False, 'chg_1d': 0, 'chg_5d': 0, 'vol_spike': 0}
    today    = prices[n-1]
    yesterday= prices[n-2]
    week     = prices[n-6]
    chg1d    = round((today - yesterday) / yesterday * 100, 2)
    chg5d    = round((today - week) / week * 100, 2)
    avg5     = sum(volumes[-6:-1]) / 5
    spike    = round(volumes[n-1] / avg5, 2) if avg5 > 0 else 0
    return {
        'is_weak_rs':  rs < 50 and chg1d >= threshold,
        'chg_1d':      chg1d,
        'chg_5d':      chg5d,
        'vol_spike':   spike,
    }

def build_rs_history(all_stocks: list, days: int = 15) -> dict:
    """Build 15-day RS history for all stocks."""
    n = len(all_stocks[0]['prices'])
    history = {s['sym']: [] for s in all_stocks}
    for d in range(days-1, -1, -1):
        end_idx = n - 1 - d
        raw_map = {}
        for s in all_stocks:
            raw = calc_rs_raw(s['prices'], end_idx)
            if raw is not None:
                raw_map[s['sym']] = raw
        raw_vals = list(raw_map.values())
        for s in all_stocks:
            if s['sym'] in raw_map:
                history[s['sym']].append(percentile_rank(raw_vals, raw_map[s['sym']]))
            else:
                history[s['sym']].append(None)
    return history

def build_sector_rs(processed: list, sector_map: dict) -> list:
    sectors = []
    for sector, syms in sector_map.items():
        members = [s for s in processed if s['sym'] in syms]
        if not members:
            continue
        avg_rs   = round(sum(s['rs'] for s in members) / len(members))
        pp_count = sum(1 for s in members if s.get('is_pp'))
        improving= sum(1 for s in members if s.get('rs_trend') == 'improving')
        top5     = sorted(members, key=lambda x: x['rs'], reverse=True)[:5]
        sectors.append({
            'sector':   sector,
            'avg_rs':   avg_rs,
            'count':    len(members),
            'pp_count': pp_count,
            'improving':improving,
            'top_stocks': [{'sym': s['sym'], 'rs': s['rs']} for s in top5],
        })
    sectors.sort(key=lambda x: x['avg_rs'], reverse=True)
    for i, s in enumerate(sectors):
        s['rank'] = i + 1
    return sectors

# ── Sector map ────────────────────────────────────────────────────────
SECTOR_MAP = {
    "IT":            ["TCS","INFOSYS","WIPRO","HCLTECH","TECHM","MPHASIS","PERSISTENT","COFORGE","LTTS","KPITTECH","TATAELXSI"],
    "Banking":       ["HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","INDUSINDBK","BANDHANBNK","FEDERALBNK","IDFCFIRSTB","RBLBANK","YESBANK","PNB","CANBK","BANKBARODA","AUBANK"],
    "NBFC":          ["BAJFINANCE","BAJAJFINSV","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","AAVAS","HOMEFIRST","LICHSGFIN","PNBHOUSING","CANFINHOME"],
    "Auto":          ["MARUTI","TATAMOTORS","M&M","BAJAJ-AUTO","HEROMOTOCO","TVSMOTOR","EICHERMOT","BOSCHLTD","MOTHERSON","ESCORTS"],
    "Pharma":        ["SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","LUPIN","AUROPHARMA","BIOCON","ALKEM","GLENMARK","IPCALAB","MANKIND","JUBLPHARMA"],
    "FMCG":          ["HINDUNILVR","ITC","NESTLEIND","DABUR","MARICO","COLPAL","EMAMILTD","GODREJCP","TATACONSUM"],
    "Energy":        ["RELIANCE","ONGC","BPCL","IOC","HINDPETRO","GAIL","PETRONET","IGL","MGL","ATGL"],
    "Metals":        ["JSWSTEEL","TATASTEEL","HINDALCO","COALINDIA","VEDL","NMDC","MOIL"],
    "Infra/Capital": ["LT","SIEMENS","ABB","BHEL","BEL","HAL","CUMMINSIND","THERMAX","HAVELLS"],
    "Cement":        ["ULTRACEMCO","GRASIM","SHREECEM","AMBUJACEM","ACC","JKCEMENT","RAMCOCEM"],
    "Consumer":      ["TITAN","ASIANPAINT","BERGEPAINT","PIDILITIND","VOLTAS","CROMPTON"],
    "Telecom":       ["BHARTIARTL","IDEA","TATACOMM","RAILTEL","HFCL","STLTECH"],
    "Realty":        ["DLF","GODREJPROP","OBEROIRLTY","PRESTIGE","BRIGADE","PHOENIXLTD","SOBHA","LODHA"],
    "Healthcare":    ["APOLLOHOSP","FORTIS","MAXHEALTH","METROPOLIS","THYROCARE","LALPATHLAB","NARAYANA","ASTER"],
    "Insurance":     ["SBILIFE","HDFCLIFE","ICICIPRULI","LICI","GICRE","STARHEALTH"],
    "Internet":      ["ZOMATO","NYKAA","PAYTM","POLICYBZR","INDIAMART","JUSTDIAL","RATEGAIN","IXIGO"],
    "Travel":        ["IRCTC","EASEMYTRIP","THOMASCOOK"],
    "Exchange":      ["BSE","CDSL","CAMS","MCX","ANGELONE"],
}

def get_sector(sym: str) -> str:
    for sector, stocks in SECTOR_MAP.items():
        if sym in stocks:
            return sector
    return "Other"

# ── Upstox API ────────────────────────────────────────────────────────
NIFTY50   = ["RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK","INFOSYS","SBIN","HINDUNILVR","ITC","LT","KOTAKBANK","HCLTECH","AXISBANK","BAJFINANCE","MARUTI","ASIANPAINT","SUNPHARMA","TITAN","ULTRACEMCO","NESTLEIND","WIPRO","NTPC","POWERGRID","TECHM","TATAMOTORS","ADANIENT","ADANIPORTS","ONGC","BAJAJFINSV","JSWSTEEL","TATASTEEL","COALINDIA","HINDALCO","M&M","DRREDDY","CIPLA","EICHERMOT","DIVISLAB","BPCL","GRASIM","INDUSINDBK","APOLLOHOSP","BAJAJ-AUTO","HEROMOTOCO","TVSMOTOR","SHREECEM","BRITANNIA","VEDL","BEL","NTPC"]
MIDCAP    = ["MPHASIS","PERSISTENT","COFORGE","LTTS","TATAELXSI","BANDHANBNK","FEDERALBNK","IDFCFIRSTB","RBLBANK","AUBANK","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","AAVAS","ESCORTS","AUROPHARMA","LUPIN","BIOCON","ALKEM","GLENMARK","IPCALAB","EMAMILTD","GODREJCP","NMDC","MOIL","PRESTIGE","BRIGADE","PHOENIXLTD","SOBHA","LODHA","METROPOLIS","THYROCARE","LALPATHLAB","NARAYANA","ASTER","STARHEALTH","MCX","ANGELONE","EASEMYTRIP","RATEGAIN"]
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

ALL_STOCKS = list(dict.fromkeys(NIFTY50 + MIDCAP + SMALLCAP + MICROCAP))

async def fetch_instruments(session: aiohttp.ClientSession) -> list:
    """Fetch all NSE instrument keys from Upstox."""
    url = "https://api.upstox.com/v2/instruments/NSE"
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            log.warning(f"Instruments fetch failed: {r.status}")
            return []
        data = await r.json()
        # Filter equity stocks
        instruments = [
            i for i in data.get('data', [])
            if i.get('instrument_type') == 'EQ' and i.get('exchange') == 'NSE'
        ]
        log.info(f"Fetched {len(instruments)} NSE equity instruments")
        return instruments

async def fetch_bulk_ohlc(session: aiohttp.ClientSession, instrument_keys: list) -> dict:
    """Fetch OHLC for up to 500 instruments in one call."""
    url = "https://api.upstox.com/v2/market-quote/ohlc"
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    params = {
        "instrument_key": ",".join(instrument_keys),
        "interval": "1d"
    }
    try:
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                log.warning(f"OHLC fetch failed: {r.status}")
                return {}
            data = await r.json()
            return data.get('data', {})
    except Exception as e:
        log.error(f"OHLC fetch error: {e}")
        return {}

async def fetch_historical(session: aiohttp.ClientSession, sym: str) -> dict:
    """Fetch 15 months of daily historical data for one stock."""
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=400)).strftime('%Y-%m-%d')
    key  = f"NSE_EQ%7C{sym}"
    url  = f"https://api.upstox.com/v2/historical-candle/{key}/day/{to}/{from_}"
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {}
            data = await r.json()
            candles = list(reversed(data.get('data', {}).get('candles', [])))
            return {
                'prices':  [c[4] for c in candles],   # close
                'volumes': [c[5] for c in candles],   # volume
                'highs':   [c[2] for c in candles],
                'lows':    [c[3] for c in candles],
            }
    except Exception as e:
        return {}

# ── Supabase client ───────────────────────────────────────────────────
async def supabase_upsert(session: aiohttp.ClientSession, table: str, rows: list):
    """Upsert rows into Supabase table."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    # Upsert in chunks of 100
    for i in range(0, len(rows), 100):
        chunk = rows[i:i+100]
        try:
            async with session.post(url, headers=headers, json=chunk,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status not in (200, 201, 204):
                    text = await r.text()
                    log.warning(f"Supabase upsert {table} failed: {r.status} {text[:100]}")
        except Exception as e:
            log.error(f"Supabase error: {e}")

async def supabase_update_meta(session: aiohttp.ClientSession, meta: dict):
    """Update scan metadata."""
    url = f"{SUPABASE_URL}/rest/v1/scan_meta"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    try:
        async with session.post(url, headers=headers, json=[{"id": "latest", **meta}],
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            pass
    except Exception as e:
        log.error(f"Meta update error: {e}")

# ── Market hours check ────────────────────────────────────────────────
def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    open_time  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_time <= now <= close_time

def is_scan_time() -> bool:
    """Run scan during market hours + 30 min before/after."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0) - timedelta(minutes=30)
    close_time = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0) + timedelta(minutes=30)
    return open_time <= now <= close_time

# ── Historical data cache (loaded once at startup, updated in batch) ──
historical_cache: dict = {}  # sym -> {prices, volumes}

async def load_historical_cache(session: aiohttp.ClientSession):
    """Load historical data for all stocks at startup."""
    log.info(f"Loading historical data for {len(ALL_STOCKS)} stocks…")
    BATCH = 10
    loaded = 0
    for i in range(0, len(ALL_STOCKS), BATCH):
        batch = ALL_STOCKS[i:i+BATCH]
        results = await asyncio.gather(*[fetch_historical(session, sym) for sym in batch])
        for sym, data in zip(batch, results):
            if data:
                historical_cache[sym] = data
                loaded += 1
        await asyncio.sleep(0.5)  # rate limit
        if (i // BATCH) % 10 == 0:
            log.info(f"  Loaded {loaded}/{len(ALL_STOCKS)} stocks…")
    log.info(f"✅ Historical cache loaded: {loaded} stocks")

# ── Main scan function ────────────────────────────────────────────────
async def run_scan(session: aiohttp.ClientSession, scan_type: str = 'live') -> int:
    start = time.time()
    now_ist = datetime.now(IST)
    log.info(f"🔄 Starting {scan_type} scan at {now_ist.strftime('%H:%M:%S IST')}")

    # Step 1: Fetch live prices for all stocks (bulk OHLC — 500 per call)
    instrument_keys = [f"NSE_EQ|{sym}" for sym in ALL_STOCKS]
    live_data = {}
    for i in range(0, len(instrument_keys), BATCH_SIZE):
        batch = instrument_keys[i:i+BATCH_SIZE]
        data  = await fetch_bulk_ohlc(session, batch)
        live_data.update(data)
        if len(instrument_keys) > BATCH_SIZE:
            await asyncio.sleep(0.5)

    log.info(f"  Live prices: {len(live_data)} stocks")

    # Step 2: For batch scans, refresh historical cache
    if scan_type in ('batch_morning', 'batch_eod'):
        await load_historical_cache(session)

    # Step 3: Update historical cache with today's live price
    for sym in ALL_STOCKS:
        key = f"NSE_EQ:{sym}"
        if key not in live_data or sym not in historical_cache:
            continue
        live = live_data[key]
        last_price = live.get('last_price', 0)
        if last_price and last_price > 0:
            # Update last price in cache (today's close = live price)
            if historical_cache[sym]['prices']:
                historical_cache[sym]['prices'][-1] = last_price
                historical_cache[sym]['volumes'][-1] = live.get('volume', historical_cache[sym]['volumes'][-1])

    # Step 4: Calculate RS ratings for all stocks
    stocks_with_hist = [
        {'sym': sym, **historical_cache[sym]}
        for sym in ALL_STOCKS
        if sym in historical_cache and len(historical_cache[sym].get('prices', [])) >= 60
    ]

    # Raw RS scores
    raw_scores = []
    for s in stocks_with_hist:
        raw = calc_rs_raw(s['prices'])
        if raw is not None:
            raw_scores.append({'sym': s['sym'], 'raw': raw})
    raw_vals = [r['raw'] for r in raw_scores]

    # RS history (15 days)
    rs_history = build_rs_history(stocks_with_hist, days=15)

    # Step 5: Build full stock records
    processed = []
    for s in stocks_with_hist:
        sym = s['sym']
        prices  = s['prices']
        volumes = s['volumes']
        n = len(prices)

        # RS
        raw_entry = next((r for r in raw_scores if r['sym'] == sym), None)
        rs = percentile_rank(raw_vals, raw_entry['raw']) if raw_entry else 0
        hist = rs_history.get(sym, [])
        trend_data = rs_slope(hist)

        # Live price
        live_key = f"NSE_EQ:{sym}"
        live = live_data.get(live_key, {})
        last  = prices[n-1]
        prev  = prices[n-2] if n > 1 else last
        chg   = round((last - prev) / prev * 100, 2) if prev else 0
        vol   = volumes[n-1] if volumes else 0

        # PP
        pp = detect_pp(prices, volumes)

        # Volume signals
        yr_vols  = volumes[-252:] if len(volumes) >= 252 else volumes
        max_yr   = max(yr_vols) if yr_vols else 1
        max_all  = max(volumes) if volumes else 1
        hy_pct   = round(vol / max_yr * 100, 1) if max_yr > 0 else 0
        ht_pct   = round(vol / max_all * 100, 1) if max_all > 0 else 0

        # EMA9
        e9 = ema(prices, 9)
        near_ema9 = False
        pct_ema9  = None
        if e9 and rs >= 90:
            pct_ema9  = round((last - e9) / e9 * 100, 2)
            near_ema9 = abs(pct_ema9) <= 3

        # 52WL
        wl = detect_52wl(prices, volumes)

        # Weak RS
        weak = detect_weak_rs(prices, volumes, rs)

        # 52W high/low
        p252  = prices[-252:] if len(prices) >= 252 else prices
        h52   = max(p252)
        l52   = min(p252)

        processed.append({
            'sym':            sym,
            'last_price':     round(last, 2),
            'open':           round(live.get('ohlc', {}).get('open', last), 2),
            'high':           round(live.get('ohlc', {}).get('high', last), 2),
            'low':            round(live.get('ohlc', {}).get('low', last), 2),
            'close':          round(last, 2),
            'prev_close':     round(prev, 2),
            'chg_pct':        chg,
            'volume':         int(vol),
            'rs':             rs,
            'rs_raw':         round(raw_entry['raw'], 6) if raw_entry else None,
            'rs_trend':       trend_data['trend'],
            'rs_slope':       trend_data['slope'],
            'rs_hist':        hist,
            'is_pp':          pp['is_pp'],
            'pp_count_10d':   pp['pp_count_10d'],
            'pp_hist':        pp['pp_hist'],
            'pp_vol_ratio':   pp['vol_ratio'],
            'ma10':           round(pp['ma10'], 2) if pp['ma10'] else None,
            'ma50':           round(pp['ma50'], 2) if pp['ma50'] else None,
            'is_hy':          hy_pct >= 95,
            'hy_pct':         hy_pct,
            'is_ht':          ht_pct >= 95,
            'ht_pct':         ht_pct,
            'ema9':           e9,
            'near_ema9':      near_ema9,
            'pct_from_ema9':  pct_ema9,
            'low_52w':        round(l52, 2),
            'high_52w':       round(h52, 2),
            'pct_from_52wl':  wl['pct_from_52wl'],
            'near_52wl':      wl['near_52wl'],
            'crossed_ema5':   wl['crossed_ema5'],
            'pp_volume_52wl': wl['pp_volume'],
            'is_52wl_signal': wl['is_signal'],
            'ema5':           wl['ema5'],
            'is_weak_rs':     weak['is_weak_rs'],
            'weak_chg_1d':    weak['chg_1d'],
            'weak_chg_5d':    weak['chg_5d'],
            'weak_vol_spike': weak['vol_spike'],
            'sector':         get_sector(sym),
            'in_nifty50':     sym in NIFTY50,
            'in_midcap':      sym in MIDCAP,
            'in_smallcap':    sym in SMALLCAP,
            'in_microcap':    sym in MICROCAP,
            'last_updated':   now_ist.isoformat(),
            'scan_type':      scan_type,
        })

    # Step 6: Build sector RS
    sector_rows = build_sector_rs(processed, SECTOR_MAP)

    # Step 7: Save to Supabase
    log.info(f"  Saving {len(processed)} stocks to Supabase…")
    await supabase_upsert(session, 'stocks', processed)
    await supabase_upsert(session, 'sectors', [
        {**s, 'last_updated': now_ist.isoformat(), 'top_stocks': json.dumps(s['top_stocks'])}
        for s in sector_rows
    ])

    # Step 8: Update scan metadata
    duration = round(time.time() - start, 1)
    next_scan = (now_ist + timedelta(seconds=UPDATE_INTERVAL)).isoformat()
    await supabase_update_meta(session, {
        'last_scan':    now_ist.isoformat(),
        'scan_type':    scan_type,
        'stocks_count': len(processed),
        'duration_sec': duration,
        'status':       'success',
        'error_msg':    None,
        'next_scan':    next_scan,
    })

    log.info(f"✅ {scan_type} scan done: {len(processed)} stocks in {duration}s")
    return len(processed)

# ── Main loop ─────────────────────────────────────────────────────────
async def main():
    log.info("=" * 60)
    log.info("  PocketRS Pro — Live Update Server")
    log.info(f"  Update interval: {UPDATE_INTERVAL} seconds")
    log.info(f"  Market hours: {MARKET_OPEN_H}:{MARKET_OPEN_M:02d} - {MARKET_CLOSE_H}:{MARKET_CLOSE_M:02d} IST")
    log.info("=" * 60)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Load historical data cache at startup
        log.info("Loading historical data cache at startup…")
        await load_historical_cache(session)

        # Run initial scan
        await run_scan(session, 'batch_morning')

        last_scan = time.time()
        scan_count = 0

        while True:
            try:
                now = time.time()
                elapsed = now - last_scan

                if elapsed >= UPDATE_INTERVAL:
                    if is_scan_time():
                        scan_type = 'live' if is_market_open() else 'batch_eod'
                        await run_scan(session, scan_type)
                        scan_count += 1
                        last_scan = time.time()
                    else:
                        ist_now = datetime.now(IST)
                        log.info(f"⏸ Market closed ({ist_now.strftime('%H:%M IST')}) — next check in {UPDATE_INTERVAL}s")
                        last_scan = time.time()

                await asyncio.sleep(5)  # check every 5 seconds

            except KeyboardInterrupt:
                log.info("Shutting down…")
                break
            except Exception as e:
                log.error(f"Loop error: {e}")
                await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
