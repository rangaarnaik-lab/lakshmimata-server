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
import random
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
    # Right-align: both series end on "today" but are usually different
    # lengths (stock's own history vs a much longer benchmark cache), so
    # trim from the front to match up the same calendar period — see the
    # same fix/comment in calc_raw_rs_series.
    n = min(len(prices), len(bench_prices))
    prices = prices[-n:]
    bench_prices = bench_prices[-n:]
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

def detect_bb_squeeze(prices: list, highs: list, lows: list, n: int = 20) -> dict:
    """
    Bollinger Band Squeeze: BB width at multi-month low, BB inside Keltner Channel.
    Classic TTM Squeeze indicator logic.
    """
    empty = {'in_squeeze': False, 'squeeze_fired': False, 'bb_width_pct': None, 'squeeze_days': 0}
    if len(prices) < n + 60:
        return empty

    closes = prices
    ma20 = sma(closes, n)
    sd20 = std_dev(closes, n)
    if not ma20 or not sd20:
        return empty

    upper_bb = ma20 + 2 * sd20
    lower_bb = ma20 - 2 * sd20
    bb_width = upper_bb - lower_bb
    bb_width_pct = round((bb_width / ma20) * 100, 2) if ma20 else None

    # Keltner Channel using ATR
    atr_val = atr(highs, lows, closes, n)
    if not atr_val:
        return empty
    upper_kc = ma20 + 1.5 * atr_val
    lower_kc = ma20 - 1.5 * atr_val

    # Squeeze ON when BB is inside KC
    in_squeeze = (upper_bb < upper_kc) and (lower_bb > lower_kc)

    # Check how many consecutive days squeeze has been on.
    # PERFORMANCE: avoid re-slicing/re-scanning the full price history on every
    # iteration (was O(n) work x 20 iterations x 3 functions x 2400 stocks,
    # which froze the event loop for minutes). Instead, precompute a short
    # window of closes/highs/lows once and reuse it.
    squeeze_days = 0
    max_lookback = min(20, len(closes) - n - 20)
    if max_lookback > 0:
        # Only need the last (n + max_lookback + 20) closes for this whole check
        window_size = n + max_lookback + 21
        wc = closes[-window_size:] if len(closes) > window_size else closes
        wh = highs[-window_size:]  if len(highs)  > window_size else highs
        wl = lows[-window_size:]   if len(lows)   > window_size else lows

        for d in range(0, max_lookback):
            end = len(wc) - 1 - d
            if end < n + 20:
                break
            sub_closes = wc[:end+1]
            sub_highs  = wh[:end+1]
            sub_lows   = wl[:end+1]
            m = sma(sub_closes, n)
            s = std_dev(sub_closes, n)
            a = atr(sub_highs, sub_lows, sub_closes, n)
            if not m or not s or not a:
                break
            ub, lb = m + 2*s, m - 2*s
            uk, lk = m + 1.5*a, m - 1.5*a
            if ub < uk and lb > lk:
                squeeze_days += 1
            else:
                break

    # Squeeze fired = was in squeeze yesterday, not in squeeze today (breakout)
    squeeze_fired = squeeze_days == 0 and was_in_squeeze_yesterday(closes, highs, lows, n)

    return {
        'in_squeeze': in_squeeze,
        'squeeze_fired': squeeze_fired,
        'bb_width_pct': bb_width_pct,
        'squeeze_days': squeeze_days,
    }

def was_in_squeeze_yesterday(closes, highs, lows, n=20) -> bool:
    if len(closes) < n + 21:
        return False
    sub_closes = closes[:-1]
    sub_highs  = highs[:-1]
    sub_lows   = lows[:-1]
    m = sma(sub_closes, n)
    s = std_dev(sub_closes, n)
    a = atr(sub_highs, sub_lows, sub_closes, n)
    if not m or not s or not a:
        return False
    ub, lb = m + 2*s, m - 2*s
    uk, lk = m + 1.5*a, m - 1.5*a
    return ub < uk and lb > lk

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

def calc_raw_rs_series(prices: list, bench_prices: list) -> list:
    """
    Compute full rawRS series for a stock vs benchmark in ONE pass.
    Returns list of rawRS values (one per day, None where not computable).
    This is called ONCE per stock — result is cached and used for normalization.

    CRITICAL: prices and bench_prices are usually different lengths (e.g. a
    stock's own ~500-day Yahoo history vs a ~1738-day Nifty cache seeded
    with years of extra history). Both arrays end on the same "today", so
    they must be RIGHT-aligned (trimmed from the front) before comparing by
    index — left-aligning (as a naive `arr[i]` for i in range(min(len...)))
    would compare the stock's recent prices against the benchmark's oldest
    prices, from a completely different calendar period.
    """
    n = min(len(prices), len(bench_prices))
    prices = prices[-n:]
    bench_prices = bench_prices[-n:]
    result = []
    for i in range(n):
        if i < 252:
            result.append(None)
            continue
        def pct(arr, length):
            prev = arr[i - length]
            return (arr[i] - prev) / prev * 100 if prev else None
        r3  = pct(prices, 63);  br3  = pct(bench_prices, 63)
        r6  = pct(prices, 126); br6  = pct(bench_prices, 126)
        r9  = pct(prices, 189); br9  = pct(bench_prices, 189)
        r12 = pct(prices, 252); br12 = pct(bench_prices, 252)
        if None in (r3,br3,r6,br6,r9,br9,r12,br12):
            result.append(None)
        else:
            result.append((r3-br3)*0.4 + (r6-br6)*0.2 + (r9-br9)*0.2 + (r12-br12)*0.2)
    return result


def calc_live_raw_rs_today(prices: list, bench_prices: list,
                            live_price: float, live_bench_price: float) -> Optional[float]:
    """
    Compute TODAY's raw RS using live price/benchmark instead of waiting
    for historical_cache to be refreshed at EOD. During live market hours,
    historical_cache's last element is still YESTERDAY's close — this
    function treats live_price/live_bench_price as an implicit "today"
    point one step past the end of the arrays, using the same 63/126/189/
    252-day-back weighting as calc_raw_rs_series. The lookback anchors
    (prices[-63] etc) are measured from yesterday rather than today, which
    is off by one trading day out of a 63-252 day window — negligible.
    """
    if live_price is None or live_bench_price is None:
        return None
    n = min(len(prices), len(bench_prices))
    if n < 252:
        return None
    prices = prices[-n:]
    bench_prices = bench_prices[-n:]

    def pct(last_val, arr, length):
        prev = arr[-length]
        return (last_val - prev) / prev * 100 if prev else None

    r3,  br3  = pct(live_price, prices, 63),  pct(live_bench_price, bench_prices, 63)
    r6,  br6  = pct(live_price, prices, 126), pct(live_bench_price, bench_prices, 126)
    r9,  br9  = pct(live_price, prices, 189), pct(live_bench_price, bench_prices, 189)
    r12, br12 = pct(live_price, prices, 252), pct(live_bench_price, bench_prices, 252)
    if None in (r3, br3, r6, br6, r9, br9, r12, br12):
        return None
    return (r3-br3)*0.4 + (r6-br6)*0.2 + (r9-br9)*0.2 + (r12-br12)*0.2


def normalize_rs(raw_series: list) -> Optional[int]:
    """
    Self-normalized RS matching Pine Script exactly.
    With stooq providing 500+ days, we have 250 valid rawRS points for min/max window.
    """
    if not raw_series:
        return None
    valid = [v for v in raw_series if v is not None]
    if len(valid) < 2:
        return None
    current = raw_series[-1]
    if current is None:
        return None
    # Use last 252 valid points for normalization window (Pine Script: ta.highest/lowest 252)
    window = [v for v in raw_series[-300:] if v is not None][-252:]
    hi = max(window)
    lo = min(window)
    if hi == lo:
        return 50
    return max(1, min(99, round(((current - lo) / (hi - lo)) * 98 + 1)))


def calc_rs_tv_normalized(prices: list, bench_prices: list, end_idx: int = None) -> Optional[int]:
    """Convenience wrapper — computes full series then normalizes."""
    raw = calc_raw_rs_series(prices, bench_prices)
    if end_idx is not None:
        raw = raw[:end_idx+1]
    return normalize_rs(raw)


