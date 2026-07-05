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
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
UPDATE_INTERVAL  = 60          # seconds between updates
BATCH_SIZE       = 500         # Upstox supports 500 per bulk call
IST              = timezone(timedelta(hours=5, minutes=30))

# ── Telegram Bot ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

async def send_telegram(session, message: str):
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning(f"Telegram send failed: {r.status}")
    except Exception as e:
        log.warning(f"Telegram error: {e}")

async def send_daily_digest(session, processed: list, breadth: dict):
    """Send EOD digest to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    improvers = sorted(
        [s for s in processed if s.get('rs_trend') == 'improving' and (s.get('rs_tv') or 0) >= 70],
        key=lambda x: x.get('rs_tv') or x.get('rs', 0), reverse=True
    )[:5]
    s2_new   = [s for s in processed if s.get('is_s2_new_entry')][:5]
    pp_today = [s for s in processed if s.get('is_pp')][:10]
    rs_highs = [s for s in processed if s.get('rs_line_new_high')][:5]

    date_str = datetime.now(IST).strftime('%d %b %Y')
    adv = breadth.get('advances', 0)
    dec = breadth.get('declines', 0)
    s2c = breadth.get('stage2_count', 0)
    s4c = breadth.get('stage4_count', 0)
    imp = breadth.get('rs_improving', 0)
    dcl = breadth.get('rs_declining', 0)
    h52 = breadth.get('new_52w_high', 0)
    l52 = breadth.get('new_52w_low', 0)
    ppc = breadth.get('pp_count', 0)
    vol = breadth.get('rvol_surge', 0)

    s2_lines  = '\n'.join(f"  {s['sym']} RS:{s.get('rs_tv') or s.get('rs','?')}" for s in s2_new) or '  None'
    rsl_lines = '\n'.join(f"  {s['sym']} RS:{s.get('rs_tv') or s.get('rs','?')}" for s in rs_highs) or '  None'
    pp_syms   = ', '.join(s['sym'] for s in pp_today) or 'None'
    top_syms  = '\n'.join(f"  {s['sym']} {s.get('rs_tv') or s.get('rs','?')}" for s in improvers) or '  None'

    msg = (
        f"<b>Lakshmimata EOD Digest — {date_str}</b>\n\n"
        f"<b>Market Breadth</b>\n"
        f"Up: {adv}  Down: {dec}\n"
        f"Stage2: {s2c}  Stage4: {s4c}\n"
        f"RS Improving: {imp}  Declining: {dcl}\n"
        f"52W High: {h52}  52W Low: {l52}\n"
        f"PP Signals: {ppc}  Vol Surge: {vol}\n\n"
        f"<b>New Stage 2 Entries ({len(s2_new)})</b>\n{s2_lines}\n\n"
        f"<b>RS Line New Highs ({len(rs_highs)})</b>\n{rsl_lines}\n\n"
        f"<b>PP Signals Today</b>\n{pp_syms}\n\n"
        f"<b>Top RS Improvers</b>\n{top_syms}"
    )
    await send_telegram(session, msg)
    log.info("Daily digest sent to Telegram")


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

def std_dev(prices: list, n: int) -> Optional[float]:
    if len(prices) < n:
        return None
    vals = prices[-n:]
    mean = sum(vals) / n
    variance = sum((p - mean) ** 2 for p in vals) / n
    return variance ** 0.5

def true_range_series(highs: list, lows: list, closes: list) -> list:
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            ))
    return tr

def atr(highs: list, lows: list, closes: list, n: int = 20) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    tr = true_range_series(highs, lows, closes)
    return sum(tr[-n:]) / n

def calc_rvol(volumes: list, today_vol: int = None) -> dict:
    """
    Relative Volume — today's volume vs average volume at same time.
    Without intraday data, we compare today's vol vs 20d avg.
    RVOL > 2.0 = very high, > 1.5 = high, < 0.5 = drying up.
    """
    if len(volumes) < 20:
        return {'rvol': None, 'avg_vol_20d': None}
    avg_20d = sum(volumes[-21:-1]) / 20  # exclude today
    today = today_vol if today_vol else volumes[-1]
    rvol = round(today / avg_20d, 2) if avg_20d > 0 else None
    return {
        'rvol': rvol,
        'avg_vol_20d': round(avg_20d),
        'vol_signal': 'surge' if rvol and rvol >= 2.0 else
                      'high'   if rvol and rvol >= 1.5 else
                      'avg'    if rvol and rvol >= 0.8 else
                      'dry'    if rvol else 'unknown'
    }

def calc_rs_line(prices: list, bench_prices: list) -> dict:
    """
    RS Line = stock price / Nifty price * 100.
    RS Line New High = RS line at all-time high in last 252 days BEFORE price.
    This is the IBD 'RS Line in Blue Sky' signal — early leader detection.
    """
    if len(prices) < 60 or len(bench_prices) < 60:
        return {'rs_line_new_high': False, 'rs_line_trend': 'flat'}
    n = min(len(prices), len(bench_prices))
    rs_line = [prices[i] / bench_prices[i] * 100 for i in range(n) if bench_prices[i] > 0]
    if len(rs_line) < 30:
        return {'rs_line_new_high': False, 'rs_line_trend': 'flat'}
    current = rs_line[-1]
    high_252 = max(rs_line[-min(252,len(rs_line)):])
    price_high_252 = max(prices[-min(252,len(prices)):])
    # RS Line New High = RS line at high but price NOT at high yet
    rs_line_new_high = (current >= high_252 * 0.99 and prices[-1] < price_high_252 * 0.98)
    # RS Line trend (5d vs 20d slope)
    if len(rs_line) >= 20:
        slope = (rs_line[-1] - rs_line[-20]) / rs_line[-20] * 100
        trend = 'rising' if slope > 1 else 'falling' if slope < -1 else 'flat'
    else:
        trend = 'flat'
    return {
        'rs_line_new_high': rs_line_new_high,
        'rs_line_trend': trend,
        'rs_line_value': round(current, 2),
    }

def calc_stage2_new_entry(prices: list, prev_stage: int = None) -> bool:
    """
    Stage 2 New Entry = stock just entered Stage 2 this scan
    (was Stage 1 previously, now above 30W MA with rising momentum).
    prev_stage comes from previous scan data — not available in stateless calc,
    so we approximate: price crossed above MA30 in last 3 days.
    """
    if len(prices) < 30:
        return False
    ma30_now   = sum(prices[-30:]) / 30
    ma30_3d    = sum(prices[-33:-3]) / 30 if len(prices) >= 33 else None
    last_price = prices[-1]
    # Crossed above MA30 in last 3 days
    recently_crossed = (ma30_3d and prices[-4] < ma30_3d and last_price > ma30_now)
    return bool(recently_crossed and last_price > ma30_now)

def calc_earnings_momentum(screener_html: str) -> dict:
    """Parse quarterly EPS growth from Screener.in HTML."""
    import re
    result = {'eps_growth_3q': None, 'consecutive_growth': 0, 'eps_quarters': []}
    if not screener_html:
        return result
    # Look for quarterly EPS data in the financial tables
    # Screener shows TTM EPS and quarterly Net Profit
    pat = re.findall(r'<td[^>]*>([\d,.-]+)</td>', screener_html)
    # Basic parse — extract numbers from profit rows
    nums = []
    for p in pat:
        try:
            nums.append(float(p.replace(',','')))
        except:
            pass
    # We'll use the pe and eps we already parse as a proxy
    return result

# ── John Carter TTM Squeeze — Full Implementation ─────────────────────
# Based on John Carter's "Mastering the Trade" TTM Squeeze indicator
# 
# Logic:
# 1. Bollinger Bands (20, 2.0) 
# 2. Keltner Channels (20, 1.5 ATR)
# 3. Squeeze ON  = BB inside KC (red dot) — stock coiling
# 4. Squeeze OFF = BB outside KC (green dot) — stock fired
# 5. Momentum = Donchian midline vs EMA midline (histogram)
# 6. Fire signal = squeeze was ON, now OFF + momentum turning up

def __sma(data, n):
    if len(data) < n: return None
    return sum(data[-n:]) / n

def ema(data, n):
    if len(data) < n: return None
    k = 2 / (n + 1)
    e = sum(data[:n]) / n
    for p in data[n:]:
        e = p * k + e * (1 - k)
    return e

def stdev(data, n):
    if len(data) < n: return None
    m = sum(data[-n:]) / n
    variance = sum((x - m) ** 2 for x in data[-n:]) / n
    return variance ** 0.5

def true_range(closes, highs, lows):
    """Compute True Range series."""
    tr = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr

def calc_ttm_squeeze(closes, highs, lows, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5):
    """
    Full John Carter TTM Squeeze calculation.
    
    Returns dict with:
    - in_squeeze: bool (BB inside KC = squeeze ON = red dot)
    - squeeze_fired: bool (just transitioned from ON to OFF = green dot)
    - momentum: float (histogram value, positive=bullish, negative=bearish)
    - momentum_dir: 'up'|'down'|'flat' (is histogram rising or falling)
    - squeeze_days: int (consecutive days in squeeze)
    - strength_score: float (squeeze_days * abs(momentum) = overall strength)
    - dots: list of last 20 dots ('red'|'green'|'black') for UI histogram
    - hist: list of last 20 momentum values for histogram bars
    - fired_bullish: bool (fired + momentum positive + rising)
    - fired_bearish: bool (fired + momentum negative + falling)
    """
    n = max(bb_len, kc_len)
    empty = {
        'in_squeeze': False, 'squeeze_fired': False,
        'momentum': 0, 'momentum_dir': 'flat',
        'squeeze_days': 0, 'strength_score': 0,
        'dots': [], 'hist': [],
        'fired_bullish': False, 'fired_bearish': False,
        'bb_width_pct': None,
    }
    if len(closes) < n + 30:
        return empty

    # Bollinger Bands
    bb_basis = [_sma(closes[:i+1], bb_len) for i in range(len(closes))]
    bb_std   = [stdev(closes[:i+1], bb_len) for i in range(len(closes))]

    # Keltner Channels using ATR
    tr = true_range(closes, highs, lows)
    tr_full = [0] + tr  # align with closes
    atr_series = []
    for i in range(len(closes)):
        if i < kc_len:
            atr_series.append(None)
        else:
            atr_series.append(sum(tr_full[i-kc_len+1:i+1]) / kc_len)

    kc_basis = [_sma(closes[:i+1], kc_len) for i in range(len(closes))]

    # Momentum = price position relative to midpoint of high/low range and EMA
    # John Carter's exact formula: 
    # val = close - avg(avg(highest_high(len), lowest_low(len)), _sma(close, len))
    mom_series = []
    for i in range(len(closes)):
        if i < n:
            mom_series.append(None)
            continue
        window_h = max(highs[max(0,i-kc_len+1):i+1])
        window_l = min(lows[max(0,i-kc_len+1):i+1])
        delta = closes[i] - (((window_h + window_l) / 2 + (_sma(closes[:i+1], kc_len) or closes[i])) / 2)
        mom_series.append(delta)

    # Linear regression of momentum (smooth it)
    def linreg(data, length):
        if len(data) < length: return data[-1] if data else 0
        xs = list(range(length))
        ys = data[-length:]
        mx = sum(xs)/length
        my = sum(ys)/length
        num = sum((xs[i]-mx)*(ys[i]-my) for i in range(length))
        den = sum((xs[i]-mx)**2 for i in range(length))
        if den == 0: return my
        slope = num/den
        return my + slope*(length-1-mx)

    # Build dot and histogram series for last 30 bars
    history_dots = []
    history_hist = []
    prev_in_sq = False

    for i in range(max(0, len(closes)-30), len(closes)):
        bb_b = bb_basis[i]
        bb_s = bb_std[i]
        kc_b = kc_basis[i]
        atr  = atr_series[i]
        if bb_b is None or bb_s is None or kc_b is None or atr is None:
            history_dots.append('black')
            history_hist.append(0)
            continue

        bb_upper = bb_b + bb_mult * bb_s
        bb_lower = bb_b - bb_mult * bb_s
        kc_upper = kc_b + kc_mult * atr
        kc_lower = kc_b - kc_mult * atr

        in_sq = (bb_upper < kc_upper) and (bb_lower > kc_lower)
        history_dots.append('red' if in_sq else 'green')

        # Momentum histogram
        mom_vals = [m for m in mom_series[max(0,i-kc_len+1):i+1] if m is not None]
        if mom_vals:
            val = linreg(mom_vals, min(len(mom_vals), kc_len))
            history_hist.append(round(val, 4))
        else:
            history_hist.append(0)

    # Current state
    curr_in_sq  = history_dots[-1] == 'red'   if history_dots else False
    prev_in_sq  = history_dots[-2] == 'red'   if len(history_dots) >= 2 else False
    fired       = prev_in_sq and not curr_in_sq  # was red, now green

    # Squeeze days — how many consecutive red dots
    sq_days = 0
    for dot in reversed(history_dots):
        if dot == 'red': sq_days += 1
        else: break

    # Momentum direction
    curr_mom = history_hist[-1] if history_hist else 0
    prev_mom = history_hist[-2] if len(history_hist) >= 2 else 0
    if curr_mom > prev_mom: mom_dir = 'up'
    elif curr_mom < prev_mom: mom_dir = 'down'
    else: mom_dir = 'flat'

    # BB width % (how tight the bands are)
    if bb_basis[-1] and bb_std[-1] and bb_basis[-1] != 0:
        bb_w = (bb_std[-1] * 4) / bb_basis[-1] * 100
    else:
        bb_w = None

    strength = round(sq_days * abs(curr_mom) * 100, 2) if sq_days > 0 else 0

    return {
        'in_squeeze':    curr_in_sq,
        'squeeze_fired': fired,
        'momentum':      round(curr_mom, 4),
        'momentum_dir':  mom_dir,
        'squeeze_days':  sq_days,
        'strength_score': strength,
        'fired_bullish': fired and curr_mom > 0 and mom_dir == 'up',
        'fired_bearish': fired and curr_mom < 0 and mom_dir == 'down',
        'bb_width_pct':  round(bb_w, 2) if bb_w else None,
        'dots':          history_dots[-20:],
        'hist':          history_hist[-20:],
    }


def detect_bb_squeeze(prices, highs, lows, n=20):
    """Backward-compatible wrapper — uses full TTM Squeeze now."""
    return calc_ttm_squeeze(prices, highs, lows)

def detect_vcp(prices: list, volumes: list, highs: list, lows: list) -> dict:
    """
    VCP (Volatility Contraction Pattern) - Minervini style.
    Looks for 2-4 contracting pullbacks, each shallower than the last,
    with declining volume on each pullback, price near top of range.
    """
    empty = {'is_vcp': False, 'vcp_stage': 0, 'contractions': [], 'vcp_fired': False}
    n = len(prices)
    if n < 60:
        return empty

    # Find swing highs and lows in last 60 days using simple pivot detection
    window = 60
    sub_p = prices[-window:]
    sub_v = volumes[-window:]
    sub_h = highs[-window:]
    sub_l = lows[-window:]

    pivots = []  # list of (idx, price, type) type: 'H' or 'L'
    for i in range(3, len(sub_p) - 3):
        if sub_h[i] == max(sub_h[i-3:i+4]):
            pivots.append((i, sub_h[i], 'H'))
        elif sub_l[i] == min(sub_l[i-3:i+4]):
            pivots.append((i, sub_l[i], 'L'))

    # Build alternating H-L sequence
    contractions = []
    last_type = None
    sequence = []
    for idx, price, typ in pivots:
        if typ != last_type:
            sequence.append((idx, price, typ))
            last_type = typ

    # Find H-L-H-L patterns and measure pullback %
    i = 0
    while i < len(sequence) - 1:
        if sequence[i][2] == 'H' and i+1 < len(sequence) and sequence[i+1][2] == 'L':
            high_price = sequence[i][1]
            low_price  = sequence[i+1][1]
            pullback_pct = round((high_price - low_price) / high_price * 100, 1)
            contractions.append(pullback_pct)
        i += 1

    # Keep only the most recent 2-4 contractions
    recent_contractions = contractions[-4:] if len(contractions) >= 2 else []

    # VCP valid if each contraction is smaller than the previous (contracting)
    is_contracting = False
    if len(recent_contractions) >= 2:
        is_contracting = all(
            recent_contractions[i] > recent_contractions[i+1] * 0.95  # allow small tolerance
            for i in range(len(recent_contractions)-1)
        )

    # Price should be within 15% of 52-week high (tight area)
    high_252 = max(prices[-252:]) if len(prices) >= 252 else max(prices)
    last_price = prices[-1]
    pct_from_high = (last_price - high_252) / high_252 * 100

    # Volume should be drying up (last 5 days avg < 20 day avg)
    vol_5d  = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    vol_20d = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
    vol_drying = vol_5d < vol_20d * 0.8

    is_vcp = (
        is_contracting and
        len(recent_contractions) >= 2 and
        pct_from_high >= -20 and
        vol_drying
    )

    # VCP fired = was VCP, now breaking out with volume (today vol > 20d avg * 1.5)
    today_vol_ratio = volumes[-1] / vol_20d if vol_20d > 0 else 0
    price_breaking = prices[-1] > prices[-2] if len(prices) > 1 else False
    vcp_fired = is_vcp and today_vol_ratio >= 1.5 and price_breaking

    return {
        'is_vcp': is_vcp,
        'vcp_stage': len(recent_contractions),
        'contractions': recent_contractions,
        'vcp_fired': vcp_fired,
        'vol_drying': vol_drying,
        'pct_from_high': round(pct_from_high, 1),
    }

def calc_rs_raw(prices: list, end_idx: int = None) -> Optional[float]:
    """Original IBD-style raw RS score — kept for RS history sparkline trend calc."""
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

def calc_rs_tv_raw(prices: list, bench_prices: list, end_idx: int = None) -> Optional[float]:
    """
    TradingView / Lakshmi Mata Pine Script RS formula.
    Matches exactly:
        perf(src, len) => (src - src[len]) / src[len] * 100
        r3m  = perf(close, 63)  - perf(benchClose, 63)
        r6m  = perf(close, 126) - perf(benchClose, 126)
        r9m  = perf(close, 189) - perf(benchClose, 189)
        r12m = perf(close, 252) - perf(benchClose, 252)
        rawRS = r3m*0.4 + r6m*0.2 + r9m*0.2 + r12m*0.2
    Returns the raw weighted relative-to-benchmark score (not yet normalized).
    """
    end   = end_idx if end_idx is not None else len(prices) - 1
    bend  = end_idx if end_idx is not None else len(bench_prices) - 1

    # Need at least 252 bars for both stock and benchmark
    if end < 252 or bend < 252:
        return None
    if len(bench_prices) <= bend:
        return None

    def perf(arr, idx, lag):
        i = idx - lag
        if i < 0 or arr[i] == 0:
            return None
        return (arr[idx] - arr[i]) / arr[i] * 100

    sp63  = perf(prices,       end,  63)
    bp63  = perf(bench_prices, bend, 63)
    sp126 = perf(prices,       end,  126)
    bp126 = perf(bench_prices, bend, 126)
    sp189 = perf(prices,       end,  189)
    bp189 = perf(bench_prices, bend, 189)
    sp252 = perf(prices,       end,  252)
    bp252 = perf(bench_prices, bend, 252)

    if any(v is None for v in [sp63,bp63,sp126,bp126,sp189,bp189,sp252,bp252]):
        return None

    r3m  = sp63  - bp63
    r6m  = sp126 - bp126
    r9m  = sp189 - bp189
    r12m = sp252 - bp252

    return r3m * 0.4 + r6m * 0.2 + r9m * 0.2 + r12m * 0.2

def calc_rs_tv_normalized(prices: list, bench_prices: list, end_idx: int = None) -> Optional[int]:
    """
    Full TradingView RS Rating — raw score normalized via the stock's OWN
    252-day min/max rawRS range, exactly matching the Pine Script:
        rsHigh = ta.highest(rawRS, 252)
        rsLow  = ta.lowest(rawRS,  252)
        rsRating = round(((rawRS - rsLow) / (rsHigh - rsLow)) * 98 + 1)
    Returns an integer 1-99, or None if insufficient data.
    """
    end = end_idx if end_idx is not None else len(prices) - 1

    # Need at least 252 bars for the raw RS calculation
    if end < 252 or len(bench_prices) < 252:
        # For stocks with less than 252 days, use simpler relative strength
        # Just compare stock return vs Nifty return over available history
        if end < 60 or len(bench_prices) < 60:
            return None
        n = min(end, len(bench_prices)-1, 60)
        s_ret = (prices[end] - prices[end-n]) / prices[end-n] * 100 if prices[end-n] else None
        b_ret = (bench_prices[n] - bench_prices[0]) / bench_prices[0] * 100 if bench_prices[0] else None
        if s_ret is None or b_ret is None:
            return None
        diff = s_ret - b_ret
        # Scale to 1-99
        return max(1, min(99, int(50 + diff * 2)))

    # Compute today's rawRS
    current_raw = calc_rs_tv_raw(prices, bench_prices, end_idx=end)
    if current_raw is None:
        return None

    # Build rawRS history for normalization (up to 252 lookback days)
    raw_history = []
    lookback_days = min(252, end - 252)
    for d in range(lookback_days, -1, -1):
        idx = end - d
        if idx < 252:
            continue
        raw = calc_rs_tv_raw(prices, bench_prices, end_idx=idx)
        if raw is not None:
            raw_history.append(raw)

    if len(raw_history) < 5:
        return 50

    rs_high = max(raw_history)
    rs_low  = min(raw_history)

    if rs_high == rs_low:
        return 50  # Pine Script returns 50 when range is zero (flat stock)

    rating = round(((current_raw - rs_low) / (rs_high - rs_low)) * 98 + 1)
    return max(1, min(99, rating))

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

async def build_rs_history(all_stocks: list, days: int = 15) -> dict:
    """Build 15-day RS history for all stocks. Async so it can yield to the
    event loop periodically — without this, 15 days x 2400 stocks of
    synchronous work can stall the process for a noticeable stretch."""
    if not all_stocks:
        return {}
    # Only use stocks with enough price history
    valid = [s for s in all_stocks if s.get('prices') and len(s['prices']) >= 60]
    if not valid:
        log.warning("No stocks with sufficient history — skipping RS history")
        return {s['sym']: [None]*days for s in all_stocks}
    n = len(valid[0]['prices'])
    history = {s['sym']: [] for s in all_stocks}
    for d in range(days-1, -1, -1):
        end_idx = n - 1 - d
        raw_map = {}
        for s in valid:
            try:
                raw = calc_rs_raw(s['prices'], end_idx)
                if raw is not None:
                    raw_map[s['sym']] = raw
            except Exception:
                pass
        raw_vals = list(raw_map.values())
        for s in all_stocks:
            if s['sym'] in raw_map and raw_vals:
                history[s['sym']].append(percentile_rank(raw_vals, raw_map[s['sym']]))
            else:
                history[s['sym']].append(None)
        await asyncio.sleep(0)  # yield to event loop after each of the 15 days
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
    "IT": ["TCS","INFY","WIPRO","HCLTECH","TECHM","MPHASIS","PERSISTENT","COFORGE",
           "LTTS","KPITTECH","TATAELXSI","OFSS","LTIM","NIITTECH","HEXAWARE","MASTEK",
           "ZENSAR","NIIT","ECLERX","BIRLASOFT","RATEGAIN","NEWGEN","TANLA","INTELLECT"],
    "Banking": ["HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","INDUSINDBK",
                "BANDHANBNK","FEDERALBNK","IDFCFIRSTB","RBLBANK","YESBANK","CANARABANK",
                "BANKBARODA","PNB","UNIONBANK","MAHABANK","UCOBANK","CENTRALBK","IOB",
                "INDIANB","BANKINDIA","DCBBANK","SOUTHBANK","KARURVYSYA","CSBBANK"],
    "NBFC": ["BAJFINANCE","BAJAJFINSV","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","M&MFIN",
             "SHRIRAMFIN","LICHSGFIN","PNBHOUSING","CANFINHOME","AAVAS","HOMEFIRST",
             "APTUS","CREDITACC","SPANDANA","UGROCAP","SATIN"],
    "Insurance": ["HDFCLIFE","SBILIFE","ICICIPRULI","LICI","STARHEALTH","NIACL","GICRE",
                  "ICICIGI","BAJAJHLDNG","MAXFINSERV"],
    "Pharma": ["SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","BIOCON","AUROPHARMA","LUPIN",
               "TORNTPHARM","ALKEM","IPCALAB","PFIZER","GLAXO","ABBOTINDIA","SANOFI",
               "GLAND","LAURUSLABS","GRANULES","NATCOPHARM","AJANTPHARM","JBCHEPHARM",
               "ERIS","SHILPAMED","SUVEN","NEULANDLAB","SEQUENT"],
    "Auto": ["MARUTI","TATAMOTORS","M&M","BAJAJ-AUTO","HEROMOTOCO","EICHERMOT",
             "TVSMOTORS","ASHOKLEY","TVSMOTOR","ATUL","ESCORTS","FORCE","SML"],
    "Auto Ancil": ["MOTHERSON","BOSCHLTD","BHARATFORG","SUNDRMFAST","EXIDEIND","AMARARAJA",
                   "MRF","APOLLOTYRE","CEATLTD","BALKRISIND","TIINDIA","FIEM","SUPRAJIT",
                   "MINDA","LUMAX","ENDURANCE","GABRIEL","SUBROS","JAMNA","RACL"],
    "FMCG": ["HINDUNILVR","ITC","NESTLEIND","BRITANNIA","DABUR","MARICO","GODREJCP",
              "COLPAL","EMAMILTD","TATACONSUM","VBL","RADICO","MCDOWELL-N","UNITEDSPIRITS",
              "PGHH","GILLETTE","HONASA","BIKAJI","DOMS","DEVYANI","SAPPHIRE","JUBLFOOD",
              "WESTLIFE","BARBEQUE","THANGAMALY","VAIBHAVGBL"],
    "Cement": ["ULTRACEMCO","SHREECEM","AMBUJACEMENT","ACC","DALMIACEM","JKCEMENT",
               "RAMCOCEM","HEIDELBERG","PRISMJOINTS","STARCEMENT","NUVOCO","BIRLACORPN",
               "ORIENTCEM","SAGCEM","MANGCEM"],
    "Steel & Metal": ["TATASTEEL","JSWSTEEL","SAIL","HINDALCO","NATIONALUM","NMDC","MOIL",
                      "VEDL","HINDCOPPER","COALINDIA","APLAPOLLO","JSPL","RATNAMANI",
                      "WELSPUNLIVING","KALYANKJIL","MSTCLTD"],
    "Energy & Oil": ["RELIANCE","ONGC","BPCL","IOC","HPCL","GAIL","PETRONET","OIL",
                     "HINDPETRO","MGL","IGL","GSPL","GUJGASLTD","ATGL","AEGISCHEM"],
    "Power": ["NTPC","POWERGRID","ADANIGREEN","TATAPOWER","CESC","TORNTPOWER","JPPOWER",
              "RPOWER","SJVN","NHPC","RVNL","IRCON","POWERMECH","KEC","KALPATPOWR",
              "RITES","ENGINERSIN","BHEL"],
    "Realty": ["DLF","GODREJPROP","OBEROIRLTY","PHOENIXLTD","PRESTIGE","BRIGADE","SOBHA",
               "MAHINDRALIFESC","KOLTEPATIL","SUNTECK","ANANTRAJ","HEMISPHEREP","KEYSTONE"],
    "Engineering": ["LTIM","LT","SIEMENS","ABB","HONAUT","CUMMINSIND","THERMAX","BFUTILITIE",
                    "KIRLOSENG","KIRLOSKARIND","ELGIEQUIP","GRINDWELL","GREAVESCOT",
                    "LLOYDSME","LLOYDSENGG","GMRINFRA","AIAENG","RAMKRISHNA","JYOTHYLAB"],
    "Defence": ["HAL","BEL","MAZDOCK","GRSE","COCHINSHIP","BEML","MIDHANI","DCXSYS","KERNEX","MTAR","DATAPATTNS","CENTUM","ASTRAMICRO","IDEAFORGE","PARAS","ZEN","SOLARINDS","DYNAMATECH","NEWSPACE","AVANTEL","ELCOM","RTNPOWER"],
    "Capital Goods": ["BHEL","TITAGARH","TEXRAIL","RAILTEL","IRFC","IRCTC","CONCOR",
                      "MAHINDCIE","SCHAEFFLER","SKFINDIA","TIMKEN","NRB","IGARASHI"],
    "Chemicals": ["PIDILITIND","ATUL","DEEPAKNTR","NAVINFLUOR","CLEAN","FINEORG","NOCIL",
                  "VINATI","ROSSARI","TATACHEM","GNFC","GSFC","DFMFOODS","BALRAMCHIN",
                  "DHANUKA","RALLIS","SUMITCHEM","BAYER","BASF","INSECTICID"],
    "Textile": ["PAGEIND","DOLLAR","RAYMOND","ARVIND","VARDHMAN","TRIDENT","WELSPUNIND",
                "GOKEX","KITEX","NITIN","NAHARSPG","FILATEX","SPANDEX"],
    "Telecom": ["BHARTIARTL","IDEA","TATACOMM","HFCL","STLTECH","RAILTEL","TEJAS"],
    "Media": ["ZEEL","SUNTV","PVRINOX","INOXWIND","SAREGAMA","TIPS","BALAJITELE"],
    "Retail": ["TRENT","ABFRL","SHOPERSTOP","VMART","SPENCERS","NYKAA","MEESHO"],
    "Hospital & Health": ["APOLLOHOSP","FORTIS","ASTER","MEDANTA","RAINBOW","VIJAYAHOSP",
                          "KIMS","METROPOLIS","THYROCARE","LALPATHLAB","KRSNAA"],
    "Hotel & Travel": ["INDHOTEL","LEMONTREE","MAHINDRAHOLIDAYS","EIH","CHALET","THOMASCOOK",
                       "IRCTC","EASEMYTRIP"],
    "Agriculture": ["UPL","COROMANDEL","KSCL","KAVERI","NUZIVEEDU","GODREJAGRO","JAINIRRIG",
                    "TATACHEM","CHAMBAL","IFFCO"],
    "Logistics": ["BLUEDART","DELHIVERY","MAHLOG","VRL","TCI","ALLCARGO","GATI","XPRO"],
    "IT Services": ["WIPRO","NIIT","APTECH","CAMS","CDSL","BSE","MCX","ANGELONE",
                    "FINCABLES","POLICYBAZAAR","PAYTM","NSDL"],
}

def get_sector(sym: str) -> str:
    for sector, stocks in SECTOR_MAP.items():
        if sym in stocks:
            return sector
    return 'Other'


def get_sector(sym: str) -> str:
    for sector, stocks in SECTOR_MAP.items():
        if sym in stocks:
            return sector
    return "Other"

# ── Upstox API ────────────────────────────────────────────────────────
NIFTY50   = ["RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK","INFOSYS","SBIN","HINDUNILVR","ITC","LT","KOTAKBANK","HCLTECH","AXISBANK","BAJFINANCE","MARUTI","ASIANPAINT","SUNPHARMA","TITAN","ULTRACEMCO","NESTLEIND","WIPRO","NTPC","POWERGRID","TECHM","TATAMOTORS","ADANIENT","ADANIPORTS","ONGC","BAJAJFINSV","JSWSTEEL","TATASTEEL","COALINDIA","HINDALCO","M&M","DRREDDY","CIPLA","EICHERMOT","DIVISLAB","BPCL","GRASIM","INDUSINDBK","APOLLOHOSP","BAJAJ-AUTO","HEROMOTOCO","TVSMOTOR","SHREECEM","BRITANNIA","VEDL","BEL","NTPC"]
MIDCAP = [
    # Nifty Midcap 150 — full list
    "MPHASIS","PERSISTENT","COFORGE","LTTS","TATAELXSI","BANDHANBNK","FEDERALBNK",
    "IDFCFIRSTB","RBLBANK","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","SHRIRAMFIN",
    "PIIND","ALKEM","TORNTPHARM","IPCALAB","AUROPHARMA","LUPIN","GLAND","LAURUSLABS",
    "GRANULES","AJANTPHARM","JBCHEPHARM","ERIS","SUNDRMFAST","BHARATFORG","EXIDEIND",
    "AMARARAJA","MRF","APOLLOTYRE","CEATLTD","BALKRISIND","TIINDIA","FIEM","SUPRAJIT",
    "MINDA","LUMAX","ENDURANCE","GABRIEL","ESCORTS","MAZDOCK","GRSE","COCHINSHIP",
    "BEML","MIDHANI","DCXSYS","KERNEX","MTAR","DATAPATTNS","CENTUM","ASTRAMICRO",
    "TITAGARH","RAILTEL","IRFC","CONCOR","MAHINDCIE","SCHAEFFLER","SKFINDIA","TIMKEN",
    "AIAENG","RAMKRISHNA","ELGIEQUIP","GRINDWELL","THERMAX","CUMMINSIND","SIEMENS",
    "ABB","HONAUT","BHEL","KEC","KALPATPOWR","RITES","ENGINERSIN","POWERMECH",
    "SJVN","NHPC","JPPOWER","RPOWER","CESC","TORNTPOWER","TATAPOWER","ADANIGREEN",
    "PIDILITIND","ATUL","DEEPAKNTR","NAVINFLUOR","CLEAN","FINEORG","NOCIL","VINATI",
    "ROSSARI","TATACHEM","GNFC","GSFC","DHANUKA","RALLIS","SUMITCHEM","BAYER",
    "UPL","COROMANDEL","KSCL","KAVERI","JAINIRRIG","CHAMBAL","IFFCO",
    "PAGEIND","DOLLAR","RAYMOND","ARVIND","VARDHMAN","TRIDENT","WELSPUNIND",
    "GOKEX","KITEX","NITIN","NAHARSPG","DLF","GODREJPROP","OBEROIRLTY","PHOENIXLTD",
    "PRESTIGE","BRIGADE","SOBHA","KOLTEPATIL","SUNTECK","ANANTRAJ","KEYSTONE",
    "APOLLOHOSP","FORTIS","ASTER","MEDANTA","RAINBOW","METROPOLIS","THYROCARE",
    "LALPATHLAB","INDHOTEL","LEMONTREE","EIH","CHALET","IRCTC","EASEMYTRIP",
    "BLUEDART","DELHIVERY","VRL","TCI","ALLCARGO","TRENT","ABFRL","SHOPERSTOP",
    "VMART","NYKAA","ZEEL","SUNTV","PVRINOX","SAREGAMA","TIPS","BALAJITELE",
    "ANGELONE","CDSL","BSE","MCX","CAMS","POLICYBAZAAR","PAYTM",
    "RELAXO","LLOYDSENGG","THANGAMALY","VAIBHAVGBL","DEVYANI","SAPPHIRE",
    "JUBLFOOD","WESTLIFE","BARBEQUE","BIKAJI","DOMS","HONASA",
]
SMALLCAP = [
    # Nifty Smallcap 250 — representative list
    "DELTACORP","GMRINFRA","IDEA","SUZLON","UNITECH","DISHTV","JPASSOCIAT",
    "PVRLTD","NATIONALUM","HINDALCO","VEDL","HINDCOPPER","COALINDIA","NMDC","MOIL",
    "APLAPOLLO","JSPL","RATNAMANI","WELSPUNLIVING","KALYANKJIL","MSTCLTD",
    "HFCL","STLTECH","TEJAS","RATEGAIN","NEWGEN","TANLA","INTELLECT","ECLERX",
    "BIRLASOFT","MASTEK","ZENSAR","NIIT","APTECH","HEXAWARE","OFSS",
    "TATACONSUMER","RADICO","MCDOWELL","UNITEDSPIRITS","PGHH","GILLETTE",
    "VARUN","KRBL","LT","LTIM","PERSISTENT","COFORGE","TATAELXSI",
    "ASHOKLEY","TVSMOTOR","FORCE","SML","MOTHERSON","GABRIEL","SUBROS","JAMNA",
    "RACL","SUPRAJIT","FIEM","MINDA","LUMAX","ENDURANCE","BALKRISIND",
    "APOLLOTYRE","CEATLTD","EXIDEIND","AMARARAJA","MRF",
    "IDEAFORGE","PARAS","ZEN","SOLARINDS","DYNAMATECH","NEWSPACE","AVANTEL",
    "ELCOM","RTNPOWER","CENTUM","ASTRAMICRO","KERNEX","MTAR","DATAPATTNS",
    "RAILTEL","RITES","ENGINERSIN","TITAGARH","KEC","KALPATPOWR","POWERMECH",
    "SJVN","NHPC","JPPOWER","CESC","TORNTPOWER",
    "ROSSARI","NOCIL","FINEORG","VINATI","CLEAN","DEEPAKNTR","NAVINFLUOR",
    "GNFC","GSFC","DHANUKA","RALLIS","SUMITCHEM","BAYER","INSECTICID",
    "PAGEIND","DOLLAR","RAYMOND","ARVIND","VARDHMAN","TRIDENT","WELSPUNIND",
    "GOKEX","KITEX","NITIN","NAHARSPG","FILATEX","SPANDEX",
    "KOLTEPATIL","SUNTECK","ANANTRAJ","KEYSTONE","SOBHA","BRIGADE","PRESTIGE",
    "METROPOLIS","THYROCARE","LALPATHLAB","KRSNAA","VIJAYAHOSP","KIMS",
    "LEMONTREE","MAHINDRAHOLIDAYS","EIH","CHALET","THOMASCOOK","EASEMYTRIP",
    "BLUEDART","DELHIVERY","VRL","TCI","ALLCARGO","GATI","XPRO",
    "VMART","SPENCERS","NYKAA","MEESHO","SHOPERSTOP",
    "SAREGAMA","TIPS","BALAJITELE","ANGELONE","CDSL","BSE","MCX","CAMS",
    "RELAXO","BIKAJI","DOMS","HONASA","DEVYANI","SAPPHIRE","WESTLIFE","BARBEQUE",
    "AAVAS","HOMEFIRST","APTUS","CREDITACC","SPANDANA","UGROCAP","SATIN",
    "STARHEALTH","NIACL","GICRE","ICICIGI","BAJAJHLDNG","MAXFINSERV",
    "HDFCLIFE","SBILIFE","ICICIPRULI","LICI",
    "KARURVYSYA","CSBBANK","DCBBANK","SOUTHBANK","YESBANK","RBLBANK",
]

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

async def fetch_index_csv(session: aiohttp.ClientSession, url: str) -> list:
    """Download an NSE index constituent CSV and return list of trading symbols."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/csv,*/*",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                log.warning(f"Index CSV fetch failed ({url}): HTTP {r.status}")
                return []
            raw = await r.read()
            text = raw.decode('utf-8', errors='ignore')

            import csv, io
            reader = csv.DictReader(io.StringIO(text))
            symbols = []
            for row in reader:
                # NSE CSVs use a "Symbol" column (case can vary slightly)
                sym = None
                for key in row:
                    if key and key.strip().lower() == 'symbol':
                        sym = row[key]
                        break
                if sym:
                    sym = sym.strip().upper()
                    if sym:
                        symbols.append(sym)
            return symbols
    except Exception as e:
        log.warning(f"Index CSV fetch error ({url}): {e}")
        return []

