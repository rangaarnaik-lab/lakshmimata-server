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
    """
    n = min(len(prices), len(bench_prices))
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


def normalize_rs(raw_series: list) -> Optional[float]:
    """Return raw score for cross-sectional ranking — done after all stocks computed."""
    if not raw_series:
        return None
    valid = [v for v in raw_series if v is not None]
    if not valid:
        return None
    return valid[-1]  # today's rawRS — ranked cross-sectionally in run_scan()


def calc_rs_tv_normalized(prices: list, bench_prices: list, end_idx: int = None) -> Optional[int]:
    """Convenience wrapper — computes full series then normalizes."""
    raw = calc_raw_rs_series(prices, bench_prices)
    if end_idx is not None:
        raw = raw[:end_idx+1]
    return normalize_rs(raw)

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


async def fetch_bulk_ohlc(session: aiohttp.ClientSession, instrument_keys: list) -> dict:
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


historical_cache: dict = {}   # sym -> {prices, volumes}
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
    }
    for name, ikey in INDEX_TRACKER.items():
        # Try primary key, then alternatives
        keys_to_try = KEY_ALTERNATIVES.get(name, [ikey])
        if ikey not in keys_to_try:
            keys_to_try = [ikey] + keys_to_try
        success = False
        for try_key in keys_to_try:
            encoded = try_key.replace('|', '%7C').replace(' ', '%20')
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
    """Auto-add any missing columns to Supabase stocks table via direct SQL."""
    sql = """
    alter table public.stocks add column if not exists rs_tv int;
    alter table public.stocks add column if not exists rs_midcap int;
    alter table public.stocks add column if not exists rs_smallcap int;
    alter table public.stocks add column if not exists rs_sector int;
    alter table public.stocks add column if not exists rs_nifty50 int;
    alter table public.stocks add column if not exists rs_microcap int;
    """
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    try:
        async with session.post(
            f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
            headers=headers,
            json={"query": sql},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status in (200, 201, 204):
                log.info("✅ DB columns verified/added")
            else:
                # Not critical — columns may already exist, or exec_sql not available
                log.info("✅ DB column check skipped (run SQL manually if rs_tv missing)")
    except Exception as e:
        log.info(f"✅ DB column check skipped: {e}")



async def fetch_stooq_history(session: aiohttp.ClientSession, symbol: str, is_index: bool = False) -> list:
    """
    Fetch 2+ years of daily closes from stooq.com.
    Free, no auth, supports NSE stocks (GRSE.NS) and indices (^NSEI).
    Returns list of closing prices oldest-first.
    """
    ticker = symbol.lower() + ".ns" if not is_index else symbol.lower()
    url = f"https://stooq.com/q/d/l/?s={ticker}&i=d"
    try:
        async with session.get(url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            text = await r.text()
            lines = text.strip().split("\n")
            prices = []
            for line in lines[1:]:  # skip header
                parts = line.split(",")
                if len(parts) >= 5 and parts[4] != "N/D":
                    try:
                        prices.append(float(parts[4]))  # Close price
                    except:
                        pass
            return list(reversed(prices))  # oldest first
    except Exception as e:
        return []


async def load_stooq_index_cache(session: aiohttp.ClientSession):
    """Load Nifty/Midcap/Smallcap index history from stooq for accurate RS calc."""
    global nifty_cache, midcap_cache, smallcap_cache

    stooq_indices = {
        "nifty":    "^NSEI",
        "midcap":   "^CNXMC",   # Nifty Midcap 100 on stooq
        "smallcap": "^CNXSC",   # Nifty Smallcap on stooq
    }

    for name, ticker in stooq_indices.items():
        prices = await fetch_stooq_history(session, ticker, is_index=True)
        if len(prices) >= 252:
            if name == "nifty" and len(prices) > len(nifty_cache.get("prices", [])):
                nifty_cache = {"prices": prices}
                log.info(f"✅ Stooq Nifty 50: {len(prices)} days (extended from Upstox)")
            elif name == "midcap" and len(prices) >= 252:
                midcap_cache = {"prices": prices}
                log.info(f"✅ Stooq Midcap: {len(prices)} days")
            elif name == "smallcap" and len(prices) >= 252:
                smallcap_cache = {"prices": prices}
                log.info(f"✅ Stooq Smallcap: {len(prices)} days")
        else:
            log.warning(f"Stooq {name} ({ticker}): only {len(prices)} days")
        await asyncio.sleep(0.5)

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
        # Rebuild synthetic indices with fresh EOD data
        midcap_cache   = build_synthetic_index(list(MIDCAP),   historical_cache, min_stocks=50)
        smallcap_cache = build_synthetic_index(list(SMALLCAP), historical_cache, min_stocks=80)
        log.info(f"  Synthetic indices rebuilt: Mid={len(midcap_cache.get('prices',[]))}d Sml={len(smallcap_cache.get('prices',[]))}d")

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
        if loop_idx % 50 == 0:
            await asyncio.sleep(0)

        # RS (IBD percentile — kept for sparkline/trend)
        my_raw_val = raw_by_sym.get(sym)
        rs = percentile_rank(raw_vals, my_raw_val) if my_raw_val is not None else 0
        hist = rs_history.get(sym, [])
        trend_data = rs_slope(hist)

        # RS-TV — TradingView / Lakshmi Mata Pine Script formula
        nifty_prices    = nifty_cache.get('prices', [])
        midcap_prices   = midcap_cache.get('prices', [])   or nifty_prices
        smallcap_prices = smallcap_cache.get('prices', []) or nifty_prices

        # Fast single-pass: compute full rawRS series, then normalize
        raw_tv  = normalize_rs(calc_raw_rs_series(prices, nifty_prices))    if nifty_prices    else None
        raw_mid = normalize_rs(calc_raw_rs_series(prices, midcap_prices))   if midcap_prices   else None
        raw_sml = normalize_rs(calc_raw_rs_series(prices, smallcap_prices)) if smallcap_prices else None
        # Sector-relative RS — percentile rank vs same sector peers
        my_raw    = my_raw_val
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
            'volume':         int(vol),
            'rs':             rs,
            'rs_tv':          raw_tv,   # raw score — cross-ranked below
            'rs_mid':         raw_mid,  # raw score — cross-ranked below
            'rs_sml':         raw_sml,  # raw score — cross-ranked below
            'rvol':           rvol_data.get('rvol'),
            'vol_signal':     rvol_data.get('vol_signal'),
            'rs_line_new_high': rs_line_data.get('rs_line_new_high', False),
            'rs_line_trend':  rs_line_data.get('rs_line_trend', 'flat'),
            'rs_line_value':  rs_line_data.get('rs_line_value'),
            'is_s2_new_entry': is_s2_new,       # TradingView / Lakshmi Mata Pine Script RS
            'rs_nifty50':     None,        # deprecated — use rs_tv
            'rs_microcap':    None,
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



    # ── Cross-sectional RS ranking ─────────────────────────────────────────
    # Rank each stock's rawRS against all stocks — gives reliable 1-99 scores
    # even with limited history. Strong stocks rank high, weak stocks rank low.
    def cross_rank(stocks, key_raw, key_out):
        raws = [(s, s[key_raw]) for s in stocks if s.get(key_raw) is not None]
        if not raws: return
        vals = [v for _, v in raws]
        for s, v in raws:
            s[key_out] = min(99, max(1, round((sum(1 for x in vals if x < v) / len(vals)) * 98) + 1))
        for s in stocks:
            s.pop(key_raw, None)

    cross_rank(processed, 'rs_tv',  'rs_tv')
    cross_rank(processed, 'rs_mid', 'rs_midcap')
    cross_rank(processed, 'rs_sml', 'rs_smallcap')
    log.info(f"  RS ranked cross-sectionally: {len(processed)} stocks")

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
        # Step 2c: Extend with stooq 2yr history for accurate RS normalization
        await load_stooq_index_cache(session)

        # Step 3: Load historical data cache at startup
        log.info("Loading historical data cache at startup…")
        await load_historical_cache(session)

        # Step 3b: Build synthetic Midcap/Smallcap indices AFTER history is loaded
        global midcap_cache, smallcap_cache
        midcap_cache   = build_synthetic_index(list(MIDCAP),   historical_cache, min_stocks=50)
        smallcap_cache = build_synthetic_index(list(SMALLCAP), historical_cache, min_stocks=80)
        log.info(f"✅ Synthetic Midcap index: {len(midcap_cache.get('prices',[]))} days from {len([s for s in MIDCAP if s in historical_cache])} stocks")
        log.info(f"✅ Synthetic Smallcap index: {len(smallcap_cache.get('prices',[]))} days from {len([s for s in SMALLCAP if s in historical_cache])} stocks")

        log.info("✅ Proceeding to initial scan…")

        # Step 4: Run initial scan (hard timeout so a stall can't hang the process forever)
        # Detect the correct scan type based on actual time, rather than always
        # forcing 'batch_morning' — if Railway restarts mid-afternoon or after
        # close, the first scan should reflect that correctly.
        SCAN_TIMEOUT = 900  # 15 minutes max — RS self-normalization adds compute time
        ist_now_initial = datetime.now(IST)
        if is_market_open():
            initial_scan_type = 'live'
        elif ist_now_initial.hour >= MARKET_CLOSE_H:
            initial_scan_type = 'batch_eod'
        else:
            initial_scan_type = 'batch_morning'
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