def tv_history_from_raw(raw_series: list, days: int = 15) -> list:
    """
    Build a day-by-day TV-style (self-normalized) RS history from an
    already-computed raw RS series, so the sparkline/trend uses the SAME
    methodology as the main RS-TV score — instead of a completely different
    percentile-rank scale that doesn't match the Pine Script numbers.
    """
    n = len(raw_series)
    hist = []
    for d in range(days - 1, -1, -1):
        end_idx = n - 1 - d
        hist.append(normalize_rs(raw_series[:end_idx + 1]) if end_idx >= 0 else None)
    return hist

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
    history = {s['sym']: [] for s in all_stocks}
    for d in range(days-1, -1, -1):
        raw_map = {}
        for s in valid:
            try:
                # Right-align per stock: "d days ago" must be measured from
                # THIS stock's own most recent day, not from some other
                # stock's array length. Stocks can have slightly different
                # total history lengths (Yahoo fetch failures, fallback to
                # shorter Upstox data, etc.) even though they all end on the
                # same "today" — using one shared end_idx for every stock
                # silently compared different calendar dates across stocks,
                # corrupting the whole day's percentile-rank pool.
                end_idx = len(s['prices']) - 1 - d
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

        # Breadth — % of this sector's stocks advancing at each timeframe.
        # Matches the "Segment Advances %" concept: not just "is the
        # sector index up", but "how broad is the move across its members"
        # (a sector up 2% on one large-cap carrying it looks very
        # different from one up 2% with 80% of members participating).
        def advance_pct(field):
            vals = [s.get(field) for s in members if s.get(field) is not None]
            if not vals:
                return None
            return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2)

        sectors.append({
            'sector':   sector,
            'avg_rs':   avg_rs,
            'count':    len(members),
            'pp_count': pp_count,
            'improving':improving,
            'advances_d': advance_pct('chg_pct'),
            'advances_w': advance_pct('chg_w_pct'),
            'advances_m': advance_pct('chg_m_pct'),
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
    Replace hardcoded NIFTY50/MIDCAP/SMALLCAP/MICROCAP with the real,
    current official lists from niftyindices.com. Falls back silently
    to the existing hardcoded lists on any failure.
    """
    global NIFTY50, MIDCAP, SMALLCAP, MICROCAP, ALL_STOCKS

    log.info("Fetching official NSE index constituent lists…")
    results = await asyncio.gather(
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['NIFTY50']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['MIDCAP150']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['SMALLCAP250']),
        fetch_index_csv(session, NIFTY_INDEX_CSV_URLS['MICROCAP250']),
        return_exceptions=True,
    )
    fresh_nifty50, fresh_midcap, fresh_smallcap, fresh_microcap = [
        r if isinstance(r, list) else [] for r in results
    ]

    if len(fresh_nifty50) >= 40:
        NIFTY50 = fresh_nifty50
        log.info(f"✅ Nifty 50: {len(NIFTY50)} stocks (official)")
    else:
        log.warning(f"⚠️ Nifty 50 fetch returned {len(fresh_nifty50)} — keeping {len(NIFTY50)} hardcoded fallback")

    if len(fresh_midcap) >= 100:
        MIDCAP = fresh_midcap
        log.info(f"✅ Midcap 150: {len(MIDCAP)} stocks (official)")
    else:
        log.warning(f"⚠️ Midcap fetch returned {len(fresh_midcap)} — keeping {len(MIDCAP)} hardcoded fallback")

    if len(fresh_smallcap) >= 150:
        SMALLCAP = fresh_smallcap
        log.info(f"✅ Smallcap 250: {len(SMALLCAP)} stocks (official)")
    else:
        log.warning(f"⚠️ Smallcap fetch returned {len(fresh_smallcap)} — keeping {len(SMALLCAP)} hardcoded fallback")

    if len(fresh_microcap) >= 150:
        MICROCAP = fresh_microcap
        log.info(f"✅ Microcap 250: {len(MICROCAP)} stocks (official)")
    else:
        log.warning(f"⚠️ Microcap fetch returned {len(fresh_microcap)} — keeping {len(MICROCAP)} hardcoded fallback")

    # Rebuild ALL_STOCKS to include any official-list stocks not already covered
    # (ALL_STOCKS itself is later overwritten by the Upstox instrument master in
    # main(), so this just ensures the index membership flags stay consistent)
    
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
        'promoter_trend': None, 'peg_ratio': None,
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

    return result if got_any else None


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
        'promoter_trend': None, 'peg_ratio': None,
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

# Cache fundamentals to avoid re-fetching every minute
fundamentals_cache: dict = {}  # sym -> {market_cap, pe, roe, eps, debt_eq, promoter, fetched_at}
FUNDAMENTALS_TTL = 7 * 24 * 3600  # refresh weekly (data changes quarterly)
_fundamentals_debug_count = 0  # caps detailed per-request diagnostic logging
_upstox_fundamentals_debug_count = 0  # caps raw-response logging for the new Upstox fundamentals API
_upstox_shareholding_debug_count = 0  # separate budget so share-holdings isn't starved by key-ratios logging
_live_nifty_debug_count = 0  # caps raw-response logging for the live Nifty price fetch
_fetch_error_counts: dict = {}  # exception-type name -> count, reset per load_fundamentals_batch call,
# aggregated (not logged per-call) so a systemic failure shows up as one
# clear summary line instead of thousands of repeated log entries

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


async def load_fundamentals_from_supabase(session: aiohttp.ClientSession) -> list:
    """
    Load previously-fetched fundamentals straight from Supabase — zero
    Screener.in requests. Same optimization as load_all_history_from_supabase:
    without this, fundamentals_cache (pure in-memory) was wiped on every
    restart, forcing a full ~2385-stock re-scrape (at ~5 stocks/sec, that's
    8-15+ minutes) gated behind a once-per-day flag — so a restart mid-fetch
    meant most stocks never got fundamentals until the NEXT calendar day.
    Returns the list of symbols that are missing or past the TTL.
    """
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    all_rows: list = []
    PAGE = 1000
    offset = 0
    while True:
        try:
            page_headers = {**headers, "Range": f"{offset}-{offset + PAGE - 1}"}
            async with session.get(
                f"{SUPABASE_URL}/rest/v1/stock_fundamentals"
                f"?select=sym,market_cap,pe,roe,eps,debt_eq,promoter,"
                f"eps_qoq,eps_yoy,sales_qoq,sales_yoy,opm_pct,opm_trend,eps_growth_streak,"
                f"fii_pct,fii_trend,dii_pct,dii_trend,promoter_trend,peg_ratio,fetched_at",
                headers=page_headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status not in (200, 206):
                    log.warning(f"load_fundamentals_from_supabase page failed: status={r.status}")
                    break
                page = await r.json()
                all_rows.extend(page)
                if len(page) < PAGE:
                    break
                offset += PAGE
        except Exception as e:
            log.error(f"load_fundamentals_from_supabase error: {e}")
            break

    now = time.time()
    loaded = 0
    blank = 0
    stale_or_missing: list = []
    found_syms: set = set()
    DATA_FIELDS = ('market_cap', 'pe', 'roe', 'eps', 'debt_eq', 'promoter',
                   'eps_qoq', 'eps_yoy', 'sales_qoq', 'sales_yoy', 'opm_pct',
                   'opm_trend', 'eps_growth_streak', 'fii_pct', 'fii_trend',
                   'dii_pct', 'dii_trend', 'promoter_trend', 'peg_ratio')

    for row in all_rows:
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
        if is_blank or (now - fetched_at_ts) > FUNDAMENTALS_TTL:
            stale_or_missing.append(sym)

    missing_entirely = [s for s in ALL_STOCKS if s not in found_syms]
    stale_or_missing.extend(missing_entirely)

    log.info(f"📊 Loaded {loaded} stocks' fundamentals from Supabase (0 Screener.in requests) — "
             f"{blank} are blank (all fields None — scrape failed, will retry), "
             f"{len(stale_or_missing)} total need fetching (blank, missing, or "
             f">{FUNDAMENTALS_TTL//86400}d stale)")
    return stale_or_missing


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


async def load_fundamentals_at_startup(session: aiohttp.ClientSession):
    """
    Startup: load fundamentals from Supabase first (fast, free), then
    kick off a BACKGROUND task to scrape Screener.in only for symbols
    that are missing or stale — decoupled from the once-per-day EOD gate,
    so it can actually finish across restarts instead of always starting
    over from zero. Runs as a background task (not awaited) since a full
    scrape can take 8-15+ minutes and fundamentals aren't as time-critical
    as price data.
    """
    table_ready = await ensure_fundamentals_table(session)
    if not table_ready:
        log.error("⏭️  stock_fundamentals table unavailable — will rely on the once-daily "
                   "EOD Screener.in fetch only (no persistence across restarts until fixed).")
        return

    stale_or_missing = await load_fundamentals_from_supabase(session)
    if stale_or_missing:
        log.info(f"📊 Starting background fundamentals fetch for {len(stale_or_missing)} stocks…")
        asyncio.create_task(load_fundamentals_batch(session, stale_or_missing))

async def load_fundamentals_batch(session: aiohttp.ClientSession, symbols: list):
    """Fetch fundamentals for a batch of symbols, respecting TTL cache."""
    global _fetch_error_counts
    _fetch_error_counts = {}  # reset so this run's summary isn't polluted by a previous run's counts
    now = time.time()
    DATA_FIELDS = ('market_cap', 'pe', 'roe', 'eps', 'debt_eq', 'promoter',
                   'eps_qoq', 'eps_yoy', 'sales_qoq', 'sales_yoy', 'opm_pct',
                   'opm_trend', 'eps_growth_streak', 'fii_pct', 'fii_trend',
                   'dii_pct', 'dii_trend', 'promoter_trend', 'peg_ratio')
    def is_blank_cache(sym):
        c = fundamentals_cache.get(sym)
        return c is not None and all(c.get(f) is None for f in DATA_FIELDS)

    to_fetch = [
        sym for sym in symbols
        if sym not in fundamentals_cache
        or is_blank_cache(sym)  # scrape failed last time — don't wait out the TTL
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
            # Upstox succeeded — use it as-is. Note key-ratios doesn't
            # include Market Cap/EPS/Debt-Equity (confirmed against real
            # responses), so those stay None here. Falling back to
            # Screener.in for just those 3 fields would mean scraping
            # EVERY stock again (since they're always missing from
            # Upstox), defeating the point of moving off scraping — so
            # this is a deliberate trade-off, not an oversight. Getting
            # those 3 fields from a different Upstox endpoint (company
            # profile / balance sheet) is a reasonable follow-up.
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


async def fetch_bulk_ohlc(session: aiohttp.ClientSession, instrument_keys: list) -> dict:
    """
    Fetch live quotes for instruments in one call.
    IMPORTANT: uses Upstox's Full Market Quotes endpoint (/market-quote/quotes),
    NOT the OHLC endpoint (/market-quote/ohlc) — the OHLC endpoint's response
    shape is just {"ohlc": {...}, "last_price": ...} and has NO "volume" field
    at all. Every volume-dependent signal (HY/HT/rvol) was silently falling
    back to yesterday's completed volume the entire time, since live.get(
    'volume') was always None/missing from that endpoint — not a bug in the
    signal logic itself, just fetching from an endpoint that never had live
    volume to give. Full Market Quotes includes live_price, volume (live,
    updating all session), depth, etc.
    Keep batch small — GET URL length limits apply.
    """
    url = "https://api.upstox.com/v2/market-quote/quotes"
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    params = {
        "instrument_key": ",".join(instrument_keys),
    }
    try:
        async with session.get(url, headers=headers, params=params,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            text = await r.text()
            if r.status != 200:
                log.warning(f"Quotes fetch failed: {r.status} — {text[:300]}")
                return {}
            try:
                data = json.loads(text)
            except Exception:
                log.warning(f"Quotes response not JSON: {text[:200]}")
                return {}
            result = data.get('data', {})
            if not result:
                log.warning(f"Quotes empty data field. Full response keys: {list(data.keys())} status={data.get('status')}")
            return result
    except Exception as e:
        log.error(f"Quotes fetch error: {e}")
        return {}

async def fetch_historical(session: aiohttp.ClientSession, sym: str,
                           instrument_key: str = None) -> dict:
    """Fetch 15 months of daily historical data for one stock."""
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=550)).strftime('%Y-%m-%d')

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


historical_cache: dict = {}   # sym -> {prices, volumes, highs, lows}
history_dates_cache: dict = {}  # sym -> [dates] — parallel to historical_cache,
# tracked separately since RS calc doesn't need dates but incremental merges do
last_eod_refresh_date: Optional[str] = None  # IST date string — ensures the
# expensive EOD refresh (full Yahoo re-fetch + fundamentals) runs only ONCE
# per day, not on every single scan cycle while the market stays closed.
nifty_cache: dict = {}        # {'prices': [...]} — Nifty index daily closes for TV RS calc
midcap_cache: dict = {}       # {'prices': [...]} — synthetic Midcap 150 index
smallcap_cache: dict = {}     # {'prices': [...]} — synthetic Smallcap 250 index

def build_synthetic_index(symbols: list, cache: dict, min_stocks: int = 20) -> dict:
    """
    Build synthetic index from constituent stocks.
    Uses the LONGEST common window (most stocks have 285 days).
    Stocks with shorter history are excluded rather than truncating all.
    """
    # Get all series, find the most common length (mode)
    all_series = []
    for sym in symbols:
        data = cache.get(sym)
        if data and len(data.get('prices', [])) >= 252:
            all_series.append(data['prices'])

    if len(all_series) < min_stocks:
        return {}

    # Use the max length that at least 80% of stocks share
    lengths = sorted([len(s) for s in all_series], reverse=True)
    target_len = lengths[int(len(lengths) * 0.2)]  # 80th percentile length

    # Keep only stocks with enough history, truncate to target_len
    series = [s[-target_len:] for s in all_series if len(s) >= target_len]

    if len(series) < min_stocks:
        return {}

    prices = [
        sum(s[i] for s in series) / len(series)
        for i in range(target_len)
    ]
    return {'prices': prices}

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
}

# Cache for all index historical data
index_history_cache: dict = {}  # name -> {prices, volumes}

async def load_index_cache(session: aiohttp.ClientSession):
    """Fetch historical data for all tracked indices."""
    global index_history_cache
    log.info(f"Loading historical data for {len(INDEX_TRACKER)} indices…")
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=550)).strftime('%Y-%m-%d')
    headers = {
        "Authorization": f"Bearer {ANALYTICS_TOKEN}",
        "Accept": "application/json"
    }
    loaded = 0
    # Alternative key formats to try if primary fails
    KEY_ALTERNATIVES = {
        "Midcap 150":   ["NSE_INDEX|Nifty Midcap 150", "NSE_INDEX|NIFTY MIDCAP 150", "NSE_INDEX|Nifty MidCap 150"],
        "Smallcap 250": ["NSE_INDEX|Nifty Smallcap 250", "NSE_INDEX|NIFTY SMALLCAP 250", "NSE_INDEX|Nifty SmallCap 250"],
        "Microcap 250": ["NSE_INDEX|Nifty Microcap 250", "NSE_INDEX|NIFTY MICROCAP 250", "NSE_INDEX|Nifty MicroCap 250"],
        # Newer additions — exact Upstox naming for these is less certain
        # than the well-established ones above, so try a few common
        # variants each (same self-healing approach as Mid/Small/Microcap).
        "Defence":            ["NSE_INDEX|Nifty India Defence", "NSE_INDEX|Nifty Defence",
                                "NSE_INDEX|NIFTY INDIA DEFENCE"],
        "Financial Services": ["NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Financial Services"],
        "PSU Bank":           ["NSE_INDEX|Nifty PSU Bank"],
        "Private Bank":       ["NSE_INDEX|Nifty Pvt Bank", "NSE_INDEX|Nifty Private Bank"],
        "PSE":                ["NSE_INDEX|Nifty PSE"],
        "Media":              ["NSE_INDEX|Nifty Media"],
        "Infrastructure":     ["NSE_INDEX|Nifty Infra", "NSE_INDEX|Nifty Infrastructure"],
        "Healthcare":         ["NSE_INDEX|Nifty Healthcare", "NSE_INDEX|Nifty Healthcare Index",
                                "NSE_INDEX|NIFTY HEALTHCARE INDEX"],
        "Consumer Durables":  ["NSE_INDEX|Nifty Consr Durable", "NSE_INDEX|Nifty Consumer Durables",
                                "NSE_INDEX|NIFTY CONSR DURABLE"],
        "Oil & Gas":          ["NSE_INDEX|Nifty Oil & Gas", "NSE_INDEX|Nifty Oil and Gas",
                                "NSE_INDEX|NIFTY OIL & GAS"],
        "Chemicals":          ["NSE_INDEX|Nifty Chemicals"],
        "Commodities":        ["NSE_INDEX|Nifty Commodities"],
        "MNC":                ["NSE_INDEX|Nifty MNC"],
        "Consumption":        ["NSE_INDEX|Nifty India Consumption", "NSE_INDEX|Nifty Consumption"],
    }
    for name, ikey in INDEX_TRACKER.items():
        # Try primary key, then alternatives
        keys_to_try = KEY_ALTERNATIVES.get(name, [ikey])
        if ikey not in keys_to_try:
            keys_to_try = [ikey] + keys_to_try
        success = False
        for try_key in keys_to_try:
            encoded = try_key.replace('|', '%7C').replace(' ', '%20').replace('&', '%26')
            url = f"https://api.upstox.com/v2/historical-candle/{encoded}/day/{to}/{from_}"
            try:
                async with session.get(url, headers=headers,
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
                            # Update tracker with working key
                            INDEX_TRACKER[name] = try_key
                            success = True
                            break
                    else:
                        body = await r.text()
                        log.warning(f"Index {name} key '{try_key}' failed: {r.status} — {body[:150]}")
            except Exception as e:
                log.warning(f"Index {name} error: {e}")
            await asyncio.sleep(0.2)
        if not success:
            log.warning(f"⚠️ Index {name}: all key formats failed — MID/SML RS will use Nifty as fallback")
    log.info(f"✅ Index cache loaded: {loaded}/{len(INDEX_TRACKER)} indices")


async def ensure_db_columns(session: aiohttp.ClientSession):
    """Verify rs_tv and eps_qoq columns exist by doing test queries.
    IMPORTANT: if a column used in the per-scan stocks upsert is missing,
    Supabase/PostgREST can reject the WHOLE upsert with a 400 error — not
    just silently skip that one field — so this check matters for every
    field added to the stock record, not just these two canaries."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/stocks?select=rs_tv&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — rs_tv column exists")
            elif r.status == 400:
                body = await r.text()
                if 'rs_tv' in body:
                    log.error("❌ rs_tv column MISSING from stocks table!")
                    log.error("   → Go to Supabase SQL Editor and run:")
                    log.error("   alter table public.stocks add column if not exists rs_tv int;")
                    log.error("   alter table public.stocks add column if not exists rs_midcap int;")
                    log.error("   alter table public.stocks add column if not exists rs_smallcap int;")
                    log.error("   alter table public.stocks add column if not exists rs_sector int;")
    except Exception as e:
        log.warning(f"DB column check error: {e}")

    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/stocks?select=eps_qoq&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — eps_qoq (fundamentals growth) column exists")
            elif r.status == 400:
                log.error("❌ Fundamentals growth/trend columns MISSING from stocks table! "
                          "The per-scan upsert may be failing entirely until this is fixed.")
                log.error("   → Go to Supabase SQL Editor and run:")
                log.error("   alter table public.stocks")
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
    except Exception as e:
        log.warning(f"DB column check error (fundamentals growth): {e}")

    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/stocks?select=chg_m_pct&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — chg_m_pct (weekly/monthly change) column exists")
            elif r.status == 400:
                log.error("❌ chg_w_pct/chg_m_pct columns MISSING from stocks table! "
                          "The ENTIRE per-scan stocks upsert has been failing (not just these "
                          "two fields) since these were added — PostgREST rejects the whole "
                          "request when any field is unrecognized.")
                log.error("   → Go to Supabase SQL Editor and run:")
                log.error("   alter table public.stocks")
                log.error("     add column if not exists chg_w_pct numeric,")
                log.error("     add column if not exists chg_m_pct numeric;")
    except Exception as e:
        log.warning(f"DB column check error (chg_w_pct/chg_m_pct): {e}")

    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/index_dashboard?select=rank_d,advances_d&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — rank_d/advances_d (index ranks + breadth) columns exist")
            elif r.status == 400:
                log.error("❌ rank_d/rank_w/rank_m/advances_d/advances_w/advances_m columns MISSING "
                          "from index_dashboard table! The index dashboard upsert may be failing "
                          "entirely until this is fixed.")
                log.error("   → Go to Supabase SQL Editor and run:")
                log.error("   alter table public.index_dashboard")
                log.error("     add column if not exists rank_d int,")
                log.error("     add column if not exists rank_w int,")
                log.error("     add column if not exists rank_m int,")
                log.error("     add column if not exists total_indices int,")
                log.error("     add column if not exists advances_d numeric,")
                log.error("     add column if not exists advances_w numeric,")
                log.error("     add column if not exists advances_m numeric;")
    except Exception as e:
        log.warning(f"DB column check error (index ranks/breadth): {e}")

    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/index_dashboard?select=rank_w_change&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — rank_w_change (week-over-week rank movement) column exists")
            elif r.status == 400:
                log.error("❌ rank_w_history/rank_w_change columns MISSING from index_dashboard table!")
                log.error("   → Go to Supabase SQL Editor and run:")
                log.error("   alter table public.index_dashboard")
                log.error("     add column if not exists rank_w_history text,")
                log.error("     add column if not exists rank_w_change int;")
    except Exception as e:
        log.warning(f"DB column check error (rank_w_change): {e}")

    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/sectors?select=advances_d&limit=1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                log.info("✅ DB columns OK — advances_d (sector breadth) column exists")
            elif r.status == 400:
                log.error("❌ advances_d/advances_w/advances_m columns MISSING from sectors table! "
                          "The sectors upsert may be failing entirely until this is fixed.")
                log.error("   → Go to Supabase SQL Editor and run:")
                log.error("   alter table public.sectors")
                log.error("     add column if not exists advances_d numeric,")
                log.error("     add column if not exists advances_w numeric,")
                log.error("     add column if not exists advances_m numeric;")
    except Exception as e:
        log.warning(f"DB column check error (sector breadth): {e}")





