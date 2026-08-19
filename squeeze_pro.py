#!/usr/bin/env python3
"""John Carter "Squeeze Pro" — the single Python source of truth.

The classic TTM Squeeze answers one question: are the Bollinger Bands inside
the Keltner Channel? Squeeze Pro runs that test against three Keltner widths,
so instead of on/off you get *how hard* price is coiled, and it reads the break
direction off the TTM momentum line rather than guessing:

    inside KC 2.0 only ......... low compression   (Carter plots a black dot)
    inside KC 1.5 .............. mid compression   (the classic squeeze, red)
    inside KC 1.0 .............. high compression  (tightest coil, orange)
    outside all ................ no squeeze        (green)

Dependency-free on purpose so the live scan and one-off backfill scripts can
both import it.

Formulas mirror the frontend exactly — src/scanners/lakshmiProprietary.js
`calcSqueezePro` and the `squeeze` indicator defaults in
src/lib/chartIndicatorPrefs.js. If these drift, the scanner will rank a coil
the chart draws differently.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

SQZ_LENGTH = 20        # BB and Keltner length
SQZ_BB_MULT = 2.0      # Bollinger stddev multiplier
SQZ_KC_HIGH = 1.0      # tightest Keltner — high compression
SQZ_KC_MID = 1.5       # the classic TTM squeeze
SQZ_KC_LOW = 2.0       # widest Keltner — low compression
SQZ_MOM_LENGTH = 20    # TTM momentum length

LEVEL_NONE, LEVEL_LOW, LEVEL_MID, LEVEL_HIGH = 0, 1, 2, 3
LEVEL_NAMES = {LEVEL_NONE: 'none', LEVEL_LOW: 'low', LEVEL_MID: 'mid', LEVEL_HIGH: 'high'}

# Longest coil we bother counting. A streak past this is "very long" either
# way, and the cap keeps the trailing window (and so the work per stock)
# bounded on a 2400-name universe.
MAX_STREAK = 120

EMPTY = {
    'sqz_level': None,
    'sqz_days': 0,
    'sqz_high_days': 0,
    'sqz_mom': None,
    'sqz_mom_slope': None,
    'sqz_bias': None,
    'sqz_fired': False,
    'sqz_fired_dir': None,
    'in_squeeze': False,
    'squeeze_fired': False,
    'squeeze_days': 0,
    'bb_width_pct': None,
}


def _sma_series(vals: list, period: int) -> list:
    out = [None] * len(vals)
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= period:
            run -= vals[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def _stdev_series(vals: list, period: int) -> list:
    """Population stdev, matching Pine's ta.stdev and the frontend."""
    out = [None] * len(vals)
    run = run2 = 0.0
    for i, v in enumerate(vals):
        run += v
        run2 += v * v
        if i >= period:
            drop = vals[i - period]
            run -= drop
            run2 -= drop * drop
        if i >= period - 1:
            mean = run / period
            var = run2 / period - mean * mean
            out[i] = (var if var > 0 else 0.0) ** 0.5
    return out