async def load_official_index_lists(session: aiohttp.ClientSession):
    """
    Load index constituents:
    1. Try to load from Supabase (fast, works offline)
    2. If stale/empty, fetch from niftyindices.com CSV
    3. Save fresh data back to Supabase
    Falls back to hardcoded lists only if everything fails.
    """
    global NIFTY50, MIDCAP, SMALLCAP, MICROCAP, ALL_STOCKS

    SUPABASE_REFRESH_DAYS = 7  # refresh weekly

    # ── Step 1: Try loading from Supabase ──────────────────────────
    try:
        url = f"{SUPABASE_URL}/rest/v1/index_constituents?select=index_name,symbols,updated_at&order=updated_at.desc"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                rows = await r.json()
                if rows:
                    # Check if data is fresh enough
                    from datetime import timezone
                    latest = rows[0].get('updated_at','')
                    if latest:
                        updated = datetime.fromisoformat(latest.replace('Z','+00:00'))
                        age_days = (datetime.now(timezone.utc) - updated).days
                        if age_days <= SUPABASE_REFRESH_DAYS:
                            # Use cached data
                            for row in rows:
                                syms = row.get('symbols', [])
                                if isinstance(syms, str):
                                    import json as _json
                                    syms = _json.loads(syms)
                                name = row.get('index_name','')
                                if name == 'NIFTY50'   and len(syms) >= 40:  NIFTY50   = syms
                                if name == 'MIDCAP150' and len(syms) >= 100: MIDCAP    = syms
                                if name == 'SMALLCAP250' and len(syms) >= 150: SMALLCAP = syms
                                if name == 'MICROCAP250' and len(syms) >= 150: MICROCAP = syms
                            log.info(f"✅ Loaded from Supabase: N50={len(NIFTY50)} MID={len(MIDCAP)} SML={len(SMALLCAP)}")
                            return
                        else:
                            log.info(f"Index data is {age_days}d old — refreshing from niftyindices.com")
    except Exception as e:
        log.warning(f"Supabase index load failed: {e}")

    # ── Step 2: Fetch fresh from niftyindices.com ──────────────────
    log.info("Fetching official NSE index constituent lists from niftyindices.com…")
    results = await asyncio.gather(
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['NIFTY50']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['MIDCAP150']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['SMALLCAP250']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['MICROCAP250']),
        return_exceptions=True,
    )
    fresh_n50, fresh_mid, fresh_sml, fresh_mic = [
        r if isinstance(r, list) else [] for r in results
    ]

    updated_any = False

    if len(fresh_n50) >= 40:
        NIFTY50 = fresh_n50
        log.info(f"✅ Nifty 50: {len(NIFTY50)} stocks")
        updated_any = True
    else:
        log.warning(f"⚠️ Nifty 50 fetch returned {len(fresh_n50)} — keeping existing {len(NIFTY50)}")

    if len(fresh_mid) >= 100:
        MIDCAP = fresh_mid
        log.info(f"✅ Midcap 150: {len(MIDCAP)} stocks")
        updated_any = True
    else:
        log.warning(f"⚠️ Midcap fetch returned {len(fresh_mid)} — keeping existing {len(MIDCAP)}")

    if len(fresh_sml) >= 150:
        SMALLCAP = fresh_sml
        log.info(f"✅ Smallcap 250: {len(SMALLCAP)} stocks")
        updated_any = True
    else:
        log.warning(f"⚠️ Smallcap fetch returned {len(fresh_sml)} — keeping existing {len(SMALLCAP)}")

    if len(fresh_mic) >= 150:
        MICROCAP = fresh_mic
        log.info(f"✅ Microcap 250: {len(MICROCAP)} stocks")
        updated_any = True
    else:
        log.warning(f"⚠️ Microcap fetch returned {len(fresh_mic)} — keeping existing {len(MICROCAP)}")

    # ── Step 3: Save fresh data to Supabase ────────────────────────
    if updated_any:
        try:
            import json as _json
            now_iso = datetime.now(IST).isoformat()
            rows = [
                {"index_name": "NIFTY50",    "symbols": _json.dumps(NIFTY50),   "updated_at": now_iso},
                {"index_name": "MIDCAP150",  "symbols": _json.dumps(MIDCAP),    "updated_at": now_iso},
                {"index_name": "SMALLCAP250","symbols": _json.dumps(SMALLCAP),  "updated_at": now_iso},
                {"index_name": "MICROCAP250","symbols": _json.dumps(MICROCAP),  "updated_at": now_iso},
            ]
            url = f"{SUPABASE_URL}/rest/v1/index_constituents?on_conflict=index_name"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            }
            async with session.post(url, headers=headers, json=rows,
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (200,201,204):
                    log.info(f"✅ Index constituents saved to Supabase (refreshes weekly)")
                else:
                    log.warning(f"⚠️ Failed to save to Supabase: {r.status}")
        except Exception as e:
            log.warning(f"Could not save index constituents: {e}")


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

async def fetch_fundamentals_screener(session: aiohttp.ClientSession, sym: str) -> dict:
    """
    Scrape fundamental data from Screener.in company page.
    Free, no auth needed. Returns: market_cap, pe, roe, eps, debt_eq, promoter.
    """
    url = f"https://www.screener.in/company/{sym}/consolidated/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    result = {
        'market_cap': None, 'pe': None, 'roe': None,
        'eps': None, 'debt_eq': None, 'promoter': None,
    }
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 404:
                # Try standalone (non-consolidated)
                url2 = f"https://www.screener.in/company/{sym}/"
                async with session.get(url2, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    if r2.status != 200:
                        return result
                    html = await r2.text()
            elif r.status != 200:
                return result
            else:
                html = await r.text()

        # Parse key ratios from the #top-ratios list
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

        # Promoter holding — from shareholding section
        prom_m = re.search(r'Promoters?\s*</td>\s*<td[^>]*>([\d.]+)%?</td>', html, re.IGNORECASE)
        if prom_m:
            result['promoter'] = float(prom_m.group(1))
        else:
            # Alternative pattern
            prom_m2 = re.search(r'"promoters":\s*([\d.]+)', html, re.IGNORECASE)
            if prom_m2:
                result['promoter'] = float(prom_m2.group(1))

    except Exception as e:
        pass
    return result

# Cache fundamentals to avoid re-fetching every minute
fundamentals_cache: dict = {}  # sym -> {market_cap, pe, roe, eps, debt_eq, promoter, fetched_at}
FUNDAMENTALS_TTL = 7 * 24 * 3600  # refresh weekly (data changes quarterly)

async def load_fundamentals_batch(session: aiohttp.ClientSession, symbols: list):
    """Fetch fundamentals for a batch of symbols, respecting TTL cache."""
    now = time.time()
    to_fetch = [
        sym for sym in symbols
        if sym not in fundamentals_cache
        or (now - fundamentals_cache[sym].get('fetched_at', 0)) > FUNDAMENTALS_TTL
    ]
    if not to_fetch:
        return

    log.info(f"  Fetching fundamentals for {len(to_fetch)} stocks from Screener.in…")
    BATCH = 5  # small batches to be respectful
    fetched = 0
    for i in range(0, len(to_fetch), BATCH):
        batch = to_fetch[i:i+BATCH]
        results = await asyncio.gather(*[
            fetch_fundamentals_screener(session, sym) for sym in batch
        ])
        for sym, data in zip(batch, results):
            data['fetched_at'] = now
            fundamentals_cache[sym] = data
            if any(v is not None for k, v in data.items() if k != 'fetched_at'):
                fetched += 1
        await asyncio.sleep(1)  # be gentle with Screener.in

    log.info(f"  Fundamentals loaded: {fetched}/{len(to_fetch)} stocks")


    """Fetch OHLC for instruments in one call. Keep batch small — GET URL length limits apply."""
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
        async with session.get(url, headers=headers, params=params,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            text = await r.text()
            if r.status != 200:
                log.warning(f"OHLC fetch failed: {r.status} — {text[:300]}")
                return {}
            try:
                data = json.loads(text)
            except Exception:
                log.warning(f"OHLC response not JSON: {text[:200]}")
                return {}
            result = data.get('data', {})
            if not result:
                log.warning(f"OHLC empty data field. Full response keys: {list(data.keys())} status={data.get('status')}")
            return result
    except Exception as e:
        log.error(f"OHLC fetch error: {e}")
        return {}

async def fetch_historical(session: aiohttp.ClientSession, sym: str,
                           instrument_key: str = None) -> dict:
    """Fetch 15 months of daily historical data for one stock."""
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=400)).strftime('%Y-%m-%d')

    # Use provided instrument_key or build from symbol
    key = instrument_key if instrument_key else f"NSE_EQ|{sym}"
    encoded_key = key.replace('|', '%7C')
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/day/{to}/{from_}"

    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {}
            data = await r.json()
            candles = list(reversed(data.get('data', {}).get('candles', [])))
            return {
                'prices':  [c[4] for c in candles],  # close
                'volumes': [c[5] for c in candles],  # volume
                'highs':   [c[2] for c in candles],
                'lows':    [c[3] for c in candles],
            }
    except Exception as e:
        return {}