async def save_index_history_to_db(session: aiohttp.ClientSession, name: str, prices: list):
    """Save index price history to Supabase for persistence across restarts."""
    import json as _json
    # Use service role key for writes (anon key blocked by RLS)
    service_key = os.environ.get('SUPABASE_SERVICE_KEY', SUPABASE_KEY)
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    row = {"name": name, "prices": _json.dumps(prices), "updated_at": datetime.now(IST).isoformat()}
    try:
        async with session.post(
            f"{SUPABASE_URL}/rest/v1/index_price_history",
            headers=headers,
            json=row,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            if r.status in (200, 201):
                log.info(f"  💾 Saved {name}: {len(prices)} days to DB")
            else:
                body = await r.text()
                log.warning(f"  Save {name} failed: {r.status} — {body[:200]}")
    except Exception as e:
        log.warning(f"  Save index history failed: {e}")


async def load_index_history_from_db(session: aiohttp.ClientSession, name: str) -> list:
    """Load index price history from Supabase."""
    import json as _json
    service_key = os.environ.get('SUPABASE_SERVICE_KEY', SUPABASE_KEY)
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    encoded_name = name.replace(' ', '%20')
    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/index_price_history?name=eq.{encoded_name}&select=prices",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 200:
                data = await r.json()
                if data and data[0].get("prices"):
                    return _json.loads(data[0]["prices"])
            else:
                body = await r.text()
                log.warning(f"  Load {name} failed: {r.status} — {body[:100]}")
    except Exception as e:
        log.warning(f"  Load index history failed: {e}")
    return []


async def load_index_rank_history(session: aiohttp.ClientSession) -> dict:
    """Load each index's existing rank_w_history from Supabase in one
    query — used to compute week-over-week rank movement without
    needing a per-index round trip."""
    import json as _json
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    result: dict = {}
    try:
        async with session.get(
            f"{SUPABASE_URL}/rest/v1/index_dashboard?select=name,rank_w_history",
            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 200:
                for row in await r.json():
                    raw = row.get('rank_w_history')
                    if raw:
                        try:
                            result[row['name']] = _json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            pass
            else:
                body = await r.text()
                log.warning(f"  Load index rank history failed: {r.status} — {body[:150]}")
    except Exception as e:
        log.warning(f"  Load index rank history error: {e}")
    return result



# ── Seeded Nifty 50 history (2020-2025, 1492 days) ────────────────────
# Uploaded from NSE bhavcopy CSVs — used to bootstrap RS accuracy
NIFTY50_SEED_PRICES = [12182.5, 12282.2, 12226.65, 11993.05, 12052.95, 12025.35, 12215.9, 12256.8, 12329.55, 12362.3, 12343.3, 12355.5, 12352.35, 12224.55, 12169.85, 12106.9, 12180.35, 12248.25, 12119.0, 12055.8, 12129.5, 12035.8, 11962.1, 11661.85, 11707.9, 11979.65, 12089.15, 12137.95, 12098.35, 12031.5, 12107.9, 12201.2, 12174.65, 12113.45, 12045.8, 11992.5, 12125.9, 12080.85, 11829.4, 11797.9, 11678.5, 11633.3, 11201.75, 11132.75, 11303.3, 11251.0, 11269.0, 10989.45, 10451.45, 10458.4, 9590.15, 9955.2, 9197.4, 8967.05, 8468.8, 8263.45, 8745.45, 7610.25, 7801.05, 8317.85, 8641.45, 8660.25, 8281.1, 8597.75, 8253.8, 8083.8, 8792.2, 8748.75, 9111.9, 8993.85, 8925.3, 8992.8, 9266.75, 9261.85, 8981.45, 9187.3, 9313.9, 9154.4, 9282.3, 9380.9, 9553.35, 9859.9, 9293.5, 9205.6, 9270.9, 9199.05, 9251.5, 9239.2, 9196.55, 9383.55, 9142.75, 9136.85, 8823.25, 8879.1, 9066.55, 9106.25, 9039.25, 9029.05, 9314.95, 9490.1, 9580.3, 9826.15, 9979.1, 10061.55, 10029.1, 10142.15, 10167.45, 10046.65, 10116.15, 9902.0, 9972.9, 9813.7, 9914.0, 9881.15, 10091.65, 10244.4, 10311.2, 10471.0, 10305.3, 10288.9, 10383.0, 10312.4, 10302.1, 10430.05, 10551.7, 10607.35, 10763.65, 10799.65, 10705.75, 10813.45, 10768.05, 10802.7, 10607.35, 10618.2, 10739.95, 10901.7, 11022.2, 11162.25, 11132.6, 11215.45, 11194.15, 11131.8, 11300.55, 11202.85, 11102.15, 11073.45, 10891.6, 11095.25, 11101.65, 11200.15, 11214.05, 11270.15, 11322.5, 11308.4, 11300.45, 11178.4, 11247.1, 11385.35, 11408.4, 11312.2, 11371.6, 11466.45, 11472.25, 11549.6, 11559.25, 11647.6, 11387.5, 11470.25, 11535.0, 11527.45, 11333.85, 11355.05, 11317.35, 11278.0, 11449.25, 11464.45, 11440.05, 11521.8, 11604.55, 11516.1, 11504.95, 11250.55, 11153.65, 11131.85, 10805.55, 11050.25, 11227.55, 11222.4, 11247.55, 11416.95, 11503.35, 11662.4, 11738.85, 11834.6, 11914.2, 11930.95, 11934.5, 11971.05, 11680.35, 11762.45, 11873.05, 11896.8, 11937.65, 11896.45, 11930.35, 11767.75, 11889.4, 11729.6, 11670.8, 11642.4, 11669.15, 11813.5, 11908.5, 12120.3, 12263.55, 12461.05, 12631.1, 12749.15, 12690.8, 12719.95, 12780.25, 12874.2, 12938.25, 12771.7, 12859.05, 12926.45, 13055.15, 12858.4, 12987.0, 12968.95, 13109.05, 13113.75, 13133.9, 13258.55, 13355.75, 13392.95, 13529.1, 13478.3, 13513.85, 13558.15, 13567.85, 13682.7, 13740.7, 13760.55, 13328.4, 13466.3, 13601.1, 13749.25, 13873.2, 13932.6, 13981.95, 13981.75, 14018.5, 14132.9, 14199.5, 14146.25, 14137.35, 14347.25, 14484.75, 14563.45, 14564.85, 14595.6, 14433.7, 14281.3, 14521.15, 14644.7, 14590.35, 14371.9, 14238.9, 13967.5, 13817.55, 13634.6, 14281.2, 14647.85, 14789.95, 14895.65, 14924.25, 15115.8, 15109.3, 15106.5, 15173.3, 15163.3, 15314.7, 15313.45, 15208.9, 15118.95, 14981.75, 14675.7, 14707.8, 14982.0, 15097.35, 14529.15, 14761.55, 14919.1, 15245.6, 15080.75, 14938.1, 14956.2, 15098.4, 15174.8, 15030.95, 14929.5, 14910.45, 14721.3, 14557.85, 14744.0, 14736.4, 14814.75, 14549.4, 14324.9, 14507.3, 14845.1, 14690.7, 14867.35, 14637.8, 14683.5, 14819.05, 14873.8, 14834.85, 14310.8, 14504.8, 14581.45, 14617.85, 14359.45, 14296.4, 14406.15, 14341.35, 14485.0, 14653.05, 14864.55, 14894.9, 14631.1, 14634.15, 14496.5, 14617.85, 14724.8, 14823.15, 14942.35, 14850.75, 14696.5, 14677.8, 14923.15, 15108.1, 15030.15, 14906.05, 15175.3, 15197.7, 15208.45, 15301.45, 15337.85, 15435.65, 15582.8, 15574.85, 15576.2, 15690.35, 15670.25, 15751.65, 15740.1, 15635.35, 15737.75, 15799.35, 15811.85, 15869.25, 15767.55, 15691.4, 15683.35, 15746.5, 15772.75, 15686.95, 15790.45, 15860.35, 15814.7, 15748.45, 15721.5, 15680.0, 15722.2, 15834.35, 15818.25, 15879.65, 15727.9, 15689.8, 15692.6, 15812.35, 15853.95, 15924.2, 15923.4, 15752.4, 15632.1, 15824.05, 15856.05, 15824.45, 15746.45, 15709.4, 15778.45, 15763.05, 15885.15, 16130.75, 16258.8, 16294.6, 16238.2, 16258.25, 16280.1, 16282.25, 16364.4, 16529.1, 16563.05, 16614.6, 16568.85, 16450.5, 16496.45, 16624.6, 16634.65, 16636.9, 16705.2, 16931.05, 17132.2, 17076.25, 17234.15, 17323.6, 17377.8, 17362.1, 17353.5, 17369.25, 17355.3, 17380.0, 17519.45, 17629.5, 17585.15, 17396.9, 17562.0, 17546.65, 17822.95, 17853.2, 17855.1, 17748.6, 17711.3, 17618.15, 17532.05, 17691.25, 17822.3, 17646.0, 17790.35, 17895.2, 17945.95, 17991.95, 18161.75, 18338.55, 18477.05, 18418.75, 18266.6, 18178.1, 18114.9, 18125.4, 18268.4, 18210.95, 17857.25, 17671.65, 17929.65, 17888.95, 17829.2, 17916.8, 18068.55, 18044.25, 18017.2, 17873.6, 18102.75, 18109.45, 17999.2, 17898.65, 17764.8, 17416.55, 17503.35, 17415.05, 17536.25, 17026.45, 17053.95, 16983.2, 17166.9, 17401.65, 17196.7, 16912.25, 17176.7, 17469.75, 17516.85, 17511.3, 17368.25, 17324.9, 17221.4, 17248.4, 16985.2, 16614.2, 16770.85, 16955.45, 17072.6, 17003.75, 17086.25, 17233.25, 17213.6, 17203.95, 17354.05, 17625.7, 17805.25, 17925.25, 17745.9, 17812.7, 18003.3, 18055.75, 18212.35, 18257.8, 18255.75, 18308.1, 18113.05, 17938.4, 17757.0, 17617.15, 17149.1, 17277.95, 17110.15, 17101.95, 17339.85, 17576.85, 17780.0, 17560.2, 17516.3, 17213.6, 17266.75, 17463.8, 17605.85, 17374.75, 16842.8, 17352.45, 17322.2, 17304.6, 17276.3, 17206.65, 17092.2, 17063.25, 16247.95, 16658.4, 16793.9, 16605.95, 16498.05, 16245.35, 15863.15, 16013.45, 16345.35, 16594.9, 16630.45, 16871.3, 16663.0, 16975.35, 17287.05, 17117.6, 17315.5, 17245.65, 17222.75, 17153.0, 17222.0, 17325.3, 17498.25, 17464.75, 17670.45, 18053.4, 17957.4, 17807.65, 17639.55, 17784.35, 17674.95, 17530.3, 17475.65, 17173.65, 16958.65, 17136.55, 17392.6, 17171.95, 16953.95, 17200.8, 17038.4, 17245.05, 17102.55, 17069.1, 16677.6, 16682.65, 16411.25, 16301.85, 16240.05, 16167.1, 15808.0, 15782.15, 15842.3, 16259.3, 16240.3, 15809.4, 16266.15, 16214.7, 16125.15, 16025.8, 16170.15, 16352.45, 16661.4, 16584.55, 16522.75, 16628.0, 16584.3, 16569.55, 16416.35, 16356.25, 16478.1, 16201.8, 15774.4, 15732.1, 15692.15, 15360.6, 15293.5, 15350.15, 15638.8, 15413.3, 15556.65, 15699.25, 15832.05, 15850.2, 15799.1, 15780.25, 15752.05, 15835.35, 15810.85, 15989.8, 16132.9, 16220.6, 16216.0, 16058.3, 15966.65, 15938.65, 16049.2, 16278.5, 16340.55, 16520.85, 16605.25, 16719.45, 16631.0, 16483.85, 16641.8, 16929.6, 17158.25, 17340.05, 17345.45, 17388.15, 17382.0, 17397.5, 17525.1, 17534.75, 17659.0, 17698.15, 17825.25, 17944.25, 17956.5, 17758.45, 17490.7, 17577.5, 17604.95, 17522.45, 17558.9, 17312.9, 17759.3, 17542.8, 17539.45, 17665.8, 17655.6, 17624.4, 17798.75, 17833.35, 17936.35, 18070.05, 18003.75, 17877.4, 17530.85, 17622.25, 17816.25, 17718.35, 17629.8, 17327.35, 17016.3, 17007.4, 16858.6, 16818.1, 17094.35, 16887.35, 17274.3, 17331.8, 17314.65, 17241.0, 16983.55, 17123.6, 17014.35, 17185.7, 17311.8, 17486.95, 17512.25, 17563.95, 17576.3, 17730.75, 17656.35, 17736.95, 17786.8, 18012.2, 18145.4, 18082.85, 18052.7, 18117.15, 18202.8, 18157.0, 18028.2, 18349.7, 18329.15, 18403.4, 18409.65, 18343.9, 18307.65, 18159.95, 18244.2, 18267.25, 18484.1, 18512.75, 18562.75, 18618.05, 18758.35, 18812.5, 18696.1, 18701.05, 18642.75, 18560.5, 18609.35, 18496.6, 18497.15, 18608.0, 18660.3, 18414.9, 18269.0, 18420.45, 18385.3, 18199.1, 18127.35, 17806.8, 18014.6, 18132.3, 18122.5, 18191.0, 18105.3, 18197.45, 18232.55, 18042.95, 17992.15, 17859.45, 18101.2, 17914.15, 17895.7, 17858.2, 17956.6, 17894.85, 18053.3, 18165.35, 18107.85, 18027.65, 18118.55, 18118.3, 17891.95, 17604.35, 17648.95, 17662.15, 17616.3, 17610.4, 17854.05, 17764.6, 17721.5, 17871.7, 17893.45, 17856.5, 17770.9, 17929.85, 18015.85, 18035.85, 17944.2, 17844.6, 17826.7, 17554.3, 17511.25, 17465.8, 17392.7, 17303.95, 17450.9, 17321.9, 17594.35, 17711.45, 17754.4, 17589.6, 17412.9, 17154.3, 17043.3, 16972.15, 16985.6, 17100.05, 16988.4, 17107.5, 17151.9, 17076.9, 16945.05, 16985.7, 16951.7, 17080.7, 17359.75, 17398.05, 17557.05, 17599.15, 17624.05, 17722.3, 17812.4, 17828.0, 17706.85, 17660.15, 17618.75, 17624.45, 17624.05, 17743.4, 17769.25, 17813.6, 17915.05, 18065.0, 18147.65, 18089.85, 18255.8, 18069.0, 18264.4, 18265.95, 18315.1, 18297.0, 18314.8, 18398.85, 18286.5, 18181.75, 18129.95, 18203.4, 18314.4, 18348.0, 18285.4, 18321.15, 18499.35, 18598.65, 18633.85, 18534.4, 18487.75, 18534.1, 18593.85, 18599.0, 18726.4, 18634.55, 18563.4, 18601.5, 18716.15, 18755.9, 18688.1, 18826.0, 18755.45, 18816.7, 18856.85, 18771.25, 18665.5, 18691.2, 18817.4, 18972.1, 19189.05, 19322.55, 19389.0, 19398.5, 19497.3, 19331.8, 19355.9, 19439.4, 19384.3, 19413.75, 19564.5, 19711.45, 19749.25, 19833.15, 19979.15, 19745.0, 19672.35, 19680.6, 19778.3, 19659.9, 19646.05, 19753.8, 19733.55, 19526.55, 19381.65, 19517.0, 19597.3, 19570.85, 19632.55, 19543.1, 19428.3, 19434.55, 19465.0, 19365.25, 19310.15, 19393.6, 19396.45, 19444.0, 19386.7, 19265.8, 19306.05, 19342.65, 19347.45, 19253.8, 19435.3, 19528.8, 19574.9, 19611.05, 19727.05, 19819.95, 19996.35, 19993.2, 20070.0, 20103.1, 20192.35, 20133.3, 19901.4, 19742.35, 19674.25, 19674.55, 19664.7, 19716.45, 19523.55, 19638.3, 19528.75, 19436.1, 19545.75, 19653.5, 19512.35, 19689.85, 19811.35, 19794.0, 19751.05, 19731.75, 19811.5, 19671.1, 19624.7, 19542.65, 19281.75, 19122.15, 18857.25, 19047.25, 19140.9, 19079.6, 18989.15, 19133.25, 19230.6, 19411.75, 19406.7, 19443.5, 19395.3, 19425.35, 19525.55, 19443.55, 19675.45, 19765.2, 19731.8, 19694.0, 19783.4, 19811.85, 19802.0, 19794.7, 19889.7, 20096.6, 20133.15, 20267.9, 20686.8, 20855.1, 20937.7, 20901.15, 20969.4, 20997.1, 20906.4, 20926.35, 21182.7, 21456.65, 21418.65, 21453.1, 21150.15, 21255.05, 21349.4, 21441.35, 21654.75, 21778.7, 21731.4, 21741.9, 21665.8, 21517.35, 21658.6, 21710.8, 21513.0, 21544.85, 21618.7, 21647.2, 21894.55, 22097.45, 22032.3, 21571.95, 21462.25, 21622.4, 21571.8, 21238.8, 21453.95, 21352.6, 21737.6, 21522.1, 21725.7, 21697.45, 21853.8, 21771.7, 21929.4, 21930.5, 21717.95, 21782.5, 21616.05, 21743.25, 21840.05, 21910.75, 22040.7, 22122.25, 22196.95, 22055.05, 22217.45, 22212.7, 22122.05, 22198.35, 21951.15, 21982.8, 22338.75, 22378.4, 22405.6, 22356.3, 22474.05, 22493.55, 22332.65, 22335.7, 21997.7, 22146.65, 22023.35, 22055.7, 21817.45, 21839.1, 22011.95, 22096.75, 22004.7, 22123.65, 22326.9, 22462.0, 22453.3, 22434.65, 22514.65, 22513.7, 22666.3, 22642.75, 22753.8, 22519.4, 22272.5, 22147.9, 21995.85, 22147.0, 22336.4, 22368.0, 22402.4, 22570.35, 22419.95, 22643.4, 22604.85, 22648.2, 22475.85, 22442.7, 22302.5, 22302.5, 21957.5, 22055.2, 22104.05, 22217.85, 22200.55, 22403.85, 22466.1, 22502.0, 22529.05, 22597.8, 22967.65, 22957.1, 22932.45, 22888.15, 22704.7, 22488.65, 22530.7, 23263.9, 21884.5, 22620.35, 22821.4, 23290.15, 23259.2, 23264.85, 23322.95, 23398.9, 23465.6, 23557.9, 23516.0, 23567.0, 23501.1, 23537.85, 23721.3, 23868.8, 24044.5, 24010.6, 24141.95, 24123.85, 24286.5, 24302.15, 24323.85, 24320.55, 24433.2, 24324.45, 24315.95, 24502.15, 24586.7, 24613.0, 24800.85, 24530.9, 24509.25, 24479.05, 24413.5, 24406.1, 24834.85, 24836.1, 24857.3, 24951.15, 25010.9, 24717.7, 24055.6, 23992.55, 24297.5, 24117.0, 24367.5, 24347.0, 24139.0, 24143.75, 24541.15, 24572.65, 24698.85, 24770.2, 24811.5, 24823.15, 25010.6, 25017.75, 25052.35, 25151.95, 25235.9, 25278.7, 25279.85, 25198.7, 25145.1, 24852.15, 24936.4, 25041.1, 24918.45, 25388.9, 25356.5, 25383.75, 25418.55, 25377.55, 25415.8, 25790.95, 25939.05, 25940.4, 26004.15, 26216.05, 26178.95, 25810.85, 25796.9, 25250.1, 25014.6, 24795.75, 25013.15, 24981.95, 24998.45, 24964.25, 25127.95, 25057.35, 24971.3, 24749.85, 24854.05, 24781.1, 24472.1, 24435.5, 24399.4, 24180.8, 24339.15, 24466.85, 24340.85, 24205.35, 24304.35, 23995.35, 24213.3, 24484.05, 24199.35, 24148.2, 24141.3, 23883.45, 23559.05, 23532.7, 23453.8, 23518.5, 23349.9, 23907.25, 24221.9, 24194.5, 24274.9, 23914.15, 24131.1, 24276.05, 24457.15, 24467.45, 24708.4, 24677.8, 24619.0, 24610.05, 24641.8, 24548.7, 24768.3, 24668.25, 24336.0, 24198.85, 23951.7, 23587.5, 23753.45, 23727.65, 23750.2, 23813.4, 23644.9, 23644.8, 23742.9, 24188.65, 24004.75, 23616.05, 23707.9, 23688.95, 23526.5, 23431.5, 23085.95, 23176.05, 23213.2, 23311.8, 23203.2, 23344.75, 23024.65, 23155.35, 23205.35, 23092.2, 22829.15, 22957.25, 23163.1, 23249.5, 23508.4, 23482.15, 23361.05, 23739.25, 23696.3, 23603.35, 23559.95, 23381.6, 23071.8, 23045.25, 23031.4, 22929.25, 22959.5, 22945.3, 22932.9, 22913.15, 22795.9, 22553.35, 22547.55, 22545.05, 22124.7, 22119.3, 22082.65, 22337.3, 22544.7, 22552.5, 22460.3, 22497.9, 22470.5, 22397.2, 22508.75, 22834.3, 22907.6, 23190.65, 23350.4, 23658.35, 23668.65, 23486.85, 23591.95, 23519.35, 23165.7, 23332.35, 23250.1, 22904.45, 22161.6, 22535.85, 22399.15, 22828.55, 23328.55, 23437.2, 23851.65, 24125.55, 24167.25, 24328.95, 24246.7, 24039.35, 24328.5, 24335.95, 24334.2, 24346.7, 24461.15, 24379.6, 24414.4, 24273.8, 24008.0, 24924.7, 24578.35, 24666.9, 25062.1, 25019.8, 24945.45, 24683.9, 24813.45, 24609.7, 24853.15, 25001.15, 24826.2, 24752.45, 24833.6, 24750.7, 24716.6, 24542.5, 24620.2, 24750.9, 25003.05, 25103.2, 25104.25, 25141.4, 24888.2, 24718.6, 24946.5, 24853.4, 24812.05, 24793.25, 25112.4, 24971.9, 25044.35, 25244.75, 25549.0, 25637.8, 25517.05, 25541.8, 25453.4, 25405.3, 25461.0, 25461.3, 25522.5, 25476.1, 25355.25, 25149.85, 25082.3, 25195.8, 25212.05, 25111.45, 24968.4, 25090.7, 25060.9, 25219.9, 25062.1, 24837.0, 24680.9, 24821.1, 24855.05, 24768.35, 24565.35, 24722.75, 24649.55, 24574.2, 24596.15, 24363.3, 24585.05, 24487.4, 24619.35, 24631.3, 24876.95, 24980.65, 25050.55, 25083.75, 24870.1, 24967.75, 24712.05, 24500.9, 24426.85, 24625.05, 24579.6, 24715.05, 24734.3, 24741.0, 24773.15, 24868.6, 24973.1, 25005.5, 25114.0, 25069.2, 25239.1, 25330.25, 25423.6, 25327.05, 25202.35, 25169.5, 25056.9, 24890.85, 24654.7, 24634.9, 24611.1, 24836.3, 24894.25, 25077.65, 25108.3, 25046.15, 25181.8, 25285.35, 25227.35, 25145.5, 25323.55, 25585.3, 25709.85, 25843.15, 25868.6, 25891.4, 25795.15, 25966.05, 25936.2, 26053.9, 25877.85, 25722.1, 25763.35, 25597.65, 25509.7, 25492.3, 25574.35, 25694.95, 25875.8, 25879.15, 25910.05, 26013.45, 25910.05, 26052.65, 26192.15, 26068.15, 25959.5, 25884.8, 26205.3, 26215.55, 26202.95, 26175.75, 26032.2, 25986.0, 26033.75, 26186.45, 25960.55, 25839.65, 25758.0, 25898.55, 26046.95, 26027.3, 25860.1, 25818.55, 25815.55, 25966.4, 26172.4, 26177.15, 26142.1, 26042.3, 25942.1, 25938.85, 26129.6]

async def seed_index_history_if_needed(session: aiohttp.ClientSession):
    """
    One-time seed: push 1492 days of Nifty 50 history to Supabase.
    Then merge with Upstox fresh data (2026 onwards) for complete history.
    """
    global nifty_cache
    existing = await load_index_history_from_db(session, "Nifty 50")
    if len(existing) >= 1400:
        log.info(f"✅ Nifty 50 already seeded: {len(existing)} days in DB — skipping")
        return

    upstox_prices = nifty_cache.get("prices", [])
    seed = NIFTY50_SEED_PRICES

    if upstox_prices:
        overlap_days = 125
        new_from_upstox = upstox_prices[overlap_days:]
        merged = seed + new_from_upstox
    else:
        merged = seed

    await save_index_history_to_db(session, "Nifty 50", merged)
    nifty_cache = {'prices': merged, 'volumes': nifty_cache.get('volumes', [])}
    log.info(f"✅ Seeded Nifty 50: {len(merged)} days saved to DB!")

async def fetch_full_nifty_history(session: aiohttp.ClientSession) -> dict:
    """
    One-time fetch of 5yr Nifty/Midcap/Smallcap history.
    Tries multiple sources until one works.
    Returns dict: {"Nifty 50": [prices...], "Midcap 150": [...], "Smallcap 250": [...]}
    """
    results = {}

    # Source 1: Yahoo Finance (yfinance style direct URL)
    yahoo_map = {
        "Nifty 50":    "%5ENSEI",
        "Midcap 150":  "%5ENIMDCP150",
        "Smallcap 250":"%5ENSMCP250",
    }
    for name, ticker in yahoo_map.items():
        if name in results:
            continue
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5y"
        try:
            async with session.get(url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    prices = [c for c in closes if c is not None]
                    if len(prices) >= 500:
                        results[name] = prices
                        log.info(f"  ✅ Yahoo {name}: {len(prices)} days")
        except Exception as e:
            log.warning(f"  Yahoo {name}: {e}")
        await asyncio.sleep(0.3)

    # Source 2: NSE Bhavcopy index CSV (already works for constituents)
    if "Nifty 50" not in results:
        nse_indices = {
            "Nifty 50":    "NIFTY 50",
            "Midcap 150":  "NIFTY MIDCAP 150",
            "Smallcap 250":"NIFTY SMALLCAP 250",
        }
        # Try NSE index historical API
        for name, idx_name in nse_indices.items():
            if name in results:
                continue
            encoded = idx_name.replace(" ", "%20")
            url = f"https://www.nseindia.com/api/historical/indicesHistory?indexType={encoded}&from=01-Jan-2020&to=06-Jul-2026"
            try:
                async with session.get(url,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        data = await r.json()
                        records = data.get("data", {}).get("indexCloseOnlineRecords", [])
                        prices = [float(rec["EOD_CLOSE_INDEX_VAL"]) for rec in reversed(records)]
                        if len(prices) >= 500:
                            results[name] = prices
                            log.info(f"  ✅ NSE {name}: {len(prices)} days")
            except Exception as e:
                log.warning(f"  NSE {name}: {e}")
            await asyncio.sleep(0.5)

    # Source 3: Stooq CSV
    stooq_map = {
        "Nifty 50":    "%5ensei",
        "Midcap 150":  "%5ecnxmc",
        "Smallcap 250":"%5ecnxsc",
    }
    for name, ticker in stooq_map.items():
        if name in results:
            continue
        url = f"https://stooq.com/q/d/l/?s={ticker}&i=d"
        try:
            async with session.get(url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    text = await r.text()
                    lines = text.strip().split("\n")
                    prices = []
                    for line in lines[1:]:
                        parts = line.split(",")
                        if len(parts) >= 5 and parts[4] not in ("N/D", "null", ""):
                            try: prices.append(float(parts[4]))
                            except: pass
                    prices = list(reversed(prices))
                    if len(prices) >= 500:
                        results[name] = prices
                        log.info(f"  ✅ Stooq {name}: {len(prices)} days")
        except Exception as e:
            log.warning(f"  Stooq {name}: {e}")
        await asyncio.sleep(0.3)

    return results


async def load_full_history_once(session: aiohttp.ClientSession):
    """
    Check if we already have 2yr history in DB.
    If not, fetch full history from external source and save to DB.
    Only runs once — after that DB has enough history.
    """
    global nifty_cache, midcap_cache, smallcap_cache

    # Check nifty_cache directly — seed updates it immediately without DB round-trip
    if len(nifty_cache.get('prices', [])) >= 1400:
        log.info(f"✅ Nifty cache has {len(nifty_cache['prices'])}d — skipping external fetch")
        # Still try to get Midcap/Smallcap from Yahoo if not cached
        db_mid = await load_index_history_from_db(session, "Midcap 150")
        db_sml = await load_index_history_from_db(session, "Smallcap 250")
        if db_mid: midcap_cache = {'prices': db_mid}
        if db_sml: smallcap_cache = {'prices': db_sml}
        if not db_mid or not db_sml:
            # Fetch Midcap/Smallcap from Yahoo
            results = await fetch_full_nifty_history(session)
            if "Midcap 150" in results:
                midcap_cache = {"prices": results["Midcap 150"]}
                await save_index_history_to_db(session, "Midcap 150", results["Midcap 150"])
                log.info(f"  💾 Saved Midcap 150: {len(results['Midcap 150'])} days")
            if "Smallcap 250" in results:
                smallcap_cache = {"prices": results["Smallcap 250"]}
                await save_index_history_to_db(session, "Smallcap 250", results["Smallcap 250"])
                log.info(f"  💾 Saved Smallcap 250: {len(results['Smallcap 250'])} days")
        return

    log.info(f"📥 DB has only {len(db_nifty)}d — fetching full history from external sources…")
    results = await fetch_full_nifty_history(session)

    if not results:
        log.warning("⚠️ All external sources blocked — history will accumulate daily")
        return

    for name, prices in results.items():
        existing = await load_index_history_from_db(session, name)
        if existing and len(existing) >= len(prices):
            log.info(f"  Keeping DB {len(existing)}d for {name} (longer than external {len(prices)}d)")
            continue
        await save_index_history_to_db(session, name, prices)
        log.info(f"  💾 Saved {name}: {len(prices)} days to DB")

    if "Nifty 50" in results and len(results["Nifty 50"]) > len(nifty_cache.get('prices', [])):
        nifty_cache = {"prices": results["Nifty 50"]}
    if "Midcap 150" in results:
        midcap_cache = {"prices": results["Midcap 150"]}
    if "Smallcap 250" in results:
        smallcap_cache = {"prices": results["Smallcap 250"]}
    log.info("✅ Full history loaded!")


async def fetch_yahoo_stock_history(session: aiohttp.ClientSession, sym: str) -> list:
    """Fetch 2yr daily closes for a single NSE stock from Yahoo Finance.
    Tries NSE (.NS) first, then BSE (.BO) as fallback."""
    for suffix in [".NS", ".BO"]:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}{suffix}?interval=1d&range=2y"
        try:
            async with session.get(url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    prices = [c for c in closes if c is not None]
                    if len(prices) >= 100:
                        return prices
        except:
            pass
    return []


async def extend_stock_history_from_yahoo(session: aiohttp.ClientSession):
    """
    One-time: extend stock histories from Yahoo Finance (2yr).
    Only fetches stocks where Upstox gave < 400 days.
    Runs in background after initial scan starts.
    """
    global historical_cache
    
    short_stocks = [sym for sym, data in historical_cache.items() 
                    if len(data.get('prices', [])) < 450]
    
    if not short_stocks:
        log.info("✅ All stocks have 400+ days history — no Yahoo extension needed")
        return
        
    log.info(f"📥 Extending history for {len(short_stocks)} stocks via Yahoo Finance…")
    extended = 0
    failed = 0
    
    sem = asyncio.Semaphore(10)  # 10 concurrent requests
    
    async def fetch_one(sym):
        nonlocal extended, failed
        async with sem:
            yahoo_prices = await fetch_yahoo_stock_history(session, sym)
            if len(yahoo_prices) > len(historical_cache.get(sym, {}).get('prices', [])):
                # Keep existing volumes — just extend the prices
                existing_vols = historical_cache.get(sym, {}).get('volumes', [])
                historical_cache[sym] = {
                    'prices':  yahoo_prices,
                    'volumes': existing_vols,  # keep original Upstox volumes
                }
                extended += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
    
    tasks = [fetch_one(sym) for sym in short_stocks]  # all stocks
    await asyncio.gather(*tasks)
    log.info(f"✅ Yahoo history extension: {extended} extended, {failed} failed/skipped")


# ── Full 2yr history → Supabase (all stocks, at startup) ──────────────
async def fetch_yahoo_full_ohlcv(session: aiohttp.ClientSession, sym: str,
                                  range_period: str = "2y", min_points: int = 100) -> Optional[dict]:
    """Fetch daily OHLCV (dates, close, volume, high, low) for one NSE stock
    from Yahoo Finance. Tries .NS first, then .BO as fallback.
    range_period/min_points let this double as either a full 2yr backfill
    (range='2y', min_points=100) or a lightweight incremental fetch for the
    EOD daily update (range='10d', min_points=1) — same parsing logic,
    much smaller response for the common case where we already have most
    of the history and just need the last day or two."""
    for suffix in [".NS", ".BO"]:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}{suffix}?interval=1d&range={range_period}"
        try:
            async with session.get(url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                result_list = data.get("chart", {}).get("result") or []
                if not result_list:
                    continue
                result = result_list[0]
                timestamps = result.get("timestamp") or []
                quote = (result.get("indicators", {}).get("quote") or [{}])[0]
                closes  = quote.get("close")  or []
                volumes = quote.get("volume") or []
                highs   = quote.get("high")   or []
                lows    = quote.get("low")    or []

                dates, prices, vols, hi, lo = [], [], [], [], []
                for i, c in enumerate(closes):
                    if c is None:
                        continue
                    ts = timestamps[i] if i < len(timestamps) else None
                    dates.append(
                        datetime.fromtimestamp(ts, tz=IST).strftime('%Y-%m-%d') if ts else None
                    )
                    prices.append(round(c, 2))
                    v = volumes[i] if i < len(volumes) else None
                    vols.append(int(v) if v is not None else None)
                    h = highs[i] if i < len(highs) else None
                    hi.append(round(h, 2) if h is not None else None)
                    l = lows[i] if i < len(lows) else None
                    lo.append(round(l, 2) if l is not None else None)

                if len(prices) >= min_points:
                    return {'dates': dates, 'prices': prices, 'volumes': vols,
                            'highs': hi, 'lows': lo}
        except Exception:
            pass
    return None


async def ensure_full_history_table(session: aiohttp.ClientSession,
                                     retries: int = 6, delay: float = 10.0) -> bool:
    """
    Verify the stock_full_history table exists. Supabase's PostgREST layer
    caches its schema, so a table created via the SQL Editor can 404 for a
    short while after creation even though it exists in Postgres. Retry a
    few times with a delay before giving up, so a fresh table created just
    before a deploy doesn't need a second manual restart.
    """
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
                f"{SUPABASE_URL}/rest/v1/stock_full_history?select=sym&limit=1",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    log.info("✅ stock_full_history table OK"
                              + (f" (after {attempt} attempt(s))" if attempt > 1 else ""))
                    return True
                last_status = r.status
                last_body = await r.text()
        except Exception as e:
            last_status = None
            last_body = str(e)

        if attempt < retries:
            log.warning(f"stock_full_history not ready yet (attempt {attempt}/{retries}, "
                        f"status={last_status}) — retrying in {delay:.0f}s "
                        f"(PostgREST schema cache may still be reloading)…")
            await asyncio.sleep(delay)

    log.error("❌ stock_full_history table MISSING or misconfigured (after retries)!")
    log.error(f"   status={last_status} body={last_body[:200]}")
    log.error("   → If you already ran the CREATE TABLE SQL, force a schema reload:")
    log.error("     Supabase Dashboard → Settings → API → 'Reload schema', or run:")
    log.error("     NOTIFY pgrst, 'reload schema';")
    log.error("   → Otherwise, go to Supabase SQL Editor and run:")
    log.error("   create table if not exists public.stock_full_history (")
    log.error("     sym text primary key,")
    log.error("     dates jsonb, prices jsonb, volumes jsonb,")
    log.error("     highs jsonb, lows jsonb,")
    log.error("     days_count int, updated_at timestamptz")
    log.error("   );")
    return False


async def save_full_history_batch_to_db(session: aiohttp.ClientSession, rows: list):
    """Upsert full-history rows into Supabase in small chunks (payload per
    row is large — full 2yr OHLCV — so chunks are kept smaller than the
    generic supabase_upsert default)."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/stock_full_history?on_conflict=sym"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    CHUNK = 40
    chunks = [rows[i:i+CHUNK] for i in range(0, len(rows), CHUNK)]
    sem = asyncio.Semaphore(5)
    uploaded = 0

    async def upload(chunk):
        nonlocal uploaded
        async with sem:
            for attempt in (1, 2):
                try:
                    async with session.post(url, headers=headers, json=chunk,
                                            timeout=aiohttp.ClientTimeout(total=60)) as r:
                        if r.status in (200, 201, 204):
                            uploaded += len(chunk)
                            return
                        text = await r.text()
                        # PGRST205 = PostgREST schema cache hasn't picked up the
                        # table yet — wait a bit and try once more before giving up.
                        if attempt == 1 and r.status == 404 and 'PGRST205' in text:
                            await asyncio.sleep(5)
                            continue
                        log.warning(f"stock_full_history upsert failed: {r.status} {text[:150]}")
                        return
                except Exception as e:
                    if attempt == 1:
                        await asyncio.sleep(2)
                        continue
                    log.error(f"stock_full_history upsert error: {e}")
                    return

    await asyncio.gather(*[upload(c) for c in chunks])
    log.info(f"  💾 Uploaded {uploaded}/{len(rows)} full-history rows to Supabase")


async def fetch_full_history_for_symbols(session: aiohttp.ClientSession, symbols: list,
                                          label: str = "full") -> int:
    """
    Fetch the full 2-year daily OHLCV history from Yahoo Finance for the
    given list of symbols and persist it into Supabase `stock_full_history`
    + historical_cache/history_dates_cache. This is the expensive full
    fetch — used only for symbols that are missing or stale in Supabase,
    NOT for every stock on every restart (see load_history_at_startup).
    Returns the count of symbols successfully fetched.
    """
    if not symbols:
        return 0

    table_ready = await ensure_full_history_table(session)
    if not table_ready:
        log.error("⏭️  Skipping Yahoo history fetch this run — table still unavailable "
                   "(will try again on next restart).")
        return 0

    total = len(symbols)
    log.info(f"📥 Fetching full 2yr Yahoo history for {total} stocks ({label})…")

    sem = asyncio.Semaphore(20)
    rows: list = []
    done = 0
    failed = 0
    failed_syms: list = []
    lock = asyncio.Lock()

    async def fetch_one(sym):
        nonlocal done, failed
        async with sem:
            data = await fetch_yahoo_full_ohlcv(session, sym)
            await asyncio.sleep(0.02)
        async with lock:
            if data:
                # Yahoo's range=2y/interval=1d includes TODAY's still-forming
                # candle whenever the market is open — its "close" is really
                # just the latest traded price at fetch time, not a real
                # daily close. If we let that become prices[-1] while the
                # market is open, every live scan's chg% calc (which assumes
                # prices[-1] is the most recent COMPLETED close) ends up
                # comparing live price against a stale intraday snapshot
                # from whenever this fetch ran, instead of yesterday's real
                # close — producing wrong/stale % change all session long.
                # Drop that last bar in this case; keep it once the market
                # has closed (EOD refresh), when it's a genuine final close.
                today_ist = datetime.now(IST).strftime('%Y-%m-%d')
                mkt_open = is_market_open()
                will_trim = mkt_open and data['dates'] and data['dates'][-1] == today_ist

                if will_trim:
                    for k in ('dates', 'prices', 'volumes', 'highs', 'lows'):
                        data[k] = data[k][:-1]

                rows.append({
                    'sym':        sym,
                    'dates':      json.dumps(data['dates']),
                    'prices':     json.dumps(data['prices']),
                    'volumes':    json.dumps(data['volumes']),
                    'highs':      json.dumps(data['highs']),
                    'lows':       json.dumps(data['lows']),
                    'days_count': len(data['prices']),
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                })
                # Also feed straight into the in-memory caches used by RS
                # calculations — Upstox only gives ~550 days at best, so
                # this Yahoo 2yr pull is the authoritative source for RS.
                historical_cache[sym] = {
                    'prices':  data['prices'],
                    'volumes': [v if v is not None else 0 for v in data['volumes']],
                    'highs':   [h if h is not None else p for h, p in zip(data['highs'], data['prices'])],
                    'lows':    [l if l is not None else p for l, p in zip(data['lows'],  data['prices'])],
                }
                history_dates_cache[sym] = data['dates']
                done += 1
            else:
                failed += 1
                failed_syms.append(sym)
            seen = done + failed
            if seen % 200 == 0 or seen == total:
                log.info(f"  …{seen}/{total} fetched ({done} ok, {failed} failed)")
            # Upload incrementally so partial progress survives a crash/timeout
            if len(rows) >= 200:
                batch, rows[:] = rows[:], []
                await save_full_history_batch_to_db(session, batch)

    await asyncio.gather(*[fetch_one(sym) for sym in symbols])

    # Retry pass — Yahoo fails a random subset of requests each run
    # (rate-limits, timeouts) that has nothing to do with the symbol itself.
    # Without a retry, a stock that's unlucky on this particular run keeps
    # whatever historical_cache value it already had — potentially days
    # stale — until some future run happens to succeed for it. One retry
    # pass over just the failures fixes most of these transient misses
    # cheaply, since it's usually a small fraction of the full universe.
    if failed_syms:
        retry_list = failed_syms[:]
        failed_syms = []
        log.info(f"🔁 Retrying {len(retry_list)} stocks that failed the first Yahoo fetch pass…")
        await asyncio.sleep(2)
        await asyncio.gather(*[fetch_one(sym) for sym in retry_list])
        recovered = len(retry_list) - len(failed_syms)
        # Use the final unresolved list as the source of truth instead of
        # subtracting — fetch_one's failure branch increments `failed` on
        # EVERY attempt, so a symbol that fails both the first pass and the
        # retry was being counted twice (e.g. "144 failed out of 72").
        failed = len(failed_syms)
        log.info(f"🔁 Retry pass complete: {recovered} recovered, {len(failed_syms)} still failing after retry")

    if rows:
        await save_full_history_batch_to_db(session, rows)

    log.info(f"✅ Yahoo history fetch ({label}) complete: {done} ok, {failed} failed out of {total}")
    return done


async def load_all_history_from_supabase(session: aiohttp.ClientSession) -> list:
    """
    Load previously-fetched full-history rows straight from Supabase —
    ZERO Yahoo calls. This is the key optimization: without it, every
    single restart re-fetched full 2yr history from Yahoo for all ~2385
    stocks, which is both slow and the main source of Yahoo rate-limiting
    (the cause of the RRKABEL stale-data bug earlier). Now a restart just
    loads what's already stored, and Yahoo is only hit for symbols that
    are missing entirely or whose stored data has gone stale.
    Returns the list of symbols that need a Yahoo fetch (missing/stale).
    """
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    all_rows: list = []
    PAGE = 1000
    offset = 0
    while True:
        try:
            page_headers = {**headers, "Range": f"{offset}-{offset + PAGE - 1}"}
            async with session.get(
                f"{SUPABASE_URL}/rest/v1/stock_full_history"
                f"?select=sym,dates,prices,volumes,highs,lows,updated_at",
                headers=page_headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status not in (200, 206):
                    log.warning(f"load_all_history_from_supabase page failed: status={r.status}")
                    break
                page = await r.json()
                all_rows.extend(page)
                if len(page) < PAGE:
                    break
                offset += PAGE
        except Exception as e:
            log.error(f"load_all_history_from_supabase error: {e}")
            break

    loaded = 0
    stale_or_missing: list = []
    found_syms: set = set()
    today = datetime.now(IST).date()

    for row in all_rows:
        sym = row.get('sym')
        if not sym:
            continue
        found_syms.add(sym)
        try:
            def parse(v):
                if v is None:
                    return []
                return json.loads(v) if isinstance(v, str) else v
            dates   = parse(row.get('dates'))
            prices  = parse(row.get('prices'))
            volumes = parse(row.get('volumes'))
            highs   = parse(row.get('highs'))
            lows    = parse(row.get('lows'))

            if len(prices) < 100:
                stale_or_missing.append(sym)
                continue

            historical_cache[sym] = {
                'prices':  prices,
                'volumes': [v if v is not None else 0 for v in volumes],
                'highs':   [h if h is not None else p for h, p in zip(highs, prices)],
                'lows':    [l if l is not None else p for l, p in zip(lows,  prices)],
            }
            history_dates_cache[sym] = dates
            loaded += 1

            # Freshness check — allow up to 4 calendar days back so
            # weekends/the odd market holiday don't falsely flag as stale.
            last_date_str = dates[-1] if dates else None
            is_stale = True
            if last_date_str:
                try:
                    last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date()
                    is_stale = (today - last_date).days > 4
                except Exception:
                    is_stale = True
            if is_stale:
                stale_or_missing.append(sym)
        except Exception:
            stale_or_missing.append(sym)

    missing_entirely = [s for s in ALL_STOCKS if s not in found_syms]
    stale_or_missing.extend(missing_entirely)

    log.info(f"📦 Loaded {loaded} stocks from Supabase stock_full_history "
             f"(0 Yahoo calls) — {len(stale_or_missing)} need a Yahoo fetch "
             f"(missing or stale)")
    return stale_or_missing


async def load_history_at_startup(session: aiohttp.ClientSession):
    """
    Startup replacement for the old 'always re-fetch all ~2385 stocks from
    Yahoo' behavior. Loads everything already stored in Supabase first
    (fast, free), then only hits Yahoo for symbols that are missing or
    whose stored data is stale — normally a small fraction of the universe
    (new IPOs, or symbols that failed every fetch attempt for several
    days running), rather than the whole thing every single restart.
    """
    table_ready = await ensure_full_history_table(session)
    if not table_ready:
        log.error("⏭️  stock_full_history table unavailable — falling back to full "
                   "Yahoo fetch for all stocks this run.")
        await fetch_full_history_for_symbols(session, list(ALL_STOCKS), label="startup-fallback-all")
        return

    stale_or_missing = await load_all_history_from_supabase(session)
    if stale_or_missing:
        await fetch_full_history_for_symbols(session, stale_or_missing, label="startup-backfill")


def merge_incremental_days(existing_dates: list, existing_prices: list, existing_volumes: list,
                            existing_highs: list, existing_lows: list,
                            fresh: dict, max_days: int = 504) -> dict:
    """
    Append new trailing day(s) from a lightweight incremental Yahoo fetch
    onto an existing full-history series, matching by date so an already-
    stored day isn't duplicated. If the fresh fetch's date matches the
    last stored date, it OVERWRITES that day instead (handles the case
    where we'd previously stored an EOD close and Yahoo later has a
    correction, or where a day stored mid-session gets finalized).
    Trims from the front afterward to keep a rolling ~2yr window.
    """
    dates   = list(existing_dates)
    prices  = list(existing_prices)
    volumes = list(existing_volumes)
    highs   = list(existing_highs)
    lows    = list(existing_lows)

    last_date = dates[-1] if dates else None

    for i, d in enumerate(fresh.get('dates', [])):
        if d is None:
            continue
        p = fresh['prices'][i]
        v = fresh['volumes'][i] if fresh['volumes'][i] is not None else 0
        h = fresh['highs'][i]   if fresh['highs'][i]   is not None else p
        l = fresh['lows'][i]    if fresh['lows'][i]    is not None else p

        if last_date and d <= last_date:
            if d == last_date and dates:
                prices[-1], volumes[-1], highs[-1], lows[-1] = p, v, h, l
            continue  # already have an earlier day than this — skip

        dates.append(d)
        prices.append(p)
        volumes.append(v)
        highs.append(h)
        lows.append(l)
        last_date = d

    if len(dates) > max_days:
        dates   = dates[-max_days:]
        prices  = prices[-max_days:]
        volumes = volumes[-max_days:]
        highs   = highs[-max_days:]
        lows    = lows[-max_days:]

    return {'dates': dates, 'prices': prices, 'volumes': volumes, 'highs': highs, 'lows': lows}


async def incremental_eod_update(session: aiohttp.ClientSession):
    """
    Once-per-day EOD task: instead of re-pulling the full 2-year history
    from Yahoo for all ~2385 stocks (slow, and the main source of Yahoo
    rate-limiting), fetch just a small recent window (range=10d) per
    stock and merge the new day(s) into what's already stored — a much
    lighter request that still keeps the rolling 2yr window current.
    Stocks with no existing history yet (new IPOs, or ones that never
    successfully backfilled) fall back to a full fetch instead.
    """
    table_ready = await ensure_full_history_table(session)
    if not table_ready:
        log.error("⏭️  Skipping EOD history update — table still unavailable.")
        return

    has_history  = [s for s in ALL_STOCKS if s in historical_cache and s in history_dates_cache
                    and len(historical_cache[s].get('prices', [])) >= 100]
    needs_full   = [s for s in ALL_STOCKS if s not in has_history]

    total = len(has_history)
    log.info(f"📥 EOD incremental update: {total} stocks (light fetch), "
             f"{len(needs_full)} need a full fetch first…")

    if needs_full:
        await fetch_full_history_for_symbols(session, needs_full, label="eod-backfill")

    sem = asyncio.Semaphore(20)
    rows: list = []
    done = 0
    failed = 0
    failed_syms: list = []
    lock = asyncio.Lock()

    async def fetch_one(sym):
        nonlocal done, failed
        async with sem:
            data = await fetch_yahoo_full_ohlcv(session, sym, range_period="10d", min_points=1)
            await asyncio.sleep(0.02)
        async with lock:
            if data:
                merged = merge_incremental_days(
                    history_dates_cache.get(sym, []),
                    historical_cache.get(sym, {}).get('prices', []),
                    historical_cache.get(sym, {}).get('volumes', []),
                    historical_cache.get(sym, {}).get('highs', []),
                    historical_cache.get(sym, {}).get('lows', []),
                    data,
                )
                historical_cache[sym] = {
                    'prices':  merged['prices'],
                    'volumes': merged['volumes'],
                    'highs':   merged['highs'],
                    'lows':    merged['lows'],
                }
                history_dates_cache[sym] = merged['dates']
                rows.append({
                    'sym':        sym,
                    'dates':      json.dumps(merged['dates']),
                    'prices':     json.dumps(merged['prices']),
                    'volumes':    json.dumps(merged['volumes']),
                    'highs':      json.dumps(merged['highs']),
                    'lows':       json.dumps(merged['lows']),
                    'days_count': len(merged['prices']),
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                })
                done += 1
            else:
                failed += 1
                failed_syms.append(sym)
            seen = done + failed
            if seen % 200 == 0 or seen == total:
                log.info(f"  …{seen}/{total} incremental-updated ({done} ok, {failed} failed)")
            if len(rows) >= 200:
                batch, rows[:] = rows[:], []
                await save_full_history_batch_to_db(session, batch)

    await asyncio.gather(*[fetch_one(sym) for sym in has_history])

    if failed_syms:
        retry_list = failed_syms[:]
        failed_syms = []
        log.info(f"🔁 Retrying {len(retry_list)} stocks that failed the incremental fetch…")
        await asyncio.sleep(2)
        await asyncio.gather(*[fetch_one(sym) for sym in retry_list])
        recovered = len(retry_list) - len(failed_syms)
        failed = len(failed_syms)  # authoritative count — see comment in fetch_full_history_for_symbols
        log.info(f"🔁 Retry pass complete: {recovered} recovered, {len(failed_syms)} still failing")

    if rows:
        await save_full_history_batch_to_db(session, rows)

    log.info(f"✅ EOD incremental update complete: {done} ok, {failed} failed out of {total}")


async def load_nifty_cache(session: aiohttp.ClientSession):
    """Fetch Nifty 50 daily close history needed for TradingView-style RS calculation."""
    global nifty_cache
    log.info("Fetching Nifty 50 historical data for TV-style RS calc…")
    to   = datetime.now(IST).strftime('%Y-%m-%d')
    from_= (datetime.now(IST) - timedelta(days=550)).strftime('%Y-%m-%d')
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
            fresh_prices = [c[4] for c in candles]

            # Merge with DB seed (2020-2025) — no overlap
            # DB seed ends Dec 2025, Upstox starts ~Jul 2025 (371 days back)
            # Overlap ~125 days. Solution: take DB base + Upstox tail only
            db_prices = await load_index_history_from_db(session, "Nifty 50")
            if db_prices and len(db_prices) > len(fresh_prices):
                # DB has more history (seed). Replace last N days with fresh.
                # This avoids overlap: base = everything before Upstox window
                base = db_prices[:-len(fresh_prices)]
                merged = base + fresh_prices
                log.info(f"✅ Nifty 50: {len(merged)}d total (seed:{len(base)}d + fresh:{len(fresh_prices)}d, no overlap)")
            else:
                merged = fresh_prices
                log.info(f"✅ Nifty 50: {len(merged)}d from Upstox only")

            nifty_cache = {'prices': merged, 'volumes': [c[5] for c in candles]}
            # Save back to DB for next restart
            await save_index_history_to_db(session, "Nifty 50", merged)
    except Exception as e:
        log.warning(f"Nifty cache load failed: {e}")

instrument_key_map: dict = {} # sym -> full instrument key (e.g. NSE_EQ|INE002A01018)

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
    failed_syms = []
    for i in range(0, len(ALL_STOCKS), BATCH):
        batch = ALL_STOCKS[i:i+BATCH]
        results = await asyncio.gather(*[
            fetch_historical(session, sym, instrument_key_map.get(sym, f"NSE_EQ|{sym}"))
            for sym in batch
        ])
        for sym, data in zip(batch, results):
            if data:
                historical_cache[sym] = data
                loaded += 1
            else:
                failed_syms.append(sym)
        await asyncio.sleep(0.5)
        if (i // BATCH) % 10 == 0:
            log.info(f"  Loaded {loaded}/{len(ALL_STOCKS)} stocks…")

    # Retry failed stocks with BSE exchange key
    if failed_syms:
        log.info(f"  Retrying {len(failed_syms)} failed stocks with BSE keys…")
        retry_loaded = 0
        for i in range(0, len(failed_syms), BATCH):
            batch = failed_syms[i:i+BATCH]
            results = await asyncio.gather(*[
                fetch_historical(session, sym, f"BSE_EQ|{sym}")
                for sym in batch
            ])
            for sym, data in zip(batch, results):
                if data:
                    historical_cache[sym] = data
                    loaded += 1
                    retry_loaded += 1
            await asyncio.sleep(0.5)
        log.info(f"  BSE retry: {retry_loaded} additional stocks loaded")

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

    # Live Nifty 50 price — needed so RS-TV can react intraday instead of
    # only updating at EOD (historical_cache's Nifty series only refreshes
    # once at startup + once daily, same as every stock's own history).
    global _live_nifty_debug_count
    live_nifty_price = None
    try:
        nifty_live_raw = await fetch_bulk_ohlc(session, [NIFTY_INSTRUMENT_KEY])
        debug_nifty = _live_nifty_debug_count < 5
        if debug_nifty:
            _live_nifty_debug_count += 1
            log.info(f"  🔍 Live Nifty fetch raw keys: {list(nifty_live_raw.keys())}")
        for key_fmt in ("NSE_INDEX:Nifty 50", "NSE_INDEX:NIFTY 50", "NSE_INDEX|Nifty 50"):
            if key_fmt in nifty_live_raw:
                live_nifty_price = nifty_live_raw[key_fmt].get('last_price')
                if debug_nifty:
                    log.info(f"  🔍 Live Nifty price resolved via key '{key_fmt}': {live_nifty_price}")
                break
    except Exception as e:
        log.warning(f"Live Nifty fetch failed: {e}")

    # Live synthetic Midcap 150 / Smallcap 250 values — same simple-average
    # method as build_synthetic_index (the historical version), just using
    # this scan's live prices instead of historical closes. Lets RS-TV's
    # Midcap/Smallcap-benchmarked variants also react intraday, not just
    # the Nifty-benchmarked one.
    def _live_synthetic_price(symbols, min_stocks=20):
        vals = [live_data[s]['last_price'] for s in symbols
                if s in live_data and live_data[s].get('last_price')]
        return (sum(vals) / len(vals)) if len(vals) >= min_stocks else None

    live_midcap_price   = _live_synthetic_price(MIDCAP)
    live_smallcap_price = _live_synthetic_price(SMALLCAP)

    if live_data:
        sample = list(live_data.keys())[:3]
        log.info(f"  Sample OHLC keys: {[f'NSE_EQ:{s}' for s in sample]}")

    log.info(f"  Live prices: {len(live_data)} stocks")

    # Step 2: Only reload full 15-month history once per day, after market
    # close (batch_eod) — this bakes in today's now-final candle as the new
    # baseline for tomorrow. During the day, 'live' scans reuse the cache
    # as-is and just overlay live_data for display, so no re-fetch needed.
    # 'batch_morning' does NOT reload here — the startup sequence already
    # loaded history once before any scan runs, re-loading again on every
    # batch_morning-tagged cycle was wasted API calls and the root cause of
    # repeated multi-minute "stalls" that looked like the live data was
    # not updating.
    global last_eod_refresh_date
    today_ist_check = datetime.now(IST).strftime('%Y-%m-%d')
    is_first_eod_today = (scan_type == 'batch_eod' and last_eod_refresh_date != today_ist_check)

    if scan_type == 'batch_eod':
        if not is_first_eod_today:
            log.info(f"  End-of-day refresh already done today ({today_ist_check}) — skipping "
                     f"(scan loop keeps tagging cycles 'batch_eod' for as long as the "
                     f"market stays closed, so this must be a once-per-day guard).")
        else:
            log.info("  End-of-day scan — incrementally updating Yahoo history to bake in today's final close…")
            # Lightweight incremental update (small per-stock fetch + merge)
            # instead of re-pulling the full 2yr history for every stock —
            # much less Yahoo load, same result (today's close gets added).
            await incremental_eod_update(session)
            await load_nifty_cache(session)
            await load_index_cache(session)
            last_eod_refresh_date = today_ist_check
            # Rebuild synthetic indices with fresh EOD data
            global midcap_cache, smallcap_cache
            fresh_mid = build_synthetic_index(list(MIDCAP),   historical_cache, min_stocks=50)
            fresh_sml = build_synthetic_index(list(SMALLCAP), historical_cache, min_stocks=80)
            db_mid = await load_index_history_from_db(session, "Midcap 150")
            db_sml = await load_index_history_from_db(session, "Smallcap 250")
            def merge_p(db, fresh):
                fp = fresh.get('prices', [])
                return db[:-len(fp)] + fp if db and len(db) > len(fp) else fp
            midcap_cache   = {'prices': merge_p(db_mid, fresh_mid)}
            smallcap_cache = {'prices': merge_p(db_sml, fresh_sml)}
            if midcap_cache['prices']:   await save_index_history_to_db(session, "Midcap 150",   midcap_cache['prices'])
            if smallcap_cache['prices']: await save_index_history_to_db(session, "Smallcap 250", smallcap_cache['prices'])
            log.info(f"  Indices rebuilt: Mid={len(midcap_cache['prices'])}d Sml={len(smallcap_cache['prices'])}d")

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

    # Build one synthetic price index per sector (average of that sector's
    # member stocks' own price histories) so sector-relative RS can use the
    # SAME TV-style, self-normalized-vs-benchmark method as Nifty/Midcap/
    # Smallcap, instead of a totally different percentile-rank scale that
    # doesn't match the Pine Script numbers at all.
    sym_to_sector: dict = {}
    for s in stocks_with_hist:
        sym_to_sector[s['sym']] = get_sector(s['sym'])

    sector_index_prices: dict = {}
    for sector_name, sector_syms in SECTOR_MAP.items():
        idx = build_synthetic_index(list(sector_syms), historical_cache, min_stocks=5)
        if idx.get('prices'):
            sector_index_prices[sector_name] = idx['prices']

    log.info(f"  Built {len(sector_index_prices)} sector benchmark indices for TV-style sector RS")

    # Step 5: Build full stock records
    log.info(f"  Building per-stock records (RS/PP/squeeze/VCP) for {len(stocks_with_hist)} stocks…")

    # Fetch fundamentals only once per day at EOD — they change quarterly,
    # no point fetching every ~60-90s for as long as the market stays closed.
    if is_first_eod_today:
        all_syms = [s['sym'] for s in stocks_with_hist]
        await load_fundamentals_batch(session, all_syms)

    processed = []
    for loop_idx, s in enumerate(stocks_with_hist):
        sym = s['sym']
        prices  = s['prices']
        volumes = s['volumes']
        # Safety: align prices and volumes to same length
        if volumes and len(volumes) != len(prices):
            min_len = min(len(prices), len(volumes))
            prices  = prices[-min_len:]
            volumes = volumes[-min_len:]
        elif not volumes:
            volumes = [0] * len(prices)
        n = len(prices)

        # Yield control back to the event loop periodically. Without this,
        # the synchronous CPU-bound work below (especially squeeze/VCP math)
        # across ~2400 stocks can block the event loop for minutes straight,
        # freezing heartbeats, timeouts, and Railway health checks.
        if loop_idx % 50 == 0:
            await asyncio.sleep(0)

        # RS-TV — TradingView / Lakshmi Mata Pine Script formula.
        # This is now the ONE methodology used everywhere: the main RS
        # badge, the 15-day sparkline/trend, and sector-relative RS all
        # derive from the same self-normalized-vs-benchmark calculation —
        # previously the main badge/sparkline used a totally different
        # IBD-style percentile-rank scale that didn't match the Pine
        # Script numbers at all (e.g. showing 96 while RS-TV showed 72).
        nifty_prices    = nifty_cache.get('prices', [])
        midcap_prices   = midcap_cache.get('prices', [])   or nifty_prices
        smallcap_prices = smallcap_cache.get('prices', []) or nifty_prices

        # Compute the raw series ONCE per stock, reuse for current value,
        # 15-day history, and (via debug block below) diagnostics — avoids
        # recomputing the same O(n) series 2-3x per stock like before.
        tv_raw_series  = calc_raw_rs_series(prices, nifty_prices)     if nifty_prices    else []
        mid_raw_series = calc_raw_rs_series(prices, midcap_prices)    if midcap_prices   else []
        sml_raw_series = calc_raw_rs_series(prices, smallcap_prices)  if smallcap_prices else []
        raw_tv  = normalize_rs(tv_raw_series)  if tv_raw_series  else None
        raw_mid = normalize_rs(mid_raw_series) if mid_raw_series else None
        raw_sml = normalize_rs(sml_raw_series) if sml_raw_series else None

        # Make today's RS-TV (and Mid/Smallcap-benchmarked RS) live instead
        # of frozen at yesterday's close. historical_cache only refreshes
        # at startup + once daily at EOD, so during live market hours the
        # values above reflect YESTERDAY's strength, not today's. If we
        # have live prices for both this stock and the benchmark, compute
        # a live "today" raw RS value and normalize it against the SAME
        # historical hi/lo window each benchmark already has.
        _live_for_rs = live_data.get(sym, {})
        _live_price_for_rs = _live_for_rs.get('last_price', 0)
        _dates_for_rs = history_dates_cache.get(sym, [])
        _today_str_for_rs = datetime.now(IST).strftime('%Y-%m-%d')
        _prices_already_today = bool(_dates_for_rs) and _dates_for_rs[-1] == _today_str_for_rs

        def _live_normalized(bench_prices, bench_raw_series, live_bench_price):
            if _prices_already_today or not _live_price_for_rs or not live_bench_price or not bench_raw_series:
                return None
            live_raw = calc_live_raw_rs_today(prices, bench_prices, _live_price_for_rs, live_bench_price)
            if live_raw is None:
                return None
            window = [v for v in bench_raw_series[-300:] if v is not None][-252:]
            if not window:
                return None
            hi, lo = max(window), min(window)
            return 50 if hi == lo else max(1, min(99, round(((live_raw - lo) / (hi - lo)) * 98 + 1)))

        _live_tv  = _live_normalized(nifty_prices,    tv_raw_series,  live_nifty_price)
        _live_mid = _live_normalized(midcap_prices,   mid_raw_series, live_midcap_price)
        _live_sml = _live_normalized(smallcap_prices, sml_raw_series, live_smallcap_price)
        if _live_tv  is not None: raw_tv  = _live_tv
        if _live_mid is not None: raw_mid = _live_mid
        if _live_sml is not None: raw_sml = _live_sml

        rs = raw_tv if raw_tv is not None else 0  # main badge now TV-style, matches RS-TV exactly
        hist = tv_history_from_raw(tv_raw_series, days=15) if tv_raw_series else [None] * 15
        if hist and raw_tv is not None:
            hist[-1] = raw_tv  # keep "today" dot consistent with the live-updated main badge
        trend_data = rs_slope(hist)

        # Debug: log RS details for GRSE every scan (previous guard used
        # loop_idx — GRSE's position within this scan's stock list — which
        # almost never lands under 5 out of ~2300+ stocks, so this never
        # actually fired before). Also checks for a discontinuity at the
        # seed/fresh Nifty data stitch point, a likely source of RS-TV drift.
        if sym in ('GRSE', 'RRKABEL'):
            valid_pts = [v for v in tv_raw_series if v is not None]
            window = [v for v in tv_raw_series[-300:] if v is not None][-252:]
            hi = max(window) if window else None
            lo = min(window) if window else None
            current = tv_raw_series[-1] if tv_raw_series else None
            stitch_idx = len(nifty_prices) - 371  # fresh Upstox data starts ~371d back
            stitch_note = ""
            if 0 < stitch_idx < len(nifty_prices) - 1:
                stitch_note = (f", nifty@stitch-1={nifty_prices[stitch_idx-1]:.1f}, "
                                f"nifty@stitch={nifty_prices[stitch_idx]:.1f}")
            log.info(f"  🔍 {sym}: stock_days={len(prices)}, nifty_days={len(nifty_prices)}, "
                     f"rawRS_valid_points={len(valid_pts)}, current_rawRS={current}, "
                     f"norm_window_hi={hi}, norm_window_lo={lo}, rs_tv={raw_tv}{stitch_note}")

        # Sector-relative RS — TV-style vs a synthetic sector benchmark
        # index (same method as Nifty/Midcap/Smallcap), replacing the old
        # percentile-rank-vs-sector-peers approach for consistency.
        my_sector    = sym_to_sector.get(sym, 'Other')
        sector_bench = sector_index_prices.get(my_sector)
        rs_sector = normalize_rs(calc_raw_rs_series(prices, sector_bench)) if sector_bench else None

        # RVOL — relative volume
        rvol_data = calc_rvol(volumes)

        # RS Line vs Nifty
        rs_line_data = calc_rs_line(prices, nifty_cache.get('prices', []))

        # Stage 2 New Entry
        is_s2_new = calc_stage2_new_entry(prices)

        # Live price — use sym-based lookup
        # IMPORTANT: historical_cache prices are NEVER mutated for RS/PP/etc,
        # which always read the raw array. But for chg% specifically we need
        # to know whether prices[-1] represents YESTERDAY's close (live
        # market hours — compare it against today's live_price) or TODAY's
        # close (after the EOD incremental update has already baked today's
        # close in) — in the latter case, live_price also reflects today, so
        # comparing it against prices[-1] (also today) always gives 0.00%.
        # Use history_dates_cache to tell which case we're in.
        live = live_data.get(sym, {})
        live_price = live.get('last_price', 0)

        dates_for_sym = history_dates_cache.get(sym, [])
        today_str = datetime.now(IST).strftime('%Y-%m-%d')
        prices_last_is_today = bool(dates_for_sym) and dates_for_sym[-1] == today_str

        if prices_last_is_today and n > 1:
            true_prev_close = prices[n-2]  # prices[-1] is today — baseline is the day before
        else:
            true_prev_close = prices[n-1]  # prices[-1] is still yesterday — baseline as-is

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

        # Weekly/monthly % change per stock — needed for sector breadth
        # (% of stocks advancing) at each timeframe, not just daily.
        chg_w = round((last - prices[n-6])  / prices[n-6]  * 100, 2) if n >= 6  and prices[n-6]  else None
        chg_m = round((last - prices[n-22]) / prices[n-22] * 100, 2) if n >= 22 and prices[n-22] else None

        if sym == 'RRKABEL':
            log.info(f"  🔍 RRKABEL chg-calc: n={n}, dates_last3={dates_for_sym[-3:] if dates_for_sym else None}, "
                     f"today_str={today_str}, prices_last_is_today={prices_last_is_today}, "
                     f"prices_last3={prices[-3:]}, true_prev_close={true_prev_close}, "
                     f"live_price={live_price}, last={last}, prev={prev}, chg={chg}")

        # PP — needs to trigger intraday, not just at EOD. detect_pp reads
        # only the static historical prices/volumes, which during live
        # market hours still end on YESTERDAY's close (today's close only
        # lands there after the EOD update) — so PP would otherwise lag a
        # full day behind. Build a live-augmented series with today's live
        # price/volume appended as an extra trailing point when we're still
        # mid-session; once prices[-1] already IS today (post-EOD), no
        # augmentation is needed since today's real close is already there.
        if prices_last_is_today or not (live_price and live_price > 0):
            rt_prices, rt_volumes = prices, volumes
        else:
            rt_prices  = prices + [live_price]
            rt_volumes = volumes + [vol]
        pp = detect_pp(rt_prices, rt_volumes)

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

        # Squeeze (BB + Keltner) and VCP
        highs_arr = s.get('highs', prices)
        lows_arr  = s.get('lows', prices)
        squeeze = detect_bb_squeeze(prices, highs_arr, lows_arr)
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
            'chg_w_pct':      chg_w,
            'chg_m_pct':      chg_m,
            'volume':         int(vol),
            'rs':             rs,
            'rs_tv':          raw_tv,
            'rs_midcap':      raw_mid,
            'rs_smallcap':    raw_sml,
            'rvol':           rvol_data.get('rvol'),
            'vol_signal':     rvol_data.get('vol_signal'),
            'rs_line_new_high': rs_line_data.get('rs_line_new_high', False),
            'rs_line_trend':  rs_line_data.get('rs_line_trend', 'flat'),
            'rs_line_value':  rs_line_data.get('rs_line_value'),
            'is_s2_new_entry': is_s2_new,       # TradingView / Lakshmi Mata Pine Script RS
            'rs_nifty50':     None,        # deprecated — use rs_tv
            'rs_microcap':    None,
            'rs_sector':      rs_sector,
            'rs_raw':         round(tv_raw_series[-1], 6) if tv_raw_series and tv_raw_series[-1] is not None else None,
            'rs_trend':       trend_data['trend'],
            'rs_slope':       trend_data['slope'],
            'rs_hist':        hist,
            'is_pp':          pp['is_pp'],
            'pp_count_10d':   pp['pp_count_10d'],
            'pp_hist':        pp['pp_hist'],
            'pp_vol_ratio':   pp['vol_ratio'],
            'ma10':           round(pp['ma10'], 2) if pp['ma10'] else None,
            'ma50':           round(pp['ma50'], 2) if pp['ma50'] else None,
            'is_hy':          hy_pct >= 95 and chg > 0,
            'hy_pct':         hy_pct,
            'is_ht':          ht_pct >= 95 and chg > 0,
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
            'in_squeeze':     squeeze['in_squeeze'],
            'squeeze_fired':  squeeze['squeeze_fired'],
            'bb_width_pct':   squeeze['bb_width_pct'],
            'squeeze_days':   squeeze['squeeze_days'],
            'is_vcp':         vcp['is_vcp'],
            'vcp_stage':      vcp['vcp_stage'],
            'vcp_fired':      vcp['vcp_fired'],
            'vcp_contractions': json.dumps(vcp['contractions']),
            'sector':         get_sector(sym),
            'in_nifty50':     sym in NIFTY50,
            'in_midcap':      sym in MIDCAP,
            'in_smallcap':    sym in SMALLCAP,
            'in_microcap':    sym in MICROCAP,
            # Fundamentals from Upstox API (Screener.in fallback), cached weekly
            'market_cap':     fundamentals_cache.get(sym, {}).get('market_cap'),
            'pe':             fundamentals_cache.get(sym, {}).get('pe'),
            'roe':            fundamentals_cache.get(sym, {}).get('roe'),
            'eps':            fundamentals_cache.get(sym, {}).get('eps'),
            'debt_eq':        fundamentals_cache.get(sym, {}).get('debt_eq'),
            'promoter':       fundamentals_cache.get(sym, {}).get('promoter'),
            # Growth/trend fundamentals — earnings acceleration (CANSLIM-style)
            # and smart-money holding trends, not just static snapshots
            'eps_qoq':            fundamentals_cache.get(sym, {}).get('eps_qoq'),
            'eps_yoy':            fundamentals_cache.get(sym, {}).get('eps_yoy'),
            'sales_qoq':          fundamentals_cache.get(sym, {}).get('sales_qoq'),
            'sales_yoy':          fundamentals_cache.get(sym, {}).get('sales_yoy'),
            'opm_pct':            fundamentals_cache.get(sym, {}).get('opm_pct'),
            'opm_trend':          fundamentals_cache.get(sym, {}).get('opm_trend'),
            'eps_growth_streak':  fundamentals_cache.get(sym, {}).get('eps_growth_streak'),
            'fii_pct':            fundamentals_cache.get(sym, {}).get('fii_pct'),
            'fii_trend':          fundamentals_cache.get(sym, {}).get('fii_trend'),
            'dii_pct':            fundamentals_cache.get(sym, {}).get('dii_pct'),
            'dii_trend':          fundamentals_cache.get(sym, {}).get('dii_trend'),
            'promoter_trend':     fundamentals_cache.get(sym, {}).get('promoter_trend'),
            'peg_ratio':          fundamentals_cache.get(sym, {}).get('peg_ratio'),
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

        # RS-TV using Nifty as benchmark — meaningless for Nifty 50 itself
        # (comparing an index against itself trivially gives 0 relative
        # performance every day, which normalizes to a degenerate 50).
        # Showing a plain "50" there looks like a real median reading
        # rather than "not applicable", so use None (renders as "—") instead.
        if idx_name == 'Nifty 50':
            rs_tv_idx = None
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

        # Stage logic for index — uses MA10-vs-MA30 (a stable, multi-day
        # trend-confirmation signal, same as the up/down arrows already
        # shown in the UI) instead of today's single-day price change.
        # The previous version gated Stage 2/3/4 on chg_d >= 0 / <= 0,
        # meaning a single red day in an established uptrend (rising
        # MA10, rising MA30, strong 1W/1M/3M returns) would flip the
        # whole index down to "S1 Base" — which is exactly why so many
        # genuinely-uptrending indices were all showing "Base" together
        # on an ordinary red day for the broader market.
        if ma30 and last > ma30 and ma10 and ma10 >= ma30:
            if pct_from_high >= -5:
                stage = 3
            else:
                stage = 2
        elif ma30 and last < ma30 and ma10 and ma10 <= ma30:
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
        adv_d = adv_w = adv_m = None
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

            # Breadth — % of this index's constituent stocks advancing at
            # each timeframe (same concept as sector breadth). Two indices
            # both "up 1% today" look very different if one has 90% of
            # members participating vs one carried by a handful of names.
            def _adv_pct(field):
                vals = [m.get(field) for m in members if m.get(field) is not None]
                return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2) if vals else None
            adv_d = _adv_pct('chg_pct')
            adv_w = _adv_pct('chg_w_pct')
            adv_m = _adv_pct('chg_m_pct')
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
            'advances_d':    adv_d,
            'advances_w':    adv_w,
            'advances_m':    adv_m,
            'top_stocks':    json.dumps(top_stocks),
            'bot_stocks':    json.dumps(bot_stocks),
            'last_updated':  now_ist.isoformat(),
        })

    # Rank each index's daily/weekly/monthly performance against all other
    # indices (1 = best performer for that timeframe). Needs a second pass
    # since ranking requires seeing every index's chg value first.
    for field, rank_field in (('chg_d', 'rank_d'), ('chg_w', 'rank_w'), ('chg_m', 'rank_m')):
        ordered = sorted(index_rows, key=lambda r: r[field], reverse=True)
        for rank, row in enumerate(ordered, start=1):
            row[rank_field] = rank
    total_indices = len(index_rows)
    for row in index_rows:
        row['total_indices'] = total_indices

    # Week-over-week rank movement — "was #7 a week ago, now #3" is more
    # useful than a bare rank on its own. Loads each index's existing
    # rank_w_history from Supabase, appends today's rank_w once per day
    # (not every ~60-90s scan — a rolling weekly comparison shouldn't
    # jitter intraday), and computes the change vs the oldest entry in
    # an 8-day rolling window (roughly a week of trading days).
    prev_history = await load_index_rank_history(session)
    for row in index_rows:
        hist = list(prev_history.get(row['name'], []))
        if is_first_eod_today:
            hist.append(row['rank_w'])
            hist = hist[-8:]
        elif not hist:
            # No history yet at all (first run ever for this index) —
            # seed it with today's value so a change becomes computable
            # starting next week, rather than staying empty forever.
            hist = [row['rank_w']]
        row['rank_w_history'] = json.dumps(hist)
        row['rank_w_change'] = (hist[0] - hist[-1]) if len(hist) >= 2 else None
        # Positive = rank number went down = moved UP the standings (good).

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
    if is_first_eod_today:
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

        # Step 2a: Ensure all required DB columns exist
        await ensure_db_columns(session)
        # Step 2b: Load Nifty 50 + all index histories
        await load_nifty_cache(session)
        await load_index_cache(session)
        # Step 2c: Seed + load history
        await seed_index_history_if_needed(session)
        # Small wait to ensure DB write completes
        await asyncio.sleep(2)
        # Step 2d: Try external sources for Midcap/Smallcap only
        await load_full_history_once(session)

        # Step 3: Load historical data cache at startup
        log.info("Loading historical data cache at startup…")
        await load_historical_cache(session)

        # Step 3b: Build synthetic Midcap/Smallcap indices AFTER history is loaded
        global midcap_cache, smallcap_cache

        # Build fresh from constituents
        fresh_mid = build_synthetic_index(list(MIDCAP),   historical_cache, min_stocks=50)
        fresh_sml = build_synthetic_index(list(SMALLCAP), historical_cache, min_stocks=80)

        # Merge with accumulated DB history
        db_mid = await load_index_history_from_db(session, "Midcap 150")
        db_sml = await load_index_history_from_db(session, "Smallcap 250")

        def merge_prices(db, fresh):
            fp = fresh.get('prices', [])
            if db and len(db) > len(fp):
                return db[:-len(fp)] + fp
            return fp

        mid_prices = merge_prices(db_mid, fresh_mid)
        sml_prices = merge_prices(db_sml, fresh_sml)

        midcap_cache   = {'prices': mid_prices}
        smallcap_cache = {'prices': sml_prices}

        # Save back to DB
        if mid_prices: await save_index_history_to_db(session, "Midcap 150",   mid_prices)
        if sml_prices: await save_index_history_to_db(session, "Smallcap 250", sml_prices)

        log.info(f"✅ Midcap index: {len(mid_prices)}d (DB had {len(db_mid)}d)")
        log.info(f"✅ Smallcap index: {len(sml_prices)}d (DB had {len(db_sml)}d)")

        # Step 3c: Load full 2yr history at startup — from Supabase first
        # (fast, zero Yahoo calls), falling back to Yahoo only for symbols
        # that are missing or stale. Previously this always re-fetched all
        # ~2385 stocks from Yahoo on every single restart, which was slow
        # and the main source of Yahoo rate-limit failures.
        FULL_HISTORY_TIMEOUT = 1800  # 30 min ceiling if a large backfill is needed
        try:
            await asyncio.wait_for(load_history_at_startup(session), timeout=FULL_HISTORY_TIMEOUT)
        except asyncio.TimeoutError:
            log.error(f"⏱ Startup history load exceeded {FULL_HISTORY_TIMEOUT}s — continuing with partial data")
        except Exception as e:
            import traceback
            log.error(f"Startup history load error: {e}\n{traceback.format_exc()}")

        # Step 3d: Load fundamentals — Supabase first, then a background
        # scrape for anything missing/stale (not blocking, since a full
        # scrape can take 8-15+ minutes and fundamentals aren't as
        # time-critical as price data).
        try:
            await load_fundamentals_at_startup(session)
        except Exception as e:
            import traceback
            log.error(f"Startup fundamentals load error: {e}\n{traceback.format_exc()}")

        log.info("✅ Proceeding to initial scan…")


        # Step 4: Run initial scan
        SCAN_TIMEOUT = 900
        ist_now_initial = datetime.now(IST)
        if is_market_open():
            initial_scan_type = 'live'
        elif ist_now_initial.hour < 9 or (ist_now_initial.hour == 9 and ist_now_initial.minute < 15):
            initial_scan_type = 'batch_morning'
        else:
            initial_scan_type = 'batch_eod'
        log.info(f"Initial scan type detected: {initial_scan_type} (current time {ist_now_initial.strftime('%H:%M IST')})")
        try:
            await asyncio.wait_for(run_scan(session, initial_scan_type), timeout=SCAN_TIMEOUT)
        except asyncio.TimeoutError:
            log.error(f"⏱ Initial scan exceeded {SCAN_TIMEOUT}s timeout — aborting and continuing to main loop")

        # NOTE: the legacy per-stock Yahoo "extend short history" background
        # task is no longer needed here — load_history_at_startup()
        # (called earlier, before the initial scan) already gives every
        # stock full 2yr Yahoo OHLCV directly into historical_cache, which
        # is a strict superset of what extend_stock_history_from_yahoo did.

        last_scan = time.time()
        scan_count = 0

        while True:
            try:
                now = time.time()
                elapsed = now - last_scan

                if elapsed >= UPDATE_INTERVAL:
                    scan_type = 'live' if is_market_open() else 'batch_eod'
                    try:
                        await asyncio.wait_for(run_scan(session, scan_type), timeout=SCAN_TIMEOUT)
                        scan_count += 1
                    except asyncio.TimeoutError:
                        log.error(f"⏱ Scan exceeded {SCAN_TIMEOUT}s timeout — skipping this cycle")
                    except Exception as e:
                        # Any other exception (bug, bad data, etc.) must NOT
                        # be allowed to skip past last_scan's update below —
                        # otherwise elapsed stays >= UPDATE_INTERVAL forever
                        # and this becomes an instant, zero-delay retry loop
                        # (which is exactly what happened once already: a
                        # NameError crashed every scan attempt back-to-back,
                        # multiple times per second, for as long as the bug
                        # was live).
                        import traceback
                        log.error(f"Scan failed with unexpected error: {e}\n{traceback.format_exc()}")
                    last_scan = time.time()

                await asyncio.sleep(5)  # check every 5 seconds

            except KeyboardInterrupt:
                log.info("Shutting down…")
                break
            except Exception as e:
                import traceback
                log.error(f"Loop error: {e}\n{traceback.format_exc()}")
                # Defense in depth: even if something outside run_scan itself
                # throws (e.g. is_market_open(), the 5s sleep call site, etc.),
                # back off before retrying instead of spinning immediately.
                await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