def _true_range_series(highs: list, lows: list, closes: list) -> list:
    out = [0.0] * len(closes)
    for i in range(len(closes)):
        h, l = highs[i], lows[i]
        if i == 0:
            out[i] = h - l
            continue
        pc = closes[i - 1]
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def _ema_series(vals: list, period: int) -> list:
    out = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(vals[:period]) / period
    out[period - 1] = e
    for i in range(period, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def _rolling_extreme_series(vals: list, period: int, want_max: bool) -> list:
    """O(n) rolling max/min via a monotonic deque."""
    out = [None] * len(vals)
    dq: deque = deque()
    for i, v in enumerate(vals):
        while dq and dq[0] <= i - period:
            dq.popleft()
        if want_max:
            while dq and vals[dq[-1]] <= v:
                dq.pop()
        else:
            while dq and vals[dq[-1]] >= v:
                dq.pop()
        dq.append(i)
        if i >= period - 1:
            out[i] = vals[dq[0]]
    return out


def _linreg_at(src: list, i: int, period: int) -> Optional[float]:
    """Pine ta.linreg(src, period, 0) — the fitted value at bar i."""
    if i < period - 1:
        return None
    sum_x = sum_y = sum_xy = sum_x2 = 0.0
    for k in range(period):
        y = src[i - period + 1 + k]
        if y is None:
            return None
        sum_x += k
        sum_y += y
        sum_xy += k * y
        sum_x2 += k * k
    den = period * sum_x2 - sum_x * sum_x
    if den == 0:
        return None
    b = (period * sum_xy - sum_x * sum_y) / den
    a = (sum_y - b * sum_x) / period
    return a + b * (period - 1)


def compute_squeeze_pro(highs: list, lows: list, closes: list,
                        length: int = SQZ_LENGTH,
                        bb_mult: float = SQZ_BB_MULT,
                        kc_high: float = SQZ_KC_HIGH,
                        kc_mid: float = SQZ_KC_MID,
                        kc_low: float = SQZ_KC_LOW,
                        mom_length: int = SQZ_MOM_LENGTH) -> dict:
    """Squeeze Pro state on the latest bar of the given daily series.

    Returns both the new tier fields and the classic `in_squeeze` /
    `squeeze_fired` / `squeeze_days` / `bb_width_pct` set, derived from the
    same numbers so the scanner can never say "fired" while the dot still
    shows a coil.

    `squeeze_days` keeps its old meaning (bars inside the 1.5 Keltner, i.e.
    mid or tighter) while `sqz_days` counts bars in *any* compression — that
    is the "how long has this been coiling" number.
    """
    n = len(closes) if closes else 0
    if n < length + 5 or not highs or not lows:
        return dict(EMPTY)
    if len(highs) < n or len(lows) < n:
        return dict(EMPTY)

    # Only the tail matters. The EMA/SMA seeds decay long before the bars we
    # report on, and this keeps the per-stock cost flat instead of growing
    # with years of history.
    win = min(n, length + MAX_STREAK + 2 * mom_length + 10)
    c = list(closes[-win:])
    h = list(highs[-win:])
    l = list(lows[-win:])
    m = len(c)

    basis = _sma_series(c, length)
    stdev = _stdev_series(c, length)
    range_kc = _ema_series(_true_range_series(h, l, c), length)

    levels = [LEVEL_NONE] * m
    bb_width_pct = None
    for i in range(length - 1, m):
        if basis[i] is None or stdev[i] is None or range_kc[i] is None:
            continue
        upper_bb = basis[i] + bb_mult * stdev[i]
        lower_bb = basis[i] - bb_mult * stdev[i]
        if i == m - 1 and basis[i]:
            bb_width_pct = round((upper_bb - lower_bb) / basis[i] * 100, 2)
        for level, mult in ((LEVEL_HIGH, kc_high), (LEVEL_MID, kc_mid), (LEVEL_LOW, kc_low)):
            if upper_bb < basis[i] + mult * range_kc[i] and lower_bb > basis[i] - mult * range_kc[i]:
                levels[i] = level
                break

    def streak(min_level: int) -> int:
        count = 0
        for i in range(m - 1, -1, -1):
            if levels[i] >= min_level:
                count += 1
                if count >= MAX_STREAK:
                    break
            else:
                break
        return count

    level_now = levels[-1]
    level_prev = levels[-2] if m >= 2 else LEVEL_NONE
    days_any = streak(LEVEL_LOW)
    days_high = streak(LEVEL_HIGH)
    days_classic = streak(LEVEL_MID)

    # TTM momentum — close against the midpoint of the Donchian mid and the
    # SMA, smoothed by a linear regression. Only the last two bars are needed
    # (value + slope), so the regression runs twice rather than per bar.
    donch_hi = _rolling_extreme_series(h, mom_length, True)
    donch_lo = _rolling_extreme_series(l, mom_length, False)
    sma_mom = _sma_series(c, mom_length)
    src: list = [None] * m
    for i in range(mom_length - 1, m):
        if donch_hi[i] is None or donch_lo[i] is None or sma_mom[i] is None:
            continue
        src[i] = c[i] - ((donch_hi[i] + donch_lo[i]) / 2 + sma_mom[i]) / 2
    mom = _linreg_at(src, m - 1, mom_length)
    mom_prev = _linreg_at(src, m - 2, mom_length) if m >= 2 else None

    bias = None
    if mom is not None:
        bias = 'long' if mom >= 0 else 'short'
    mom_slope = None
    if mom is not None and mom_prev is not None:
        mom_slope = round(mom - mom_prev, 4)

    fired_any = level_now == LEVEL_NONE and level_prev > LEVEL_NONE
    fired_classic = level_now < LEVEL_MID and level_prev >= LEVEL_MID

    return {
        'sqz_level': LEVEL_NAMES[level_now],
        'sqz_days': days_any,
        'sqz_high_days': days_high,
        'sqz_mom': round(mom, 4) if mom is not None else None,
        'sqz_mom_slope': mom_slope,
        'sqz_bias': bias,
        'sqz_fired': fired_any,
        'sqz_fired_dir': (bias if fired_any else None),
        # Classic columns, same source numbers.
        'in_squeeze': level_now >= LEVEL_MID,
        'squeeze_fired': fired_classic,
        'squeeze_days': days_classic,
        'bb_width_pct': bb_width_pct,
    }