# ── Supabase client ───────────────────────────────────────────────────
async def supabase_upsert(session: aiohttp.ClientSession, table: str, rows: list, on_conflict: str = None):
    """Upsert rows into Supabase table in parallel chunks for speed."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    CHUNK = 500  # larger chunks = fewer round trips

    async def upsert_chunk(chunk):
        try:
            async with session.post(url, headers=headers, json=chunk,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status not in (200, 201, 204):
                    text = await r.text()
                    log.warning(f"Supabase upsert {table} failed: {r.status} {text[:100]}")
        except Exception as e:
            log.error(f"Supabase error ({table}): {e}")

    # Fire all chunks concurrently (max 4 at a time)
    chunks = [rows[i:i+CHUNK] for i in range(0, len(rows), CHUNK)]
    sem = asyncio.Semaphore(4)
    async def upsert_with_sem(chunk):
        async with sem:
            await upsert_chunk(chunk)
    await asyncio.gather(*[upsert_with_sem(c) for c in chunks])

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

# ── Squeeze fire state tracking ──────────────────────────────────────
# Track which stocks were firing last scan — only alert on NEW fires
# Format: {sym: {'bb': bool, 'vcp': bool}}
prev_squeeze_state: dict = {}


historical_cache: dict = {}   # sym -> {prices, volumes}
nifty_cache: dict = {}        # {'prices': [...]} — Nifty index daily closes for TV RS calc

NIFTY_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"  # Upstox key for Nifty 50 index

# All indices to track on the Index Dashboard page
# Key = display name, value = Upstox instrument key
INDEX_TRACKER = {
    "Nifty 50":       "NSE_INDEX|Nifty 50",
    "Nifty Next 50":  "NSE_INDEX|Nifty Next 50",
    "Nifty 500":      "NSE_INDEX|Nifty 500",
    "Midcap 150":     "NSE_INDEX|Nifty Midcap 150",
    "Smallcap 250":   "NSE_INDEX|Nifty Smallcap 250",
    "Microcap 250":   "NSE_INDEX|Nifty Microcap 250",
    "Bank Nifty":     "NSE_INDEX|Nifty Bank",
    "IT":             "NSE_INDEX|Nifty IT",
    "Pharma":         "NSE_INDEX|Nifty Pharma",
    "Auto":           "NSE_INDEX|Nifty Auto",
    "FMCG":           "NSE_INDEX|Nifty FMCG",
    "Metal":          "NSE_INDEX|Nifty Metal",
    "Realty":         "NSE_INDEX|Nifty Realty",
    "Energy":         "NSE_INDEX|Nifty Energy",
}

# Weekly and hourly historical data caches for multi-timeframe squeeze
weekly_cache:  dict = {}   # sym -> {prices, highs, lows, volumes}
hourly_cache:  dict = {}   # sym -> {prices, highs, lows, volumes}

# Cache for all index historical data
index_history_cache: dict = {}  # name -> {prices, volumes}

async def fetch_weekly_hourly(session, sym, instrument_key, n_stocks):
    """Fetch weekly (1Y) and hourly (5 days) candles for TTM Squeeze."""
    headers = {"Authorization": f"Bearer {ANALYTICS_TOKEN}", "Accept": "application/json"}
    key_enc = instrument_key.replace('|','%7C') if instrument_key else f"NSE_EQ%7C{sym}"
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    fr1y = (datetime.now(IST) - timedelta(days=400)).strftime('%Y-%m-%d')
    fr5d = (datetime.now(IST) - timedelta(days=7)).strftime('%Y-%m-%d')

    result = {}
    # Weekly candles
    try:
        url = f"https://api.upstox.com/v2/historical-candle/{key_enc}/week/{to}/{fr1y}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                d = await r.json()
                candles = list(reversed(d.get('data',{}).get('candles',[])))
                if candles:
                    result['weekly'] = {
                        'prices': [c[4] for c in candles],
                        'highs':  [c[2] for c in candles],
                        'lows':   [c[3] for c in candles],
                    }
    except Exception: pass

    # Hourly candles
    try:
        url2 = f"https://api.upstox.com/v2/historical-candle/{key_enc}/60minute/{to}/{fr5d}"
        async with session.get(url2, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r2:
            if r2.status == 200:
                d2 = await r2.json()
                candles2 = list(reversed(d2.get('data',{}).get('candles',[])))
                if candles2:
                    result['hourly'] = {
                        'prices': [c[4] for c in candles2],
                        'highs':  [c[2] for c in candles2],
                        'lows':   [c[3] for c in candles2],
                    }
    except Exception: pass
    return result

async def load_weekly_hourly_cache(session):
    """Load weekly + hourly data for all stocks — for TTM multi-timeframe squeeze."""
    global weekly_cache, hourly_cache
    log.info(f"Loading weekly + hourly data for TTM Squeeze ({len(ALL_STOCKS)} stocks)…")
    sem = asyncio.Semaphore(10)
    loaded = 0

    async def fetch_one(sym):
        async with sem:
            ikey = instrument_key_map.get(sym, f"NSE_EQ|{sym}")
            result = await fetch_weekly_hourly(session, sym, ikey, len(ALL_STOCKS))
            if 'weekly' in result: weekly_cache[sym] = result['weekly']
            if 'hourly' in result: hourly_cache[sym] = result['hourly']
            return sym

    # Batch in groups of 50 to avoid overwhelming API
    for i in range(0, len(ALL_STOCKS), 50):
        batch = ALL_STOCKS[i:i+50]
        await asyncio.gather(*[fetch_one(s) for s in batch])
        loaded += len(batch)
        if i % 500 == 0 and i > 0:
            log.info(f"  Weekly/hourly cache: {loaded}/{len(ALL_STOCKS)} stocks")
        await asyncio.sleep(0.1)

    log.info(f"✅ Weekly/hourly cache: {len(weekly_cache)} weekly, {len(hourly_cache)} hourly")

async def load_index_cache(session: aiohttp.ClientSession):
    """Fetch historical data for all tracked indices.
    First tries Supabase (fast), falls back to Upstox API if stale/empty."""
    global index_history_cache
    import json as _json

    # Try loading from Supabase first (instant restart)
    try:
        url = f"{SUPABASE_URL}/rest/v1/index_price_history?select=index_name,prices,highs,lows,volumes,updated_at"
        headers_sb = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        async with session.get(url, headers=headers_sb,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                rows = await r.json()
                if rows:
                    from datetime import timezone
                    # Check freshness - use if less than 2 days old
                    latest = rows[0].get('updated_at','')
                    if latest:
                        updated = datetime.fromisoformat(latest.replace('Z','+00:00'))
                        age_days = (datetime.now(timezone.utc) - updated).days
                        if age_days <= 2:
                            for row in rows:
                                name = row['index_name']
                                index_history_cache[name] = {
                                    'prices':  _json.loads(row.get('prices','[]')),
                                    'highs':   _json.loads(row.get('highs','[]')),
                                    'lows':    _json.loads(row.get('lows','[]')),
                                    'volumes': _json.loads(row.get('volumes','[]')),
                                }
                            log.info(f"✅ Index price history loaded from Supabase: {len(index_history_cache)} indices (age: {age_days}d)")
                            return
                        else:
                            log.info(f"Index price history is {age_days}d old — refreshing from Upstox")
    except Exception as e:
        log.warning(f"Could not load index prices from Supabase: {e}")
    log.info(f"Loading historical data for {len(INDEX_TRACKER)} indices…")
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=420)).strftime('%Y-%m-%d')
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    loaded = 0
    for name, ikey in INDEX_TRACKER.items():
        encoded = ikey.replace('|', '%7C').replace(' ', '%20')
        url = f"https://api.upstox.com/v2/historical-candle/{encoded}/day/{to}/{from_}"
        # Try multiple key formats - Upstox is inconsistent with index names
        alt_keys = [
            ikey,
            ikey.replace("Nifty Midcap 150", "NIFTY MIDCAP 150"),
            ikey.replace("Nifty Smallcap 250", "NIFTY SMALLCAP 250"),
            ikey.replace("Nifty Microcap 250", "NIFTY MICROCAP 250"),
            ikey.replace("Nifty Next 50", "NIFTY NEXT 50"),
            ikey.replace("Nifty 500", "NIFTY 500"),
        ]
        fetched = False
        for try_key in alt_keys:
            if fetched: break
            try_encoded = try_key.replace('|', '%7C').replace(' ', '%20')
            try_url = f"https://api.upstox.com/v2/historical-candle/{try_encoded}/day/{to}/{from_}"
            try:
                async with session.get(try_url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        data = await r.json()
                        candles = list(reversed(data.get('data', {}).get('candles', [])))
                        if candles:
                            index_history_cache[name] = {
                                'prices':  [c[4] for c in candles],
                                'volumes': [c[5] for c in candles],
                                'highs':   [c[2] for c in candles],
                                'lows':    [c[3] for c in candles],
                            }
                            loaded += 1
                            fetched = True
                    elif r.status != 400:
                        log.warning(f"Index {name} fetch failed: {r.status}")
                        break
            except Exception as e:
                log.warning(f"Index {name} error: {e}")
                break
        if not fetched:
            log.warning(f"Index {name} could not be fetched with any key format")
        await asyncio.sleep(0.2)
    log.info(f"✅ Index cache loaded: {loaded}/{len(INDEX_TRACKER)} indices")

    # Save index price history to Supabase for persistence across restarts
    import json as _json
    rows = []
    for name, data in index_history_cache.items():
        rows.append({
            "index_name": name,
            "prices":     _json.dumps(data.get('prices',[])),
            "highs":      _json.dumps(data.get('highs',[])),
            "lows":       _json.dumps(data.get('lows',[])),
            "volumes":    _json.dumps(data.get('volumes',[])),
            "updated_at": datetime.now(IST).isoformat(),
        })
    if rows:
        url = f"{SUPABASE_URL}/rest/v1/index_price_history?on_conflict=index_name"
        headers_sb = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        try:
            async with session.post(url, headers=headers_sb, json=rows,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status in (200,201,204):
                    log.info(f"✅ Index price history saved to Supabase ({len(rows)} indices)")
                else:
                    txt = await r.text()
                    log.warning(f"⚠️ Index price save failed: {r.status} {txt[:100]}")
        except Exception as e:
            log.warning(f"Could not save index prices: {e}")


async def load_nifty_cache(session: aiohttp.ClientSession):
    """Fetch Nifty 50 daily close history needed for TradingView-style RS calculation."""
    global nifty_cache
    log.info("Fetching Nifty 50 historical data for TV-style RS calc…")
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=420)).strftime('%Y-%m-%d')
    encoded = NIFTY_INSTRUMENT_KEY.replace('|', '%7C').replace(' ', '%20')
    url  = f"https://api.upstox.com/v2/historical-candle/{encoded}/day/{to}/{from_}"
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                text = await r.text()
                log.warning(f"Nifty fetch failed: {r.status} {text[:200]}")
                return
            data = await r.json()
            candles = list(reversed(data.get('data', {}).get('candles', [])))
            nifty_cache = {
                'prices':  [c[4] for c in candles],  # close
                'volumes': [c[5] for c in candles],
            }
            log.info(f"✅ Nifty 50 history: {len(nifty_cache['prices'])} days")
    except Exception as e:
        log.warning(f"Nifty cache load failed: {e}")

instrument_key_map: dict = {} # sym -> full instrument key (e.g. NSE_EQ|INE002A01018)

async def load_instrument_master(session: aiohttp.ClientSession):
    """Fetch Upstox instrument master to get correct instrument keys."""
    global instrument_key_map, ALL_STOCKS
    log.info("Fetching instrument master from Upstox…")
    try:
        # Use the publicly available JSON master file
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status == 200:
                import gzip, io
                content = await r.read()
                try:
                    data = json.loads(gzip.decompress(content))
                except Exception:
                    data = json.loads(content)

                # Build sym -> instrument_key map for EQ stocks
                for item in data:
                    sym = item.get('trading_symbol', '').replace('-EQ', '').replace('EQ', '')
                    itype = item.get('instrument_type', '')
                    exch = item.get('exchange', '')
                    key = item.get('instrument_key', '')
                    if exch == 'NSE' and itype == 'EQ' and sym and key:
                        instrument_key_map[sym] = key

                log.info(f"✅ Instrument master loaded: {len(instrument_key_map)} EQ stocks")

                # Update ALL_STOCKS to only include stocks we have keys for
                if len(instrument_key_map) > 100:
                    ALL_STOCKS = list(instrument_key_map.keys())
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
                    if row.get('exchange') == 'NSE' and row.get('instrument_type') == 'EQ':
                        sym = row.get('trading_symbol', '').replace('-EQ', '')
                        key = row.get('instrument_key', '')
                        if sym and key:
                            instrument_key_map[sym] = key
                log.info(f"✅ CSV master loaded: {len(instrument_key_map)} EQ stocks")
                if len(instrument_key_map) > 100:
                    ALL_STOCKS = list(instrument_key_map.keys())
                return True
    except Exception as e:
        log.warning(f"CSV master also failed: {e} — using symbol-based keys")

    # Last resort: build keys from symbol names (may not always work)
    log.warning("Using symbol-based instrument keys as fallback")
    for sym in ALL_STOCKS:
        instrument_key_map[sym] = f"NSE_EQ|{sym}"
    return False

async def load_historical_cache(session: aiohttp.ClientSession):
    """Load historical data for all stocks at startup."""
    log.info(f"Loading historical data for {len(ALL_STOCKS)} stocks…")
    BATCH = 10
    loaded = 0
    for i in range(0, len(ALL_STOCKS), BATCH):
        batch = ALL_STOCKS[i:i+BATCH]
        results = await asyncio.gather(*[
            fetch_historical(session, sym, instrument_key_map.get(sym))
            for sym in batch
        ])
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
    # Use correct instrument keys from master map
    instrument_keys = [
        instrument_key_map.get(sym, f"NSE_EQ|{sym}")
        for sym in ALL_STOCKS
        if sym in historical_cache  # only fetch stocks we have history for
    ]
    stocks_for_ohlc = [
        sym for sym in ALL_STOCKS
        if sym in historical_cache
    ]

    live_data = {}
    OHLC_BATCH = 500  # Upstox supports 500 per call — use max to reduce round trips
    first_batch_logged = False

    # Fetch all OHLC batches concurrently for maximum speed
    async def fetch_ohlc_batch(batch_keys, batch_syms):
        data = await fetch_bulk_ohlc(session, batch_keys)
        result = {}
        for sym in batch_syms:
            resp_key = f"NSE_EQ:{sym}"
            if resp_key in data:
                result[sym] = data[resp_key]
        return result

    # Split into batches and fire all concurrently (with small concurrency limit)
    batches = [
        (instrument_keys[i:i+OHLC_BATCH], stocks_for_ohlc[i:i+OHLC_BATCH])
        for i in range(0, len(instrument_keys), OHLC_BATCH)
    ]
    # Run up to 5 concurrent OHLC fetches
    sem = asyncio.Semaphore(5)
    async def fetch_with_sem(bkeys, bsyms):
        async with sem:
            return await fetch_ohlc_batch(bkeys, bsyms)

    results = await asyncio.gather(*[fetch_with_sem(bk, bs) for bk, bs in batches])
    for r in results:
        live_data.update(r)

    if live_data:
        sample = list(live_data.keys())[:3]
        log.info(f"  Sample OHLC keys: {[f'NSE_EQ:{s}' for s in sample]}")

    log.info(f"  Live prices: {len(live_data)} stocks")
    log.info(f"  OHLC keys sample: {list(live_data.keys())[:3] if live_data else 'EMPTY'}")

    # Step 2: Only reload full 15-month history once per day, after market
    # close (batch_eod) — this bakes in today's now-final candle as the new
    # baseline for tomorrow. During the day, 'live' scans reuse the cache
    # as-is and just overlay live_data for display, so no re-fetch needed.
    # 'batch_morning' does NOT reload here — the startup sequence already
    # loaded history once before any scan runs, re-loading again on every
    # batch_morning-tagged cycle was wasted API calls and the root cause of
    # repeated multi-minute "stalls" that looked like the live data was
    # not updating.
    if scan_type == 'batch_eod':
        log.info("  End-of-day scan — reloading full historical cache to bake in today's final close…")
        await load_historical_cache(session)
        await load_nifty_cache(session)
        await load_index_cache(session)
        await load_weekly_hourly_cache(session)

    # Step 3: DO NOT mutate historical_cache prices in place (was causing chg% drift).
    # Instead, keep historical close as the immutable baseline and use live price
    # only for today's last/chg calculation downstream.
    # (No-op here intentionally — see Step 5 for safe chg calculation.)

    # Step 4: Calculate RS ratings for all stocks
    stocks_with_hist = [
        {'sym': sym, **historical_cache[sym]}
        for sym in ALL_STOCKS
        if sym in historical_cache and len(historical_cache[sym].get('prices', [])) >= 60
    ]

    if not stocks_with_hist:
        log.warning("⚠️ No stocks with historical data yet — skipping scan, will retry next cycle")
        return 0

    # Raw RS scores
    raw_scores = []
    for s in stocks_with_hist:
        raw = calc_rs_raw(s['prices'])
        if raw is not None:
            raw_scores.append({'sym': s['sym'], 'raw': raw})
    raw_vals = [r['raw'] for r in raw_scores]
    log.info(f"  Computed raw RS scores for {len(raw_scores)} stocks, building 15-day history…")

    # Per-index raw score pools for index-relative RS
    raw_by_sym = {r['sym']: r['raw'] for r in raw_scores}
    nifty50_raws  = [raw_by_sym[s] for s in NIFTY50   if s in raw_by_sym]
    midcap_raws   = [raw_by_sym[s] for s in MIDCAP    if s in raw_by_sym]
    smallcap_raws = [raw_by_sym[s] for s in SMALLCAP  if s in raw_by_sym]
    microcap_raws = [raw_by_sym[s] for s in MICROCAP  if s in raw_by_sym]

    # Per-sector raw score pools for sector-relative RS
    sector_raws = {}  # sector_name -> [raw scores of its members]
    sym_to_sector = {}
    for sym in raw_by_sym:
        sec = get_sector(sym)
        sym_to_sector[sym] = sec
        sector_raws.setdefault(sec, []).append(raw_by_sym[sym])

    # RS history (15 days)
    rs_history = await build_rs_history(stocks_with_hist, days=15)

    # Step 5: Build full stock records
    log.info(f"  Building per-stock records (RS/PP/squeeze/VCP) for {len(stocks_with_hist)} stocks…")

    # Fetch fundamentals only at EOD — they change quarterly, no point fetching more often
    if scan_type == 'batch_eod':
        all_syms = [s['sym'] for s in stocks_with_hist]
        await load_fundamentals_batch(session, all_syms)

    processed = []
    for loop_idx, s in enumerate(stocks_with_hist):
        sym = s['sym']
        prices  = s['prices']
        volumes = s['volumes']
        n = len(prices)

        # Yield control back to the event loop periodically. Without this,
        # the synchronous CPU-bound work below (especially squeeze/VCP math)
        # across ~2400 stocks can block the event loop for minutes straight,
        # freezing heartbeats, timeouts, and Railway health checks.
        if loop_idx % 100 == 0:
            await asyncio.sleep(0)

        # RS (IBD percentile — kept for trend/history sparkline)
        my_raw_val = raw_by_sym.get(sym)
        rs = percentile_rank(raw_vals, my_raw_val) if my_raw_val is not None else 0
        hist = rs_history.get(sym, [])
        trend_data = rs_slope(hist)

        # RS — TradingView / Lakshmi Mata Pine Script formula
        # Benchmark-relative, normalized by stock's own 252-day rawRS range
        rs_tv = None
        nifty_prices = nifty_cache.get('prices', [])
        if len(nifty_prices) >= 252:
            rs_tv = calc_rs_tv_normalized(prices, nifty_prices)
        elif len(nifty_prices) >= 60:
            rs_tv = calc_rs_tv_normalized(prices, nifty_prices)
        # else: nifty_cache is empty - RS-TV stays None
        # Showing wrong RS would mislead trading decisions

        # Index-relative RS — rank within each index peer group only
        my_raw = my_raw_val
        # Index-relative RS using actual index price history (Pine Script formula)
        # RS-MID = how stock performs vs Midcap150 index (not vs constituent stocks)
        # RS-SML = how stock performs vs Smallcap250 index
        # This is correct: same formula as RS-TV but benchmark changes
        mid_prices = index_history_cache.get('Midcap 150', {}).get('prices', [])
        sml_prices = index_history_cache.get('Smallcap 250', {}).get('prices', [])
        n50_prices = nifty_cache.get('prices', [])

        rs_nifty50  = calc_rs_tv_normalized(prices, n50_prices)  if len(n50_prices)  >= 60 else None
        rs_midcap   = calc_rs_tv_normalized(prices, mid_prices)  if len(mid_prices)  >= 60 else None
        rs_smallcap = calc_rs_tv_normalized(prices, sml_prices)  if len(sml_prices)  >= 60 else None
        rs_microcap = percentile_rank(microcap_raws, my_raw) if my_raw is not None and sym in MICROCAP and len(microcap_raws) >= 5 else None

        # Sector-relative RS — rank within stock's own sector only
        my_sector = sym_to_sector.get(sym, 'Other')
        sec_pool  = sector_raws.get(my_sector, [])
        rs_sector = percentile_rank(sec_pool, my_raw) if my_raw is not None and len(sec_pool) >= 5 else None

        # RVOL — relative volume
        rvol_data = calc_rvol(volumes)

        # RS Line vs Nifty
        rs_line_data = calc_rs_line(prices, nifty_cache.get('prices', []))

        # Stage 2 New Entry
        is_s2_new = calc_stage2_new_entry(prices)

        # Live price — use sym-based lookup
        # IMPORTANT: historical_cache prices are NEVER mutated. The last element of
        # `prices` is the most recent COMPLETED daily close (yesterday's close during
        # market hours, or today's close after EOD batch). We use that as the
        # baseline "prev" and overlay live_data's last_price as "today" ONLY for
        # display (last/chg) — RS/PP/etc continue to use the immutable closes.
        live = live_data.get(sym, {})
        true_prev_close = prices[n-1]               # most recent completed close
        live_price = live.get('last_price', 0)

        if live_price and live_price > 0:
            last = live_price
            prev = true_prev_close
        else:
            # No live data (market closed / fetch failed) — show last completed
            # close vs the one before it, exactly like EOD.
            last = prices[n-1]
            prev = prices[n-2] if n > 1 else last

        chg = round((last - prev) / prev * 100, 2) if prev else 0
        vol = live.get('volume') if live.get('volume') else (volumes[n-1] if volumes else 0)

        # PP
        pp = detect_pp(prices, volumes)

        # Volume signals — IBD style
        # HY (High Year) = today volume ranks in top 5% of last 252 trading days
        # HT (High Time) = today volume ranks in top 5% of all available history
        yr_vols  = volumes[-252:] if len(volumes) >= 252 else volumes
        all_vols = volumes  # all available history

        # Percentile rank: what % of past volumes is today's volume greater than?
        def vol_pct_rank(today_v, hist_vols):
            if not hist_vols or today_v is None: return 0
            rank = sum(1 for v in hist_vols if today_v > v)
            return round(rank / len(hist_vols) * 100, 1)

        hy_pct = vol_pct_rank(vol, yr_vols)   # percentile rank in last 1 year
        ht_pct = vol_pct_rank(vol, all_vols)  # percentile rank in all history

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

        # Squeeze (BB + Keltner) and VCP
        highs_arr = s.get('highs', prices)
        lows_arr  = s.get('lows', prices)
        # ── Multi-timeframe TTM Squeeze ──────────────────────────
        squeeze_daily  = calc_ttm_squeeze(prices, highs_arr, lows_arr)
        _w = weekly_cache.get(sym, {})
        squeeze_weekly = calc_ttm_squeeze(_w.get('prices',[]),_w.get('highs',[]),_w.get('lows',[])) if _w else {'in_squeeze':False,'squeeze_fired':False,'momentum':0,'momentum_dir':'flat','squeeze_days':0,'strength_score':0,'fired_bullish':False,'fired_bearish':False,'bb_width_pct':None,'dots':[],'hist':[]}
        _h = hourly_cache.get(sym, {})
        squeeze_hourly = calc_ttm_squeeze(_h.get('prices',[]),_h.get('highs',[]),_h.get('lows',[])) if _h else {'in_squeeze':False,'squeeze_fired':False,'momentum':0,'momentum_dir':'flat','squeeze_days':0,'strength_score':0,'fired_bullish':False,'fired_bearish':False,'bb_width_pct':None,'dots':[],'hist':[]}
        squeeze = squeeze_daily  # backward compat
        vcp     = detect_vcp(prices, volumes, highs_arr, lows_arr)

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
            'rs_tv':          rs_tv,
            'rvol':           rvol_data.get('rvol'),
            'vol_signal':     rvol_data.get('vol_signal'),
            'rs_line_new_high': rs_line_data.get('rs_line_new_high', False),
            'rs_line_trend':  rs_line_data.get('rs_line_trend', 'flat'),
            'rs_line_value':  rs_line_data.get('rs_line_value'),
            'is_s2_new_entry': is_s2_new,       # TradingView / Lakshmi Mata Pine Script RS
            'rs_nifty50':     rs_nifty50,
            'rs_midcap':      rs_midcap,
            'rs_smallcap':    rs_smallcap,
            'rs_microcap':    rs_microcap,
            'rs_sector':      rs_sector,
            'rs_raw':         round(my_raw_val, 6) if my_raw_val is not None else None,
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
            'in_squeeze':        squeeze_daily['in_squeeze'],
            'squeeze_fired':     squeeze_daily['squeeze_fired'],
            'bb_width_pct':      squeeze_daily.get('bb_width_pct'),
            'squeeze_days':      squeeze_daily.get('squeeze_days',0),
            'sq_momentum':       squeeze_daily.get('momentum',0),
            'sq_momentum_dir':   squeeze_daily.get('momentum_dir','flat'),
            'sq_strength':       squeeze_daily.get('strength_score',0),
            'sq_fired_bullish':  squeeze_daily.get('fired_bullish',False),
            'sq_fired_bearish':  squeeze_daily.get('fired_bearish',False),
            'sq_dots_d':         json.dumps(squeeze_daily.get('dots',[])),
            'sq_hist_d':         json.dumps(squeeze_daily.get('hist',[])),
            'sq_weekly_in':      squeeze_weekly['in_squeeze'],
            'sq_weekly_fired':   squeeze_weekly['squeeze_fired'],
            'sq_weekly_days':    squeeze_weekly.get('squeeze_days',0),
            'sq_weekly_mom':     squeeze_weekly.get('momentum',0),
            'sq_weekly_mom_dir': squeeze_weekly.get('momentum_dir','flat'),
            'sq_weekly_bullish': squeeze_weekly.get('fired_bullish',False),
            'sq_hourly_in':      squeeze_hourly['in_squeeze'],
            'sq_hourly_fired':   squeeze_hourly['squeeze_fired'],
            'sq_hourly_days':    squeeze_hourly.get('squeeze_days',0),
            'sq_hourly_mom':     squeeze_hourly.get('momentum',0),
            'sq_hourly_mom_dir': squeeze_hourly.get('momentum_dir','flat'),
            'sq_hourly_bullish': squeeze_hourly.get('fired_bullish',False),
            'is_vcp':         vcp['is_vcp'],
            'vcp_stage':      vcp['vcp_stage'],
            'vcp_fired':      vcp['vcp_fired'],
            'vcp_contractions': json.dumps(vcp['contractions']),
            'sector':         get_sector(sym),
            'in_nifty50':     sym in NIFTY50,
            'in_midcap':      sym in MIDCAP,
            'in_smallcap':    sym in SMALLCAP,
            'in_microcap':    sym in MICROCAP,
            # Fundamentals from Screener.in (cached, refreshed every 6h)
            'market_cap':     fundamentals_cache.get(sym, {}).get('market_cap'),
            'pe':             fundamentals_cache.get(sym, {}).get('pe'),
            'roe':            fundamentals_cache.get(sym, {}).get('roe'),
            'eps':            fundamentals_cache.get(sym, {}).get('eps'),
            'debt_eq':        fundamentals_cache.get(sym, {}).get('debt_eq'),
            'promoter':       fundamentals_cache.get(sym, {}).get('promoter'),
            'last_updated':   now_ist.isoformat(),
            'scan_type':      scan_type,
        })

    # Step 5.4: Detect NEW squeeze/VCP fires (state change from last scan)
    global prev_squeeze_state
    new_fires = []
    for s in processed:
        sym = s['sym']
        bb_fired  = s.get('squeeze_fired', False)
        vcp_fired = s.get('vcp_fired', False)
        prev = prev_squeeze_state.get(sym, {'bb': False, 'vcp': False})

        new_bb  = bb_fired  and not prev['bb']
        new_vcp = vcp_fired and not prev['vcp']

        if new_bb or new_vcp:
            fire_type = []
            if new_bb:  fire_type.append('BB Squeeze')
            if new_vcp: fire_type.append('VCP')
            new_fires.append({
                'sym':        sym,
                'fire_type':  ', '.join(fire_type),
                'rs_tv':      s.get('rs_tv'),
                'rs':         s.get('rs'),
                'last_price': s.get('last_price'),
                'chg_pct':    s.get('chg_pct'),
                'sector':     s.get('sector'),
                'fired_at':   now_ist.isoformat(),
            })

    # Update state for next scan
    prev_squeeze_state = {
        s['sym']: {
            'bb':  s.get('squeeze_fired', False),
            'vcp': s.get('vcp_fired', False),
        }
        for s in processed
    }

    if new_fires:
        log.info(f"  🔥 {len(new_fires)} NEW squeeze fires: {[f['sym'] for f in new_fires]}")
        # Save to Supabase so frontend can poll and show notifications
        await supabase_upsert(session, 'squeeze_alerts', new_fires, on_conflict='sym,fired_at')

    # Step 5.5: Market Breadth metrics
    # These give a pulse on overall market health
    total = len(processed)
    if total > 0:
        breadth = {
            'total_stocks':       total,
            'above_ma10':         sum(1 for s in processed if s.get('ma10') and s.get('last_price',0) > s.get('ma10',0)),
            'above_ma50':         sum(1 for s in processed if s.get('ma50') and s.get('last_price',0) > s.get('ma50',0)),
            'rs_above_70':        sum(1 for s in processed if (s.get('rs_tv') or s.get('rs',0)) >= 70),
            'rs_above_50':        sum(1 for s in processed if (s.get('rs_tv') or s.get('rs',0)) >= 50),
            'rs_improving':       sum(1 for s in processed if s.get('rs_trend') == 'improving'),
            'rs_declining':       sum(1 for s in processed if s.get('rs_trend') == 'declining'),
            'stage2_count':       sum(1 for s in processed if s.get('weinstein_stage') == 2),
            'stage4_count':       sum(1 for s in processed if s.get('weinstein_stage') == 4),
            'new_52w_high':       sum(1 for s in processed if s.get('pct_from_52wh', -100) >= -2),
            'new_52w_low':        sum(1 for s in processed if s.get('pct_from_52wl', 100) <= 2),
            'pp_count':           sum(1 for s in processed if s.get('is_pp')),
            'rvol_surge':         sum(1 for s in processed if s.get('vol_signal') == 'surge'),
            's2_new_entry':       sum(1 for s in processed if s.get('is_s2_new_entry')),
            'rs_line_new_high':   sum(1 for s in processed if s.get('rs_line_new_high')),
            'advances':           sum(1 for s in processed if s.get('chg_pct', 0) > 0),
            'declines':           sum(1 for s in processed if s.get('chg_pct', 0) < 0),
            'last_updated':       now_ist.isoformat(),
            'scan_date':          now_ist.strftime('%Y-%m-%d'),
        }
        await supabase_upsert(session, 'market_breadth', [breadth], on_conflict='scan_date')
        log.info(f"  📈 Market breadth saved: {breadth['advances']}↑ {breadth['declines']}↓ Stage2:{breadth['stage2_count']}")

    # Step 6: Build sector RS
    sector_rows = build_sector_rs(processed, SECTOR_MAP)

    # Step 6.5: Build index dashboard data
    # For each tracked index: live price + daily/weekly/monthly chg + RS-TV + Stage
    index_rows = []
    nifty_prices = nifty_cache.get('prices', [])

    for idx_name, idx_data in index_history_cache.items():
        prices  = idx_data['prices']
        n       = len(prices)
        if n < 5:
            continue

        last    = prices[-1]
        prev    = prices[-2]  if n >= 2   else last
        week    = prices[-6]  if n >= 6   else prices[0]
        month   = prices[-22] if n >= 22  else prices[0]
        qtr     = prices[-66] if n >= 66  else prices[0]
        yr      = prices[-252] if n >= 252 else prices[0]

        chg_d = round((last - prev) / prev * 100, 2) if prev else 0
        chg_w = round((last - week) / week * 100, 2) if week else 0
        chg_m = round((last - month) / month * 100, 2) if month else 0
        chg_q = round((last - qtr)  / qtr  * 100, 2) if qtr  else 0
        chg_y = round((last - yr)   / yr   * 100, 2) if yr   else 0

        # RS-TV using Nifty as benchmark (skip for Nifty itself)
        if idx_name == 'Nifty 50':
            rs_tv_idx = 50  # Nifty vs itself is always median
        elif nifty_prices and len(nifty_prices) >= 252:
            rs_tv_idx = calc_rs_tv_normalized(prices, nifty_prices)
        else:
            rs_tv_idx = None

        # Weinstein Stage for the index
        highs = idx_data.get('highs', prices)
        lows  = idx_data.get('lows', prices)
        ma30  = sma(prices, min(30, n))
        ma10  = sma(prices, min(10, n))
        h52   = max(prices[-252:]) if n >= 252 else max(prices)
        l52   = min(prices[-252:]) if n >= 252 else min(prices)
        pct_from_high = round((last - h52) / h52 * 100, 1) if h52 else 0

        # Stage logic for index
        if ma30 and last > ma30 and chg_d >= 0:
            if pct_from_high >= -5:
                stage = 3
            else:
                stage = 2
        elif ma30 and last < ma30 and chg_d <= 0:
            stage = 4
        else:
            stage = 1

        stage_labels = {1:'S1 Base', 2:'S2 Up', 3:'S3 Top', 4:'S4 Down'}
        stage_label  = stage_labels.get(stage, 'S1 Base')

        # Above/below key MAs
        above_ma10 = last > ma10 if ma10 else None
        above_ma30 = last > ma30 if ma30 else None

        # Top 3 stocks in this index from our scan (only for constituent indices)
        top_stocks = []
        constituent_map = {
            'Nifty 50':    [s for s in processed if s.get('in_nifty50')],
            'Midcap 150':  [s for s in processed if s.get('in_midcap')],
            'Smallcap 250':[s for s in processed if s.get('in_smallcap')],
            'Microcap 250':[s for s in processed if s.get('in_microcap')],
        }
        if idx_name in constituent_map:
            members = constituent_map[idx_name]
            top3    = sorted(members, key=lambda x: x.get('rs_tv') or x.get('rs') or 0, reverse=True)[:3]
            bot3    = sorted(members, key=lambda x: x.get('rs_tv') or x.get('rs') or 0)[:3]
            top_stocks = [{'sym':s['sym'],'rs':s.get('rs_tv') or s.get('rs')} for s in top3]
            bot_stocks = [{'sym':s['sym'],'rs':s.get('rs_tv') or s.get('rs')} for s in bot3]
        else:
            bot_stocks = []

        index_rows.append({
            'name':          idx_name,
            'last_price':    round(last, 2),
            'chg_d':         chg_d,
            'chg_w':         chg_w,
            'chg_m':         chg_m,
            'chg_q':         chg_q,
            'chg_y':         chg_y,
            'rs_tv':         rs_tv_idx,
            'stage':         stage,
            'stage_label':   stage_label,
            'above_ma10':    above_ma10,
            'above_ma30':    above_ma30,
            'high_52w':      round(h52, 2),
            'low_52w':       round(l52, 2),
            'pct_from_high': pct_from_high,
            'top_stocks':    json.dumps(top_stocks),
            'bot_stocks':    json.dumps(bot_stocks),
            'last_updated':  now_ist.isoformat(),
        })

    if index_rows:
        await supabase_upsert(session, 'index_dashboard', index_rows, on_conflict='name')
        log.info(f"  📊 Index dashboard: {len(index_rows)} indices saved")

    # Step 7: Save to Supabase
    log.info(f"  Saving {len(processed)} stocks to Supabase…")
    await supabase_upsert(session, 'stocks', processed)
    await supabase_upsert(session, 'sectors', [
        {**s, 'last_updated': now_ist.isoformat(), 'top_stocks': json.dumps(s['top_stocks'])}
        for s in sector_rows
    ])

    # Step 7.5: At end-of-day, also archive a permanent daily snapshot.
    # This is what powers the "view any past date" history feature —
    # without this, only today's live state is ever available.
    if scan_type == 'batch_eod':
        snapshot_date = now_ist.strftime('%Y-%m-%d')
        log.info(f"  📸 Archiving EOD snapshot for {snapshot_date}…")

        history_rows = []
        for p in processed:
            row = {k: v for k, v in p.items() if k not in ('last_updated', 'scan_type')}
            row['snapshot_date'] = snapshot_date
            history_rows.append(row)
        await supabase_upsert(session, 'stock_history', history_rows, on_conflict='snapshot_date,sym')

        sector_history_rows = [
            {
                'snapshot_date': snapshot_date,
                'sector':        s['sector'],
                'avg_rs':        s['avg_rs'],
                'rank':          s['rank'],
                'count':         s['count'],
                'pp_count':      s['pp_count'],
                'improving':     s['improving'],
                'top_stocks':    json.dumps(s['top_stocks']),
            }
            for s in sector_rows
        ]
        await supabase_upsert(session, 'sector_history', sector_history_rows, on_conflict='snapshot_date,sector')
        log.info(f"  ✅ Snapshot archived: {len(history_rows)} stocks for {snapshot_date}")
        # Send daily Telegram digest at EOD
        if breadth:
            await send_daily_digest(session, processed, breadth)

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
    global ALL_STOCKS, NIFTY50, MIDCAP, SMALLCAP, MICROCAP
    global nifty_cache, index_history_cache, historical_cache
    global instrument_key_map, weekly_cache, hourly_cache

    log.info("=" * 60)
    log.info("  PocketRS Pro — Live Update Server")
    log.info(f"  Update interval: {UPDATE_INTERVAL} seconds")
    log.info(f"  Market hours: {MARKET_OPEN_H}:{MARKET_OPEN_M:02d} - {MARKET_CLOSE_H}:{MARKET_CLOSE_M:02d} IST")
    log.info("=" * 60)

    connector = aiohttp.TCPConnector(limit=50, ssl=False)  # higher limit for parallel OHLC fetches
    async with aiohttp.ClientSession(connector=connector) as session:

        # Step 0: Fetch real official Nifty index constituent lists
        # (replaces the small hardcoded MIDCAP/SMALLCAP/MICROCAP samples
        #  with the actual current 150/250/250 stock lists)
        await load_official_index_lists(session)

        # Step 1: Fetch ALL NSE instruments from Upstox
        log.info("Fetching all NSE instruments from Upstox…")
        try:
            url = "https://api.upstox.com/v2/instruments"
            headers = {
                "Authorization": f"Bearer {ANALYTICS_TOKEN}",
                "Accept": "application/json"
            }
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    data = await r.json()
                    instruments = data.get('data', [])
                    # Filter NSE equity stocks only
                    nse_eq = [
                        i['trading_symbol'] for i in instruments
                        if i.get('exchange') == 'NSE'
                        and i.get('instrument_type') == 'EQ'
                        and i.get('trading_symbol')
                        and '-' not in i.get('trading_symbol','')[-3:]
                    ]
                    # Remove duplicates and sort
                    nse_eq = list(dict.fromkeys(nse_eq))
                    log.info(f"✅ Fetched {len(nse_eq)} NSE equity stocks from Upstox")
                    if len(nse_eq) > 100:
                        ALL_STOCKS = nse_eq
                    else:
                        log.warning("Too few instruments fetched — using hardcoded list")
                else:
                    log.warning(f"Instrument fetch failed: {r.status} — using hardcoded list")
        except Exception as e:
            log.warning(f"Could not fetch instruments: {e} — using hardcoded list")

        log.info(f"📊 Total stocks to scan: {len(ALL_STOCKS)}")

        # Step 2: Load instrument master to get correct API keys
        await load_instrument_master(session)

        # Step 2b: Load Nifty 50 + all index histories for TV RS calc and index dashboard
        await load_nifty_cache(session)
        await load_index_cache(session)

        # Step 3: Load historical data cache at startup
        log.info("Loading historical data cache at startup…")
        await load_historical_cache(session)
        # Fallback: if Nifty cache empty (Upstox rejected outside hours),
        # build it from RELIANCE/HDFCBANK price history as Nifty proxy
        # OR better: use the Nifty index historical candle with a different key
        if not nifty_cache.get('prices'):
            log.warning("⚠️ Nifty cache empty — retrying with alternate key...")
            # Try alternate Nifty key format
            for nifty_key in ["NSE_INDEX|Nifty 50", "NSE_INDEX|NIFTY 50", "NSE_EQ|NIFTY"]:
                encoded = nifty_key.replace('|','%7C').replace(' ','%20')
                url = f"https://api.upstox.com/v2/historical-candle/{encoded}/day/{datetime.now(IST).strftime('%Y-%m-%d')}/{(datetime.now(IST)-timedelta(days=420)).strftime('%Y-%m-%d')}"
                try:
                    async with session.get(url, headers={"Authorization":f"Bearer {ANALYTICS_TOKEN}","Accept":"application/json"},
                                          timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status == 200:
                            data = await r.json()
                            candles = list(reversed(data.get('data',{}).get('candles',[])))
                            if candles:
                                nifty_cache.clear()
                                nifty_cache.update({'prices':[c[4] for c in candles],'volumes':[c[5] for c in candles]})
                                log.info(f"✅ Nifty loaded with key {nifty_key}: {len(nifty_cache['prices'])} days")
                                break
                except Exception as e:
                    log.warning(f"Nifty retry {nifty_key} failed: {e}")

        if not nifty_cache.get('prices'):
            log.error("❌ Nifty cache still empty after retries — RS-TV will be None for all stocks")
        else:
            log.info(f"✅ Nifty cache ready: {len(nifty_cache['prices'])} days — RS-TV will be calculated")

        log.info("✅ Proceeding to initial scan…")

        # Step 4: Run initial scan (hard timeout so a stall can't hang the process forever)
        # Detect the correct scan type based on actual time, rather than always
        # forcing 'batch_morning' — if Railway restarts mid-afternoon or after
        # close, the first scan should reflect that correctly.
        SCAN_TIMEOUT = 600  # 10 minutes max for a single scan cycle
        ist_now_initial = datetime.now(IST)
        # Always run batch_morning first — calculates RS-TV from historical data
        # This ensures RS-TV is populated immediately even outside market hours
        # If market is open, we'll also get live prices in this scan
        initial_scan_type = 'batch_morning'
        log.info(f"Starting with batch_morning scan to populate RS-TV from history...")
        log.info(f"Initial scan type detected: {initial_scan_type} (current time {ist_now_initial.strftime('%H:%M IST')})")

        try:
            await asyncio.wait_for(run_scan(session, initial_scan_type), timeout=SCAN_TIMEOUT)
        except asyncio.TimeoutError:
            log.error(f"⏱ Initial scan exceeded {SCAN_TIMEOUT}s timeout — aborting and continuing to main loop")
        except Exception as e:
            log.error(f"❌ Initial scan failed: {e}")

        last_scan = time.time()
        scan_count = 0

        while True:
            try:
                now = time.time()
                elapsed = now - last_scan

                if elapsed >= UPDATE_INTERVAL:
                    if is_scan_time():
                        scan_type = 'live' if is_market_open() else 'batch_eod'
                        try:
                            await asyncio.wait_for(run_scan(session, scan_type), timeout=SCAN_TIMEOUT)
                            scan_count += 1
                        except asyncio.TimeoutError:
                            log.error(f"⏱ Scan exceeded {SCAN_TIMEOUT}s timeout — skipping this cycle")
                        last_scan = time.time()
                    else:
                        # Market closed — still run scan using historical data
                        # This ensures RS-TV is always calculated and saved
                        ist_now = datetime.now(IST)
                        log.info(f"📊 Market closed ({ist_now.strftime('%H:%M IST')}) — running historical scan for RS-TV...")
                        try:
                            await asyncio.wait_for(run_scan(session, 'batch_morning'), timeout=SCAN_TIMEOUT)
                            log.info("✅ Historical scan done — RS-TV updated")
                            # After hours: scan every 30 mins not every minute
                            await asyncio.sleep(1800)
                        except Exception as e:
                            log.error(f"Historical scan failed: {e}")
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
