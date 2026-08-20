"""
Graded chart-pattern engine — geometry + volume + trend context.

Why this module exists
---------------------
The first generation of pattern detection (``detect_chart_patterns`` in
live_scan.py) only answered "do the last two or three swing points
roughly look like X?". It used flat percentage tolerances on every
stock regardless of volatility, ignored volume completely (it accepted a
``volumes`` argument it never read), never checked whether the pattern
sat in a trend context where the pattern means anything, and had no
notion of how good an example it had found. Everything was a bare
boolean, so a textbook 3-month cup and a 4-bar accident looked
identical in the UI.

This module answers a harder question: *is this a textbook-quality X,
and what is the trade plan if it triggers?* Every detector here:

  * works off ATR-normalised swing points — a zigzag that only records
    a turn after price retraces ``max(ZIGZAG_ATR_MULT x ATR, 3%)``, so
    a single noisy bar can never manufacture a pivot;
  * validates the *whole* geometry — duration, depth, symmetry, line
    fit, convergence and line violations — instead of the last few
    pivots;
  * requires the volume signature the pattern is supposed to have
    (dry-up inside bases and handles, expansion on poles and breakouts);
  * requires the trend context the pattern needs to be meaningful (a
    continuation base needs a prior advance; a topping pattern needs an
    uptrend to top out from; a bottom needs a decline to reverse);
  * scores itself 0-100 across geometry / volume / trend / maturity /
    tightness, and is discarded below ``MIN_PUBLISH_SCORE``;
  * returns the trade plan (trigger, stop, measured-move target, R:R)
    plus drawing geometry, so the UI can show the level that matters and
    the chart can draw the pattern rather than asking the user to take
    our word for it.

What "accuracy" means here
--------------------------
Accuracy in this module means **detection** accuracy: when it says
"cup with handle", is the shape really a cup with handle. That is
measured by ``scripts/validate_chart_patterns.py`` against a labelled
set of synthetic series — clean patterns, noisy patterns, and negatives
(random walks, straight trends, sine waves, single spikes, V-shapes).

It is explicitly **not** a claim about how often a pattern makes money.
No pattern engine can promise that, and this one does not.

Performance
-----------
Runs on ~2,400 symbols every 60s, so everything is bounded: one O(n)
zigzag pass, suffix max/min arrays for O(1) window extremes, detectors
that work off the (few) swing points, and a hard ``MAX_WINDOW`` cap on
how much history any detector may look at.
"""

from __future__ import annotations

import math

# ── Swing detection ──────────────────────────────────────────────────
ATR_PERIOD = 14
ZIGZAG_ATR_MULT = 1.6    # a turn counts once price retraces this much ATR…
ZIGZAG_MIN_PCT = 0.03    # …or this much of price, whichever is larger

# ── Engine limits ────────────────────────────────────────────────────
MIN_BARS = 70            # below this there is nothing worth measuring
MAX_WINDOW = 260         # ~1 trading year of structure is plenty
MAX_RESULTS = 3          # patterns published per symbol

# ── Score model. Weights sum to 100. A detector that cannot evaluate a
#    component simply loses those points, which is the intended
#    behaviour: an unverifiable pattern should not grade as an A. ─────
WEIGHTS = {
    'geometry':  40,   # how well the shape fits the textbook
    'volume':    20,   # does volume behave the way the pattern requires
    'trend':     20,   # is it in a context where the pattern means anything
    'maturity':  10,   # is the pattern long enough to be real
    'tightness': 10,   # is the structure orderly or noisy
}

GRADE_A = 85
GRADE_B = 72
MIN_PUBLISH_SCORE = 60


# ═════════════════════════════════════════════════════════════════════
# Small numeric helpers (pure Python — no numpy on the scan path)
# ═════════════════════════════════════════════════════════════════════

def _mean(vals) -> float:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _sma_last(vals: list, n: int):
    if not vals or len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def atr_series(highs: list, lows: list, closes: list, period: int = ATR_PERIOD) -> list:
    """Wilder ATR at every bar (front-filled so index maths stays simple)."""
    n = len(closes)
    if n == 0:
        return []
    trs = [max(highs[0] - lows[0], 0.0)]
    for i in range(1, n):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    if n <= period:
        flat = _mean(trs)
        return [flat] * n
    out = [0.0] * n
    run = sum(trs[:period]) / period
    out[period - 1] = run
    for i in range(period, n):
        run = (run * (period - 1) + trs[i]) / period
        out[i] = run
    for i in range(period - 1):
        out[i] = out[period - 1]
    return out


def _fit_line(pts: list):
    """Least-squares fit through (x, y) points -> (slope, intercept, r2)."""
    k = len(pts)
    if k < 2:
        return None
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = k * sxx - sx * sx
    if den == 0:
        return None
    m = (k * sxy - sx * sy) / den
    b = (sy - m * sx) / k
    if k == 2:
        return m, b, 1.0
    ybar = sy / k
    ss_tot = sum((p[1] - ybar) ** 2 for p in pts)
    ss_res = sum((p[1] - (m * p[0] + b)) ** 2 for p in pts)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return m, b, max(0.0, r2)


def _line_at(line, x: float) -> float:
    return line[0] * x + line[1]


def _fit_quad(xs: list, ys: list):
    """Least-squares y = a x^2 + b x + c -> (a, b, c, r2). Used for saucers."""
    k = len(xs)
    if k < 4:
        return None
    s = [0.0] * 5
    t = [0.0] * 3
    for x, y in zip(xs, ys):
        x2 = x * x
        s[0] += 1.0
        s[1] += x
        s[2] += x2
        s[3] += x2 * x
        s[4] += x2 * x2
        t[0] += y
        t[1] += x * y
        t[2] += x2 * y
    # Solve the 3x3 normal-equation system by Gaussian elimination.
    # Unknowns are ordered (c, b, a) — constant, linear, quadratic.
    m = [[s[0], s[1], s[2], t[0]],
         [s[1], s[2], s[3], t[1]],
         [s[2], s[3], s[4], t[2]]]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(3):
            if r == col:
                continue
            f = m[r][col] / pv
            for c in range(col, 4):
                m[r][c] -= f * m[col][c]
    c0 = m[0][3] / m[0][0]   # constant term
    b0 = m[1][3] / m[1][1]   # linear term
    a0 = m[2][3] / m[2][2]   # quadratic term
    ybar = _mean(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - (a0 * x * x + b0 * x + c0)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return a0, b0, c0, max(0.0, r2)


def _trap(v, lo, ilo, ihi, hi):
    """
    Trapezoidal quality score. 1.0 inside the ideal band [ilo, ihi],
    ramping linearly to 0 at the hard bounds lo/hi, and ``None``
    outside them — which every caller treats as "reject this pattern".
    This is how "roughly right" becomes a graded number instead of a
    pass/fail on an arbitrary threshold.
    """
    if v is None:
        return None
    if v < lo or v > hi:
        return None
    if v < ilo:
        return (v - lo) / (ilo - lo) if ilo > lo else 1.0
    if v > ihi:
        return (hi - v) / (hi - ihi) if hi > ihi else 1.0
    return 1.0


def _clamp01(v) -> float:
    return 0.0 if v is None else max(0.0, min(1.0, v))


# ═════════════════════════════════════════════════════════════════════
# ATR zigzag swing points
# ═════════════════════════════════════════════════════════════════════

def swing_points(highs: list, lows: list, closes: list, atr: list,
                 mult: float = ZIGZAG_ATR_MULT, min_pct: float = ZIGZAG_MIN_PCT) -> list:
    """
    Alternating swing highs/lows from an ATR zigzag.

    A running extreme only becomes a pivot once price moves back against
    it by ``max(mult x ATR, min_pct x price)``. That single rule removes
    the whole class of false patterns the old ±3-bar fractal produced,
    where three insignificant wiggles on a quiet stock could be read as
    a head and shoulders.

    The final, still-unconfirmed extreme is returned with ``prov=True``:
    patterns whose last leg is in progress (the right lip of a cup, the
    high of a flag) need it, and callers that require confirmation can
    ignore it. No future bars are ever used, so there is no look-ahead:
    every pivot index is strictly in the past relative to the bar that
    confirmed it.
    """
    n = len(closes)
    out = []
    if n < 10:
        return out

    def thresh(i: int) -> float:
        a = (atr[i] * mult) if i < len(atr) and atr[i] else 0.0
        return max(a, closes[i] * min_pct, 1e-9)

    direction = 0                     # +1 tracking a high, -1 tracking a low
    hi_i, hi_p = 0, highs[0]
    lo_i, lo_p = 0, lows[0]

    for i in range(1, n):
        t = thresh(i)
        if direction == 1:
            if highs[i] > hi_p:
                hi_i, hi_p = i, highs[i]
            elif hi_p - lows[i] >= t:
                out.append({'i': hi_i, 'p': hi_p, 't': 'H'})
                direction = -1
                lo_i, lo_p = i, lows[i]
        elif direction == -1:
            if lows[i] < lo_p:
                lo_i, lo_p = i, lows[i]
            elif highs[i] - lo_p >= t:
                out.append({'i': lo_i, 'p': lo_p, 't': 'L'})
                direction = 1
                hi_i, hi_p = i, highs[i]
        else:
            if highs[i] > hi_p:
                hi_i, hi_p = i, highs[i]
            if lows[i] < lo_p:
                lo_i, lo_p = i, lows[i]
            if hi_i < i and hi_p - lows[i] >= t:
                out.append({'i': hi_i, 'p': hi_p, 't': 'H'})
                direction = -1
                lo_i, lo_p = i, lows[i]
            elif lo_i < i and highs[i] - lo_p >= t:
                out.append({'i': lo_i, 'p': lo_p, 't': 'L'})
                direction = 1
                hi_i, hi_p = i, highs[i]

    if direction == 1:
        out.append({'i': hi_i, 'p': hi_p, 't': 'H', 'prov': True})
    elif direction == -1:
        out.append({'i': lo_i, 'p': lo_p, 't': 'L', 'prov': True})
    return out


# ═════════════════════════════════════════════════════════════════════
# Context — everything the detectors share, computed once per symbol
# ═════════════════════════════════════════════════════════════════════

def build_context(prices: list, highs: list, lows: list, volumes: list,
                  rs=None, live_price=None, dates=None) -> dict:
    n = len(prices)
    highs = list(highs) if highs else list(prices)
    lows = list(lows) if lows else list(prices)
    volumes = list(volumes) if volumes else [0.0] * n
    if len(highs) != n:
        highs = (highs + prices[len(highs):])[:n]
    if len(lows) != n:
        lows = (lows + prices[len(lows):])[:n]
    if len(volumes) != n:
        volumes = (volumes + [0.0] * n)[:n]

    atr = atr_series(highs, lows, prices)
    # Suffix extremes: max high / min low from bar i to the last bar.
    suf_hi = [0.0] * n
    suf_lo = [0.0] * n
    run_h, run_l = -float('inf'), float('inf')
    for i in range(n - 1, -1, -1):
        run_h = max(run_h, highs[i])
        run_l = min(run_l, lows[i])
        suf_hi[i] = run_h
        suf_lo[i] = run_l

    h252 = max(highs[-252:]) if n >= 2 else (highs[-1] if highs else 0)
    px = live_price if live_price else prices[-1]
    return {
        'n': n,
        'closes': prices,
        'highs': highs,
        'lows': lows,
        'volumes': volumes,
        'atr': atr,
        'suf_hi': suf_hi,
        'suf_lo': suf_lo,
        'swings': swing_points(highs, lows, prices, atr),
        'ma50': _sma_last(prices, 50),
        'ma200': _sma_last(prices, 200),
        'vol20': _sma_last(volumes, 20) or 0.0,
        'vol50': _sma_last(volumes, 50) or 0.0,
        'rs': rs,
        'px': px,
        'prev_close': prices[-1],
        'h252': h252,
        'pct_from_high': ((px - h252) / h252 * 100) if h252 else 0.0,
        'dates': dates or None,
        'start': max(0, n - MAX_WINDOW),
    }


def _vol_mean(ctx, a: int, b: int) -> float:
    a, b = max(0, a), min(ctx['n'], b)
    if b <= a:
        return 0.0
    return _mean(ctx['volumes'][a:b])


def _range_high(ctx, a: int, b: int) -> float:
    a, b = max(0, a), min(ctx['n'], b)
    return max(ctx['highs'][a:b]) if b > a else 0.0


def _range_low(ctx, a: int, b: int) -> float:
    a, b = max(0, a), min(ctx['n'], b)
    return min(ctx['lows'][a:b]) if b > a else 0.0


def _prior_move_pct(ctx, at: int, bars: int = 60) -> float:
    """Percent move over the ``bars`` before index ``at`` — the run-up
    (or decline) the pattern is supposed to be resolving."""
    a = max(0, at - bars)
    base = ctx['closes'][a]
    return ((ctx['closes'][at] - base) / base * 100) if base else 0.0


def _rs_component(ctx) -> float:
    rs = ctx.get('rs')
    if rs is None:
        return 0.5      # unknown, not zero — don't punish missing data
    return _clamp01((rs - 35) / 55.0)


def _bull_trend(ctx, start: int) -> float:
    """Trend context for bullish continuation bases: prior advance into
    the base, price above the long average, and decent relative
    strength. A base with no advance in front of it is not a base."""
    adv = _prior_move_pct(ctx, start, 60)
    parts = 0.45 * _clamp01(adv / 25.0)
    ma200 = ctx['ma200']
    ma50 = ctx['ma50']
    if ma200 and ctx['px'] > ma200:
        parts += 0.2
    elif ma50 and ctx['px'] > ma50:
        parts += 0.1
    parts += 0.35 * _rs_component(ctx)
    return parts


def _bear_top_trend(ctx, start: int) -> float:
    """Topping patterns need something to top out of: an advance into
    the pattern, and preferably a stock now losing its 50-day."""
    adv = _prior_move_pct(ctx, start, 70)
    parts = 0.55 * _clamp01(adv / 20.0)
    ma50 = ctx['ma50']
    if ma50 and ctx['px'] < ma50:
        parts += 0.25
    parts += 0.2 * (1.0 - _rs_component(ctx))
    return parts


def _bear_cont_trend(ctx, start: int) -> float:
    """Bearish continuation (bear flag, descending triangle) needs the
    opposite of a base: a decline into the pattern and price under its
    averages."""
    dec = -_prior_move_pct(ctx, start, 60)
    parts = 0.5 * _clamp01(dec / 15.0)
    ma50, ma200 = ctx['ma50'], ctx['ma200']
    if ma50 and ctx['px'] < ma50:
        parts += 0.2
    if ma200 and ctx['px'] < ma200:
        parts += 0.1
    parts += 0.2 * (1.0 - _rs_component(ctx))
    return parts


def _bull_reversal_trend(ctx, start: int) -> float:
    """Bottoming patterns need a decline to reverse, and are far better
    when the stock has already reclaimed its 50-day."""
    dec = -_prior_move_pct(ctx, start, 70)
    parts = 0.5 * _clamp01(dec / 18.0)
    ma50 = ctx['ma50']
    if ma50 and ctx['px'] > ma50:
        parts += 0.3
    parts += 0.2 * _rs_component(ctx)
    return parts


def _tightness(ctx, a: int, b: int) -> float:
    """
    Tight closes — average absolute close-to-close move over the tail of
    the pattern. Orderly, institutionally-supported structure moves
    ~1% a day; a whipsawing random-walk chart moves 2-3% and scores near
    zero. This is what separates a real base from noise that happens to
    fall inside the right bounding box.
    """
    a, b = max(1, a), min(ctx['n'], b)
    lo = max(a, b - 15)
    if b - lo < 5:
        return 0.0
    closes = ctx['closes']
    chg = _mean([abs(closes[i] / closes[i - 1] - 1) * 100
                 for i in range(lo, b) if closes[i - 1]])
    return _clamp01((3.2 - chg) / 2.2)


def _significant_swings(ctx, swings: list, mult: float = 1.8) -> list:
    """
    Drop swings whose leg is smaller than ``mult`` x ATR. Trendlines,
    triangles and head-and-shoulders must be fitted to real turns, not
    to the 3% wiggles the zigzag floor lets through on quiet stocks.
    """
    out = []
    for s in swings:
        if not out:
            out.append(s)
            continue
        prev = out[-1]
        if s['t'] == prev['t']:
            if (s['t'] == 'H' and s['p'] > prev['p']) or (s['t'] == 'L' and s['p'] < prev['p']):
                out[-1] = s
            continue
        ref = ctx['atr'][s['i']] or (s['p'] * 0.02)
        if abs(s['p'] - prev['p']) >= mult * ref:
            out.append(s)
    return out


def _atr_at(ctx, i: int) -> float:
    i = max(0, min(ctx['n'] - 1, i))
    return ctx['atr'][i] or (ctx['closes'][i] * 0.02)


def _dryup(ctx, base_a: int, base_b: int, ref_a: int, ref_b: int) -> float:
    """Volume contraction inside a base versus the move that preceded
    it. Real bases go quiet; distribution does not."""
    base = _vol_mean(ctx, base_a, base_b)
    ref = _vol_mean(ctx, ref_a, ref_b)
    if not base or not ref:
        return 0.4
    return _clamp01((1.05 - base / ref) / 0.45)


def _breakout_vol(ctx) -> float:
    """Today's volume against the 50-day average — the confirmation leg."""
    v50 = ctx['vol50']
    if not v50:
        return 0.4
    return _clamp01((ctx['volumes'][-1] / v50 - 0.9) / 0.8)


# ═════════════════════════════════════════════════════════════════════
# Record building
# ═════════════════════════════════════════════════════════════════════

def _grade(score: int) -> str:
    if score >= GRADE_A:
        return 'A'
    if score >= GRADE_B:
        return 'B'
    return 'C'


def _score(parts: dict) -> int:
    return int(round(sum(WEIGHTS[k] * _clamp01(parts.get(k)) for k in WEIGHTS)))


def _status(direction: str, trigger, ctx) -> str:
    """
    forming — still building, price away from the trigger
    ready   — within 1.5% of the trigger, the decision bar
    fired   — crossed the trigger today (prior close on the other side)
    extended— already through the trigger on an earlier bar
    """
    if not trigger:
        return 'forming'
    prev, px = ctx['prev_close'], ctx['px']
    if direction == 'bull':
        if prev <= trigger < px:
            return 'fired'
        if px > trigger:
            return 'extended'
        return 'ready' if px >= trigger * 0.985 else 'forming'
    if prev >= trigger > px:
        return 'fired'
    if px < trigger:
        return 'extended'
    return 'ready' if px <= trigger * 1.015 else 'forming'


def _near_trigger(ctx, trigger, tol: float = 0.08) -> bool:
    """
    Reversal gate. A reversal pattern is a claim that the trend is
    turning, so it is only worth publishing when price is actually at the
    neckline — within 8% either side. This is what stops the engine
    calling "double top" on a stock sitting happily at its highs, which
    is most of what made the old classic-pattern list untrustworthy, and
    it also drops stale patterns whose neckline broke weeks ago.
    """
    if not trigger or not ctx['px']:
        return False
    return abs(trigger - ctx['px']) / ctx['px'] <= tol


def _date_at(ctx, idx: int):
    dates = ctx.get('dates')
    if not dates or idx is None:
        return None
    if 0 <= idx < len(dates):
        d = dates[idx]
        return d if isinstance(d, str) else str(d)
    return None


def _record(key, name, direction, family, ctx, *, parts, start, end,
            trigger, stop, target, lines=None, pts=None, why='', extra=None) -> dict:
    n = ctx['n']
    score = _score(parts)
    if score < MIN_PUBLISH_SCORE:
        return None
    trigger = round(trigger, 2) if trigger else None
    stop = round(stop, 2) if stop else None
    target = round(target, 2) if target else None
    rr = None
    if trigger and stop and target:
        risk = abs(trigger - stop)
        if risk > 0:
            rr = round(abs(target - trigger) / risk, 2)
    rec = {
        'key': key,
        'name': name,
        'dir': direction,
        'family': family,
        'score': score,
        'grade': _grade(score),
        'status': _status(direction, trigger, ctx),
        'bars': int(end - start + 1),
        'x_start': int(n - 1 - start),
        'x_end': int(n - 1 - end),
        'start_date': _date_at(ctx, start),
        'end_date': _date_at(ctx, end),
        'trigger': trigger,
        'stop': stop,
        'target': target,
        'rr': rr,
        'why': why,
        'parts': {k: round(_clamp01(parts.get(k)) * 100) for k in WEIGHTS},
    }
    if trigger and ctx['px']:
        rec['dist_pct'] = round((trigger - ctx['px']) / ctx['px'] * 100, 2)
    if lines:
        rec['lines'] = [{'k': k,
                         'x1': int(n - 1 - i1), 'y1': round(y1, 2),
                         'x2': int(n - 1 - i2), 'y2': round(y2, 2)}
                        for (k, i1, y1, i2, y2) in lines]
    if pts:
        rec['pts'] = [{'l': l, 'x': int(n - 1 - i), 'y': round(y, 2)} for (l, i, y) in pts]
    if extra:
        rec.update(extra)
    return rec


# ═════════════════════════════════════════════════════════════════════
# Detectors — bullish bases and continuations
# ═════════════════════════════════════════════════════════════════════

def _cup_with_handle(ctx):
    """Best of the cup readings available — see ``_cup_from_lip``."""
    n, sw = ctx['n'], ctx['swings']
    hs = [s for s in sw if s['t'] == 'H' and s['i'] >= ctx['start']]
    if len(hs) < 2:
        return None
    # The right lip may be the current bar (a cup that has just
    # completed) or an earlier swing high with a handle hanging off it.
    # Both readings are valid, so score the last few candidates and keep
    # the best rather than guessing which one the stock is in.
    best = None
    for cand_lip in hs[-3:]:
        rec = _cup_from_lip(ctx, hs, cand_lip)
        if rec and (best is None or rec['score'] > best['score']):
            best = rec
    return best


def _cup_from_lip(ctx, hs: list, right: dict):
    """
    Cup with handle (O'Neil). Rules enforced, all of which the old
    version skipped: U-shaped (not V) bottom, 90-110% right-lip
    recovery, time symmetry between the two halves, handle in the upper
    half of the cup, handle 1-20% deep and at least 3 bars long, volume
    drying up through the handle, and a prior advance into the base.
    """
    n = ctx['n']
    highs, lows = ctx['highs'], ctx['lows']
    rl_i, rl = right['i'], right['p']

    best = None
    for s in hs:
        if s['i'] >= rl_i - 20:
            continue
        ll_i, ll = s['i'], s['p']
        dur = rl_i - ll_i
        if dur < 25 or dur > 250:
            continue
        bot_i = min(range(ll_i, rl_i + 1), key=lambda i: lows[i])
        bot = lows[bot_i]
        lip = max(ll, rl)
        if lip <= 0 or bot <= 0:
            continue
        depth = (lip - bot) / lip * 100
        q_depth = _trap(depth, 11, 15, 35, 50)
        if q_depth is None:
            continue
        recovery = rl / ll if ll else 0
        q_rec = _trap(recovery * 100, 88, 95, 108, 115)
        if q_rec is None:
            continue
        left_leg, right_leg = bot_i - ll_i, rl_i - bot_i
        if left_leg < 5 or right_leg < 5:
            continue
        sym = min(left_leg, right_leg) / max(left_leg, right_leg)
        q_sym = _trap(sym, 0.3, 0.55, 1.0, 1.0)
        if q_sym is None:
            continue
        # U not V: enough bars must sit in the lower third of the cup.
        third = bot + (lip - bot) / 3.0
        rounded = sum(1 for i in range(ll_i, rl_i + 1) if lows[i] <= third) / dur
        q_round = _trap(rounded, 0.08, 0.16, 0.65, 0.85)
        if q_round is None:
            continue
        q_dur = _trap(dur, 25, 35, 160, 250)
        geo = 0.32 * q_depth + 0.22 * q_rec + 0.22 * q_sym + 0.24 * q_round
        cand = (geo, ll_i, ll, bot_i, bot, dur, depth, q_dur)
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None
    geo, ll_i, ll, bot_i, bot, dur, depth, q_dur = best

    # Handle: everything after the right lip.
    h_bars = n - 1 - rl_i
    handle_low = _range_low(ctx, rl_i + 1, n) if h_bars >= 1 else rl
    handle_high = _range_high(ctx, rl_i + 1, n) if h_bars >= 1 else rl
    has_handle = h_bars >= 3
    if has_handle:
        h_depth = (rl - handle_low) / rl * 100 if rl else 0
        q_hd = _trap(h_depth, 0.5, 4, 13, 22)
        q_hb = _trap(h_bars, 3, 5, 30, 60)
        if q_hd is None or q_hb is None:
            return None
        # Handle must hold the upper half of the cup, or it is a new leg down.
        if handle_low < bot + 0.5 * (rl - bot):
            return None
        if handle_high > rl * 1.02:
            return None
        geo = 0.72 * geo + 0.28 * (0.6 * q_hd + 0.4 * q_hb)
        trigger = max(handle_high, rl)
        stop = max(handle_low * 0.995, trigger * 0.90)
        vol = 0.55 * _dryup(ctx, rl_i + 1, n, bot_i, rl_i + 1) + 0.45 * _breakout_vol(ctx)
        name = 'Cup with Handle'
    else:
        geo *= 0.88          # a cup without a handle is a weaker, earlier read
        trigger = rl
        # Without a handle there is no tightening to lean on, so it is
        # only worth naming while price is still near the lip. Otherwise
        # this is just "a stock that recovered", not a base.
        if abs(rl - ctx['px']) / ctx['px'] > 0.08:
            return None
        stop = max(bot + 0.55 * (rl - bot), trigger * 0.90)
        vol = 0.55 * _dryup(ctx, bot_i, n, max(0, ll_i - 40), ll_i + 1) + 0.45 * _breakout_vol(ctx)
        name = 'Cup Base (no handle)'

    target = trigger + (rl - bot)          # classic measured move: cup depth
    parts = {
        'geometry': geo,
        'volume': vol,
        'trend': _bull_trend(ctx, ll_i),
        'maturity': q_dur,
        'tightness': _tightness(ctx, rl_i, n) if has_handle else _tightness(ctx, bot_i, n),
    }
    return _record(
        'cup_handle' if has_handle else 'cup_base', name, 'bull', 'base', ctx,
        parts=parts, start=ll_i, end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('res', ll_i, ll, n - 1, trigger),
               ('trigger', rl_i, trigger, n - 1, trigger)],
        pts=[('L', ll_i, ll), ('Bottom', bot_i, bot), ('R', rl_i, rl)],
        why=f'{depth:.0f}% deep cup over {dur} bars'
            + (f', handle {(rl - handle_low) / rl * 100:.0f}% deep' if has_handle else ''),
        extra={'depth_pct': round(depth, 1)},
    )


def _flat_base(ctx):
    """
    Flat base: a shallow, tight sideways range after an advance. Depth
    <= 15%, at least 25 bars, and volume drying up — the highest-quality
    continuation base there is because the stop is so close to the
    trigger.
    """
    n = ctx['n']
    highs, lows, closes = ctx['highs'], ctx['lows'], ctx['closes']
    best = None
    for dur in range(25, min(90, n - 45) + 1, 5):
        a = n - dur
        hi = ctx['suf_hi'][a]
        lo = ctx['suf_lo'][a]
        if hi <= 0:
            continue
        depth = (hi - lo) / hi * 100
        q_depth = _trap(depth, 0, 0, 11, 17)
        if q_depth is None:
            continue
        # Price must still be in the top half of the range, not sagging out of it.
        pos = (ctx['px'] - lo) / (hi - lo) if hi > lo else 0
        q_pos = _trap(pos, 0.35, 0.6, 1.05, 1.15)
        if q_pos is None:
            continue
        adv = _prior_move_pct(ctx, a, 55)
        if adv < 12:
            continue
        # A base is sideways. A rising channel fits inside a shallow box
        # too, so require the drift across the base to be small and both
        # edges to be genuinely horizontal.
        drift = (closes[-1] - closes[a]) / closes[a] * 100 if closes[a] else 0
        q_drift = _trap(abs(drift), 0, 0, 4, 7)
        if q_drift is None:
            continue
        fit_lo = _fit_line([(i, lows[i]) for i in range(a, n)])
        fit_hi = _fit_line([(i, highs[i]) for i in range(a, n)])
        if not fit_lo or not fit_hi:
            continue
        ref = closes[a] or 1e-9
        tilt = max(abs(fit_lo[0]), abs(fit_hi[0])) * dur / ref * 100
        q_tilt = _trap(tilt, 0, 0, 5, 9)
        if q_tilt is None:
            continue
        # Resistance has to have been tested more than once.
        touch_hi = [i for i in range(a, n) if highs[i] >= hi * 0.975]
        if len(touch_hi) < 2 or (touch_hi[-1] - touch_hi[0]) < 8:
            continue
        q_dur = _trap(dur, 25, 30, 70, 90)
        geo = 0.4 * q_depth + 0.25 * q_pos + 0.2 * q_tilt + 0.15 * q_drift
        cand = (geo * 0.7 + 0.3 * q_dur, a, dur, depth, hi, lo, q_dur, geo)
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None
    _, a, dur, depth, hi, lo, q_dur, geo = best
    trigger = hi
    stop = max(lo * 0.995, trigger * 0.93)
    target = trigger + 2.0 * (trigger - stop)      # no classic measured move: 2R
    parts = {
        'geometry': geo,
        'volume': 0.6 * _dryup(ctx, a, n, max(0, a - 45), a) + 0.4 * _breakout_vol(ctx),
        'trend': _bull_trend(ctx, a),
        'maturity': q_dur,
        'tightness': _tightness(ctx, a, n),
    }
    return _record(
        'flat_base', 'Flat Base', 'bull', 'base', ctx,
        parts=parts, start=a, end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('res', a, hi, n - 1, hi), ('sup', a, lo, n - 1, lo)],
        why=f'{depth:.0f}% deep, {dur}-bar flat range after a {_prior_move_pct(ctx, a, 55):.0f}% advance',
        extra={'depth_pct': round(depth, 1)},
    )


def _pole_flag(ctx, tight: bool):
    """
    Flag / pennant, and its extreme cousin the high tight flag.

    ``tight=True`` looks for the high tight flag: a near-vertical pole
    (>=55%, ideally >=90%) with a shallow, brief flag. ``tight=False`` is
    the ordinary bull flag/pennant: >=12% pole, flag retracing no more
    than half of it. Both require volume expansion on the pole and
    contraction in the flag, which is the whole point of the pattern and
    was entirely missing before.
    """
    n = ctx['n']
    highs, lows, closes = ctx['highs'], ctx['lows'], ctx['closes']
    pole_min = 55.0 if tight else 12.0
    pole_ideal = 90.0 if tight else 22.0
    flag_lens = (6, 8, 10, 13, 16, 20, 25) if tight else (5, 7, 9, 12, 15, 20, 25, 30)
    pole_lens = (10, 15, 20, 25, 30, 40) if tight else (5, 8, 12, 16, 22, 30)
    best = None
    for fl in flag_lens:
        f_a = n - fl
        if f_a < 30:
            continue
        f_hi = ctx['suf_hi'][f_a]
        f_lo = ctx['suf_lo'][f_a]
        for pl in pole_lens:
            p_a = f_a - pl
            if p_a < 10:
                continue
            p_lo = _range_low(ctx, p_a, f_a)
            p_hi = _range_high(ctx, p_a, f_a)
            if p_lo <= 0 or p_hi <= p_lo:
                continue
            pole = (p_hi - p_lo) / p_lo * 100
            q_pole = _trap(pole, pole_min, pole_ideal, 400, 900)
            if q_pole is None:
                continue
            # The pole must end near its high, or it is not a pole.
            if closes[f_a - 1] < p_lo + 0.6 * (p_hi - p_lo):
                continue
            top = max(p_hi, f_hi)
            retr = (top - f_lo) / (p_hi - p_lo) * 100 if p_hi > p_lo else 100
            q_retr = _trap(retr, 0, 0, 25 if tight else 40, 32 if tight else 55)
            if q_retr is None:
                continue
            f_depth = (f_hi - f_lo) / f_hi * 100 if f_hi else 0
            q_depth = _trap(f_depth, 0, 0, 12 if tight else 15, 20 if tight else 25)
            if q_depth is None:
                continue
            geo = 0.4 * q_pole + 0.35 * q_retr + 0.25 * q_depth
            cand = (geo, p_a, f_a, fl, pl, pole, retr, p_lo, p_hi, f_hi, f_lo)
            if best is None or cand[0] > best[0]:
                best = cand
    if not best:
        return None
    geo, p_a, f_a, fl, pl, pole, retr, p_lo, p_hi, f_hi, f_lo = best

    # Flag shape: falling/flat channel = flag, converging = pennant.
    mid = f_a + fl // 2
    early_hi, late_hi = _range_high(ctx, f_a, mid), _range_high(ctx, mid, n)
    early_lo, late_lo = _range_low(ctx, f_a, mid), _range_low(ctx, mid, n)
    converging = late_hi < early_hi and late_lo > early_lo
    if tight:
        key, name = 'high_tight_flag', 'High Tight Flag'
    elif converging:
        key, name = 'bull_pennant', 'Bullish Pennant'
    else:
        key, name = 'bull_flag', 'Bull Flag'

    pole_vol = _vol_mean(ctx, p_a, f_a)
    pre_vol = _vol_mean(ctx, max(0, p_a - 30), p_a)
    surge = _clamp01(((pole_vol / pre_vol) - 1.0) / 0.8) if pre_vol else 0.4
    trigger = f_hi
    stop = max(f_lo * 0.995, trigger * 0.92)
    target = trigger + (p_hi - p_lo) * (0.6 if tight else 1.0)
    parts = {
        'geometry': geo,
        'volume': 0.45 * _dryup(ctx, f_a, n, p_a, f_a) + 0.3 * surge + 0.25 * _breakout_vol(ctx),
        'trend': _bull_trend(ctx, f_a),
        'maturity': _trap(fl, 5, 7, 25, 30) or 0.0,
        'tightness': _tightness(ctx, f_a, n),
    }
    return _record(
        key, name, 'bull', 'continuation', ctx,
        parts=parts, start=p_a, end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('pole', p_a, p_lo, f_a - 1, p_hi),
               ('res', f_a, f_hi, n - 1, f_hi),
               ('sup', f_a, f_lo, n - 1, f_lo)],
        why=f'{pole:.0f}% pole in {pl} bars, {fl}-bar flag holding {100 - retr:.0f}% of it',
        extra={'pole_pct': round(pole, 1)},
    )


def _vcp(ctx):
    """
    Volatility contraction (Minervini). Rewritten off the shared zigzag
    so it agrees with every other detector: 2-4 successive pullbacks,
    each at most 75% of the one before, final contraction tight, volume
    dried up, price near its 52-week high and above the 50-day.
    """
    n, sw = ctx['n'], ctx['swings']
    seq = [s for s in sw if s['i'] >= n - 130]
    contractions, marks = [], []
    for a, b in zip(seq, seq[1:]):
        if a['t'] == 'H' and b['t'] == 'L' and a['p'] > 0:
            contractions.append((b['p'] - a['p']) / a['p'] * -100)
            marks.append((a, b))
    if len(contractions) < 2:
        return None
    # Keep the longest strictly-tightening run that ends at the most
    # recent pullback. Blindly taking the last four throws the pattern
    # away whenever an older, smaller wiggle sits in front of it.
    keep = 1
    while (keep < 4 and keep < len(contractions)
           and contractions[-keep - 1] > contractions[-keep] / 0.78):
        keep += 1
    if keep < 2:
        return None
    contractions, marks = contractions[-keep:], marks[-keep:]
    first, last = contractions[0], contractions[-1]
    q_first = _trap(first, 6, 10, 40, 60)
    q_last = _trap(last, 0.5, 1.5, 10, 16)
    q_ratio = _trap(last / first if first else 1, 0, 0, 0.5, 0.78)
    if None in (q_first, q_last, q_ratio):
        return None
    if ctx['ma50'] and ctx['px'] < ctx['ma50']:
        return None
    q_high = _trap(ctx['pct_from_high'], -32, -14, 0, 6)
    if q_high is None:
        return None
    start = marks[0][0]['i']
    pivot = max(s['p'] for s in sw if s['t'] == 'H' and s['i'] >= start)
    low = marks[-1][1]['p']
    v5, v50 = _sma_last(ctx['volumes'], 5) or 0, ctx['vol50']
    dry = _clamp01((0.95 - (v5 / v50)) / 0.45) if v50 else 0.4
    trigger = max(pivot, ctx['highs'][-1])
    stop = max(low * 0.995, trigger * 0.92)
    parts = {
        'geometry': 0.4 * q_ratio + 0.3 * q_last + 0.3 * q_first,
        'volume': 0.6 * dry + 0.4 * _breakout_vol(ctx),
        'trend': 0.6 * _bull_trend(ctx, start) + 0.4 * q_high,
        'maturity': _trap(n - 1 - start, 15, 25, 120, 130) or 0.0,
        'tightness': _tightness(ctx, marks[-1][0]['i'], n),
    }
    return _record(
        'vcp', 'VCP (Volatility Contraction)', 'bull', 'base', ctx,
        parts=parts, start=start, end=n - 1,
        trigger=trigger, stop=stop, target=trigger + 2.0 * (trigger - stop),
        lines=[('res', start, pivot, n - 1, trigger)],
        pts=[(f'{c:.0f}%', m[1]['i'], m[1]['p']) for c, m in zip(contractions, marks)],
        why=' → '.join(f'{c:.0f}%' for c in contractions) + ' contractions on drying volume',
        extra={'contractions': [round(c, 1) for c in contractions]},
    )


def _rounding_bottom(ctx):
    """
    Rounding bottom / saucer — new here; there was no detector at all
    before. Fitted with a quadratic: the curve must genuinely be convex
    with a good fit (r2 >= 0.72), the low near the middle, and price now
    back in the upper part of the range.
    """
    n = ctx['n']
    closes = ctx['closes']
    best = None
    for dur in (70, 100, 130, 160):
        if n < dur + 20:
            continue
        a = n - dur
        ys = closes[a:]
        xs = [i / dur for i in range(len(ys))]
        fit = _fit_quad(xs, ys)
        if not fit:
            continue
        q_a, q_b, q_c, r2 = fit
        if q_a <= 0:
            continue
        q_fit = _trap(r2, 0.72, 0.85, 1.0, 1.0)
        if q_fit is None:
            continue
        vertex = -q_b / (2 * q_a) if q_a else 0.5
        q_vert = _trap(vertex, 0.2, 0.32, 0.68, 0.82)
        if q_vert is None:
            continue
        hi = ctx['suf_hi'][a]
        lo = ctx['suf_lo'][a]
        if hi <= lo:
            continue
        depth = (hi - lo) / hi * 100
        q_depth = _trap(depth, 10, 15, 45, 60)
        if q_depth is None:
            continue
        pos = (ctx['px'] - lo) / (hi - lo)
        q_pos = _trap(pos, 0.6, 0.78, 1.05, 1.15)
        if q_pos is None:
            continue
        geo = 0.35 * q_fit + 0.2 * q_vert + 0.2 * q_depth + 0.25 * q_pos
        cand = (geo, a, dur, depth, hi, lo, r2)
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None
    geo, a, dur, depth, hi, lo, r2 = best
    trigger = hi
    stop = max(lo + 0.45 * (hi - lo), trigger * 0.90)
    parts = {
        'geometry': geo,
        'volume': 0.5 * _clamp01((_vol_mean(ctx, a + dur // 2, n) / (_vol_mean(ctx, a, a + dur // 2) or 1e-9) - 0.85) / 0.5)
                  + 0.5 * _breakout_vol(ctx),
        'trend': _bull_reversal_trend(ctx, a),
        'maturity': _trap(dur, 60, 80, 160, 180) or 0.0,
        'tightness': _tightness(ctx, n - 30, n),
    }
    return _record(
        'rounding_bottom', 'Rounding Bottom', 'bull', 'reversal', ctx,
        parts=parts, start=a, end=n - 1,
        trigger=trigger, stop=stop, target=trigger + (hi - lo),
        lines=[('res', a, hi, n - 1, hi)],
        why=f'{dur}-bar saucer, {depth:.0f}% deep, curve fit r²={r2:.2f}',
        extra={'depth_pct': round(depth, 1)},
    )


# ═════════════════════════════════════════════════════════════════════
# Detectors — double tops/bottoms and head & shoulders
# ═════════════════════════════════════════════════════════════════════

def _double(ctx, bullish: bool):
    """
    Double bottom (W) / double top (M). Now requires: the two extremes
    to match within ATR-scaled tolerance, at least 12 bars between them,
    a middle counter-swing of real size, and the prior trend the
    reversal is supposed to reverse. A small undercut of the first low
    by the second scores *better* for a double bottom, which is how the
    good ones actually look.
    """
    n = ctx['n']
    sw = _significant_swings(ctx, [s for s in ctx['swings'] if s['i'] >= n - 160])
    want, other = ('L', 'H') if bullish else ('H', 'L')
    ext = [s for s in sw if s['t'] == want]
    mids = [s for s in sw if s['t'] == other]
    if len(ext) < 2 or not mids:
        return None
    # A double top has to be *at the top*. Two equal highs in the middle
    # of a range are just two equal highs, and that is where most of the
    # false positives on choppy charts came from.
    win_hi = _range_high(ctx, n - 130, n)
    win_lo = _range_low(ctx, n - 130, n)
    best = None
    # All plausible pairs, not just neighbours: the two tops of a real
    # double top usually have a minor swing between them, which is
    # exactly the case the old adjacent-pivot scan could never see.
    for i in range(len(ext) - 1):
        for j in range(i + 1, min(i + 4, len(ext))):
            e1, e2 = ext[i], ext[j]
            gap = e2['i'] - e1['i']
            if gap < 15 or gap > 120:
                continue
            # The middle of an M or a W is a genuine turn, not whichever
            # bar happened to print the extreme in between.
            if not any(e1['i'] < m['i'] < e2['i'] for m in mids):
                continue
            if bullish:
                mp = _range_high(ctx, e1['i'], e2['i'] + 1)
                mi = max(range(e1['i'], e2['i'] + 1), key=lambda x: ctx['highs'][x])
            else:
                mp = _range_low(ctx, e1['i'], e2['i'] + 1)
                mi = min(range(e1['i'], e2['i'] + 1), key=lambda x: ctx['lows'][x])
            mid = {'i': mi, 'p': mp}
            atr_ref = ctx['atr'][e2['i']] or (e2['p'] * 0.02)
            diff = abs(e1['p'] - e2['p'])
            # Textbook doubles match within ~3%. A wide, ATR-scaled
            # tolerance lets any volatile chart qualify, so cap it.
            tol = max(1.2 * atr_ref, e1['p'] * 0.025)
            q_eq = _trap(diff / tol if tol else 1, 0, 0, 0.7, 1.0)
            if q_eq is None:
                continue
            base = min(e1['p'], e2['p']) if bullish else max(e1['p'], e2['p'])
            if bullish:
                if base > win_lo * 1.04:
                    continue
            elif base < win_hi * 0.96:
                continue
            height = abs(mid['p'] - base)
            h_pct = height / base * 100 if base else 0
            q_h = _trap(h_pct, 5, 8, 35, 55)
            if q_h is None:
                continue
            # The counter-swing has to matter relative to the stock's own
            # noise, or any choppy chart contains a "double bottom".
            if height < 2.5 * _atr_at(ctx, mid['i']):
                continue
            # Textbook volume signature: the second test comes on lighter
            # volume than the first — buyers exhausted at a double top,
            # sellers exhausted at a double bottom. Noise fails this half
            # the time, which is the point.
            v1 = _vol_mean(ctx, e1['i'] - 2, e1['i'] + 3)
            v2 = _vol_mean(ctx, e2['i'] - 2, e2['i'] + 3)
            if v1 and v2 and v2 > v1 * 1.15:
                continue
            q_gap = _trap(gap, 12, 18, 80, 120)
            # Second leg slightly beyond the first (a shakeout) is a plus.
            undercut = ((e1['p'] - e2['p']) / e1['p'] * 100) if bullish \
                else ((e2['p'] - e1['p']) / e1['p'] * 100)
            q_shake = 1.0 if 0 < undercut <= 3 else 0.6
            geo = 0.4 * q_eq + 0.3 * q_h + 0.3 * q_shake
            cand = (geo, e1, e2, mid, q_gap, h_pct)
            if best is None or cand[0] > best[0]:
                best = cand
    if not best:
        return None
    geo, e1, e2, mid, q_gap, h_pct = best
    trigger = mid['p']
    if bullish:
        stop = max(min(e1['p'], e2['p']) * 0.99, trigger * 0.90)
        target = trigger + (trigger - min(e1['p'], e2['p']))
        trend = _bull_reversal_trend(ctx, e1['i'])
        key, name, direction = 'double_bottom', 'Double Bottom', 'bull'
    else:
        stop = min(max(e1['p'], e2['p']) * 1.01, trigger * 1.10)
        target = trigger - (max(e1['p'], e2['p']) - trigger)
        trend = _bear_top_trend(ctx, e1['i'])
        key, name, direction = 'double_top', 'Double Top', 'bear'
    if not _near_trigger(ctx, trigger):
        return None
    parts = {
        'geometry': geo,
        'volume': (0.5 * _dryup(ctx, mid['i'], n, e1['i'], mid['i']) + 0.5 * _breakout_vol(ctx)
                   if bullish else 0.4 + 0.6 * _breakout_vol(ctx)),
        'trend': trend,
        'maturity': q_gap,
        'tightness': _tightness(ctx, mid['i'], n),
    }
    return _record(
        key, name, direction, 'reversal', ctx,
        parts=parts, start=e1['i'], end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('neck', e1['i'], trigger, n - 1, trigger)],
        pts=[('1', e1['i'], e1['p']), ('2', e2['i'], e2['p'])],
        why=f'two {"lows" if bullish else "highs"} {abs(e1["p"] - e2["p"]) / e1["p"] * 100:.1f}% apart, '
            f'{h_pct:.0f}% counter-swing between',
    )


def _head_shoulders(ctx, inverse: bool):
    """
    Head and shoulders (top) / inverse (bottom). Requires the full
    five-point sequence from the zigzag, a head clearly beyond both
    shoulders, shoulders within 12% of each other, time symmetry inside
    0.4-2.5, a neckline that is not wildly sloped, and the correct prior
    trend. The old version took the last three pivots and hoped.
    """
    n = ctx['n']
    seq = _significant_swings(ctx, [s for s in ctx['swings'] if s['i'] >= n - 170])
    if len(seq) < 5:
        return None
    s_t, m_t = ('L', 'H') if inverse else ('H', 'L')
    best = None
    for k in range(len(seq) - 4):
        a, b, c, d, e = seq[k:k + 5]
        if [x['t'] for x in (a, b, c, d, e)] != [s_t, m_t, s_t, m_t, s_t]:
            continue
        ls, head, rs = a['p'], c['p'], e['p']
        t1, t2 = b['p'], d['p']
        atr_ref = ctx['atr'][c['i']] or head * 0.02
        if inverse:
            if head > min(ls, rs) - max(1.0 * atr_ref, 0.015 * head):
                continue
            depth = ((max(t1, t2) - head) / max(t1, t2) * 100) if max(t1, t2) else 0
        else:
            if head < max(ls, rs) + max(1.0 * atr_ref, 0.015 * head):
                continue
            depth = ((head - min(t1, t2)) / head * 100) if head else 0
        q_sh = _trap(abs(ls - rs) / max(ls, rs) * 100, 0, 0, 7, 12)
        if q_sh is None:
            continue
        left_t, right_t = c['i'] - a['i'], e['i'] - c['i']
        if left_t < 4 or right_t < 4:
            continue
        q_time = _trap(min(left_t, right_t) / max(left_t, right_t), 0.35, 0.55, 1.0, 1.0)
        if q_time is None:
            continue
        q_neck = _trap(abs(t1 - t2) / max(t1, t2) * 100, 0, 0, 6, 12)
        if q_neck is None:
            continue
        q_depth = _trap(depth, 5, 9, 40, 60)
        if q_depth is None:
            continue
        # The right shoulder must form on lighter volume than the head:
        # the classic sign that the move that built the head has run out
        # of participation.
        v_head = _vol_mean(ctx, c['i'] - 3, c['i'] + 4)
        v_rs = _vol_mean(ctx, e['i'] - 3, e['i'] + 4)
        if v_head and v_rs and v_rs > v_head * 1.2:
            continue
        geo = 0.3 * q_sh + 0.25 * q_time + 0.2 * q_neck + 0.25 * q_depth
        cand = (geo, a, b, c, d, e, depth)
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None
    geo, a, b, c, d, e, depth = best
    neck = _fit_line([(b['i'], b['p']), (d['i'], d['p'])])
    trigger = _line_at(neck, n - 1) if neck else (b['p'] + d['p']) / 2
    height = abs(c['p'] - (_line_at(neck, c['i']) if neck else trigger))
    if inverse:
        stop = max(e['p'] * 0.99, trigger * 0.90)
        target = trigger + height
        trend = _bull_reversal_trend(ctx, a['i'])
        key, name, direction = 'inv_head_shoulders', 'Inverse Head & Shoulders', 'bull'
    else:
        stop = min(e['p'] * 1.01, trigger * 1.10)
        target = trigger - height
        trend = _bear_top_trend(ctx, a['i'])
        key, name, direction = 'head_shoulders', 'Head & Shoulders', 'bear'
    if height < 3.0 * _atr_at(ctx, c['i']):
        return None
    if not _near_trigger(ctx, trigger):
        return None
    parts = {
        'geometry': geo,
        'volume': 0.5 * _breakout_vol(ctx) + 0.5 * (
            _dryup(ctx, d['i'], n, a['i'], c['i']) if inverse else 0.5),
        'trend': trend,
        'maturity': _trap(e['i'] - a['i'], 20, 30, 140, 170) or 0.0,
        'tightness': _tightness(ctx, d['i'], n),
    }
    return _record(
        key, name, direction, 'reversal', ctx,
        parts=parts, start=a['i'], end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('neck', b['i'], b['p'], n - 1, trigger)],
        pts=[('LS', a['i'], a['p']), ('H', c['i'], c['p']), ('RS', e['i'], e['p'])],
        why=f'head {depth:.0f}% beyond a neckline, shoulders '
            f'{abs(a["p"] - e["p"]) / max(a["p"], e["p"]) * 100:.1f}% apart',
    )


# ═════════════════════════════════════════════════════════════════════
# Detectors — trendline patterns (triangles and wedges)
# ═════════════════════════════════════════════════════════════════════

def _trendline_pattern(ctx):
    """
    Triangles and wedges from fitted trendlines rather than a two-point
    slope. Requires at least two swing touches per line, an acceptable
    fit, real convergence (the range must narrow by >=25%), volume
    contraction, and *no* meaningful violation of either line — a
    "triangle" whose highs have already been blown through is not a
    triangle. Returns whichever of the five shapes fits best.
    """
    n = ctx['n']
    highs, lows, atr = ctx['highs'], ctx['lows'], ctx['atr']
    # 1.3 x ATR rather than the default 1.8: converging patterns end in
    # small legs by definition, and dropping them loses the pattern.
    sig = _significant_swings(ctx, [s for s in ctx['swings'] if s['i'] >= ctx['start']], mult=1.3)
    best = None
    # Scope the fit to the last k turns rather than a fixed bar window:
    # a window is what dragged the pre-pattern trend's swings into the
    # regression and destroyed the fit.
    for k in (4, 5, 6, 7, 8, 9, 10):
        if len(sig) < k:
            break
        sub = sig[-k:]
        hp = [(s['i'], s['p']) for s in sub if s['t'] == 'H']
        lp = [(s['i'], s['p']) for s in sub if s['t'] == 'L']
        if len(hp) < 2 or len(lp) < 2:
            continue
        a0 = sub[0]['i']
        span = n - 1 - a0
        if span < 20 or span > 170:
            continue
        up = _fit_line(hp)
        dn = _fit_line(lp)
        if not up or not dn:
            continue
        # Line quality as residual scatter in ATR units, not r². r² is
        # useless for the flat line of an ascending triangle: with no
        # slope to explain there is no variance either, so a perfectly
        # horizontal resistance scores r² ~ 0 and gets thrown away.
        ref_atr = _atr_at(ctx, n - 1)
        resid = max(max(abs(p - _line_at(up, i)) for i, p in hp),
                    max(abs(p - _line_at(dn, i)) for i, p in lp)) / ref_atr
        q_fit = _trap(resid, 0, 0, 0.9, 2.2)
        if q_fit is None:
            continue
        w0 = _line_at(up, a0) - _line_at(dn, a0)
        w1 = _line_at(up, n - 1) - _line_at(dn, n - 1)
        if w0 <= 0 or w1 <= 0:
            continue
        # Lines must not have been violated before the last few bars.
        viol = sum(1 for i in range(a0, n - 3)
                   if highs[i] > _line_at(up, i) + 1.3 * (atr[i] or 0)
                   or lows[i] < _line_at(dn, i) - 1.3 * (atr[i] or 0))
        if viol > max(1, span // 30):
            continue
        ref = ctx['closes'][a0] or 1e-9
        su = up[0] / ref          # slope as fraction of price per bar
        sl = dn[0] / ref
        flat = 0.0008
        contract = 1 - w1 / w0
        prior = _prior_move_pct(ctx, a0, 60)
        kind = None
        if abs(su) <= flat and sl > flat:
            kind = ('asc_triangle', 'Ascending Triangle', 'bull', 'continuation')
        elif abs(sl) <= flat and su < -flat:
            kind = ('desc_triangle', 'Descending Triangle', 'bear', 'continuation')
        elif su < -flat and sl > flat:
            kind = ('sym_triangle', 'Symmetrical Triangle',
                    'bull' if prior >= 0 else 'bear', 'continuation')
        elif su > flat and sl > flat and su < sl:
            kind = ('rising_wedge', 'Rising Wedge', 'bear', 'reversal')
        elif su < -flat and sl < -flat and su < sl:
            kind = ('falling_wedge', 'Falling Wedge', 'bull', 'reversal')
        if not kind:
            continue
        q_con = _trap(contract, 0.18, 0.3, 0.9, 0.98)
        if q_con is None:
            continue
        q_touch = _trap(min(len(hp), len(lp)), 2, 3, 6, 9) or 0.0
        # The apex must still be ahead of price, not already behind it.
        if w1 < 0.4 * _atr_at(ctx, n - 1):
            continue
        geo = 0.4 * q_fit + 0.35 * q_con + 0.25 * q_touch
        cand = (geo, kind, up, dn, a0, span, contract, len(hp), len(lp))
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None
    geo, (key, name, direction, family), up, dn, a0, span, contract, nh, nl = best
    hi_now = _line_at(up, n - 1)
    lo_now = _line_at(dn, n - 1)
    height0 = _line_at(up, a0) - _line_at(dn, a0)
    if direction == 'bull':
        trigger = hi_now
        stop = max(lo_now, trigger * 0.93)
        target = trigger + height0
        trend = _bull_trend(ctx, a0) if family == 'continuation' else _bull_reversal_trend(ctx, a0)
    else:
        trigger = lo_now
        stop = min(hi_now, trigger * 1.07)
        target = trigger - height0
        trend = _bear_top_trend(ctx, a0) if family == 'reversal' else _bear_cont_trend(ctx, a0)
    parts = {
        'geometry': geo,
        'volume': 0.6 * _dryup(ctx, n - span // 3, n, a0, a0 + span // 3) + 0.4 * _breakout_vol(ctx),
        'trend': trend,
        'maturity': _trap(span, 20, 30, 120, 150) or 0.0,
        'tightness': _tightness(ctx, a0, n),
    }
    return _record(
        key, name, direction, family, ctx,
        parts=parts, start=a0, end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('res', a0, _line_at(up, a0), n - 1, hi_now),
               ('sup', a0, _line_at(dn, a0), n - 1, lo_now)],
        why=f'{nh} highs / {nl} lows on trend, range narrowed {contract * 100:.0f}% over {span} bars',
    )


def _bear_flag(ctx):
    """Bear flag: a sharp decline, then a weak, low-volume drift back up."""
    n = ctx['n']
    closes = ctx['closes']
    best = None
    for fl in (5, 8, 12, 16, 20):
        f_a = n - fl
        if f_a < 30:
            continue
        f_hi = ctx['suf_hi'][f_a]
        f_lo = ctx['suf_lo'][f_a]
        for pl in (8, 12, 18, 25):
            p_a = f_a - pl
            if p_a < 10:
                continue
            p_hi = _range_high(ctx, p_a, f_a)
            p_lo = _range_low(ctx, p_a, f_a)
            if p_hi <= 0 or p_hi <= p_lo:
                continue
            drop = (p_hi - p_lo) / p_hi * 100
            q_drop = _trap(drop, 10, 15, 45, 70)
            if q_drop is None:
                continue
            if (p_hi - p_lo) < 4.0 * _atr_at(ctx, f_a):
                continue          # a "decline" inside the noise is not a pole
            # Bear flags form at the lows of the decline, not mid-range.
            if p_lo > _range_low(ctx, n - 45, n) * 1.03:
                continue
            if closes[f_a - 1] > p_lo + 0.4 * (p_hi - p_lo):
                continue
            bounce = (f_hi - p_lo) / (p_hi - p_lo) * 100 if p_hi > p_lo else 100
            q_bounce = _trap(bounce, 0, 0, 45, 62)
            if q_bounce is None:
                continue
            # The bounce must be listless. A rally on heavier volume than
            # the decline is a reversal attempt, not a bear flag.
            vp, vf = _vol_mean(ctx, p_a, f_a), _vol_mean(ctx, f_a, n)
            if vp and vf and vf > vp * 0.85:
                continue
            geo = 0.55 * q_drop + 0.45 * q_bounce
            cand = (geo, p_a, f_a, fl, pl, drop, p_hi, p_lo, f_lo)
            if best is None or cand[0] > best[0]:
                best = cand
    if not best:
        return None
    geo, p_a, f_a, fl, pl, drop, p_hi, p_lo, f_lo = best
    trigger = f_lo
    stop = min(_range_high(ctx, f_a, n) * 1.005, trigger * 1.08)
    target = trigger - (p_hi - p_lo)
    parts = {
        'geometry': geo,
        'volume': 0.6 * _dryup(ctx, f_a, n, p_a, f_a) + 0.4 * _breakout_vol(ctx),
        'trend': _bear_cont_trend(ctx, p_a),
        'maturity': _trap(fl, 5, 7, 18, 22) or 0.0,
        'tightness': _tightness(ctx, f_a, n),
    }
    return _record(
        'bear_flag', 'Bear Flag', 'bear', 'continuation', ctx,
        parts=parts, start=p_a, end=n - 1,
        trigger=trigger, stop=stop, target=target,
        lines=[('sup', f_a, f_lo, n - 1, f_lo)],
        why=f'{drop:.0f}% decline, then a {fl}-bar drift on lighter volume',
    )


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════

DETECTORS = (
    ('cup', lambda c: _cup_with_handle(c)),
    ('flat', lambda c: _flat_base(c)),
    ('htf', lambda c: _pole_flag(c, True)),
    ('flag', lambda c: _pole_flag(c, False)),
    ('vcp', lambda c: _vcp(c)),
    ('saucer', lambda c: _rounding_bottom(c)),
    ('dbot', lambda c: _double(c, True)),
    ('dtop', lambda c: _double(c, False)),
    ('ihs', lambda c: _head_shoulders(c, True)),
    ('hs', lambda c: _head_shoulders(c, False)),
    ('trend', lambda c: _trendline_pattern(c)),
    ('bflag', lambda c: _bear_flag(c)),
)

# Every pattern this engine can emit, for UI menus and docs.
PATTERN_CATALOG = {
    'cup_handle':        ('Cup with Handle', 'bull', 'base'),
    'cup_base':          ('Cup Base (no handle)', 'bull', 'base'),
    'flat_base':         ('Flat Base', 'bull', 'base'),
    'vcp':               ('VCP (Volatility Contraction)', 'bull', 'base'),
    'high_tight_flag':   ('High Tight Flag', 'bull', 'continuation'),
    'bull_flag':         ('Bull Flag', 'bull', 'continuation'),
    'bull_pennant':      ('Bullish Pennant', 'bull', 'continuation'),
    'asc_triangle':      ('Ascending Triangle', 'bull', 'continuation'),
    'sym_triangle':      ('Symmetrical Triangle', 'bull', 'continuation'),
    'falling_wedge':     ('Falling Wedge', 'bull', 'reversal'),
    'double_bottom':     ('Double Bottom', 'bull', 'reversal'),
    'inv_head_shoulders': ('Inverse Head & Shoulders', 'bull', 'reversal'),
    'rounding_bottom':   ('Rounding Bottom', 'bull', 'reversal'),
    'head_shoulders':    ('Head & Shoulders', 'bear', 'reversal'),
    'double_top':        ('Double Top', 'bear', 'reversal'),
    'rising_wedge':      ('Rising Wedge', 'bear', 'reversal'),
    'desc_triangle':     ('Descending Triangle', 'bear', 'continuation'),
    'bear_flag':         ('Bear Flag', 'bear', 'continuation'),
}


# Ordering only — never added to the published score. A shallow box
# contains almost every other pattern, so when a specific structure and
# a generic range score about the same, name the specific one.
_ORDER_BONUS = {
    'cup_handle': 5, 'high_tight_flag': 5, 'vcp': 4, 'asc_triangle': 4,
    'desc_triangle': 4, 'sym_triangle': 3, 'rising_wedge': 4,
    'falling_wedge': 4, 'head_shoulders': 4, 'inv_head_shoulders': 4,
    'double_top': 3, 'double_bottom': 3, 'rounding_bottom': 3,
    'bull_pennant': 2, 'bull_flag': 2, 'bear_flag': 2,
    'flat_base': 0, 'cup_base': 0,
}


def detect_patterns(prices: list, highs: list = None, lows: list = None,
                    volumes: list = None, *, rs=None, live_price=None,
                    dates: list = None, max_results: int = MAX_RESULTS,
                    min_score: int = MIN_PUBLISH_SCORE) -> list:
    """
    Every publishable pattern for one symbol, best first.

    Each record carries the pattern identity, a 0-100 score with its
    A/B/C grade, the component breakdown, status (forming / ready /
    fired / extended), the trade plan (trigger, stop, measured target,
    R:R) and drawing geometry.
    """
    if not prices or len(prices) < MIN_BARS:
        return []
    n = len(prices)
    if n > MAX_WINDOW:
        cut = n - MAX_WINDOW
        prices = prices[cut:]
        highs = highs[cut:] if highs else None
        lows = lows[cut:] if lows else None
        volumes = volumes[cut:] if volumes else None
        dates = dates[cut:] if dates else None
    ctx = build_context(prices, highs, lows, volumes, rs=rs,
                        live_price=live_price, dates=dates)
    out = []
    for _, fn in DETECTORS:
        try:
            rec = fn(ctx)
        except Exception:
            rec = None      # one bad symbol must never stop the scan
        if rec and rec['score'] >= min_score:
            out.append(rec)
    out.sort(key=lambda r: (-(r['score'] + _ORDER_BONUS.get(r['key'], 0)), r['key']))
    # Overlap is real information — a cup with handle that is also a flat
    # base is a stronger setup, not a duplicate — so keep the top few
    # rather than collapsing by family.
    return out[:max_results]


def summarize(records: list) -> dict:
    """
    Flatten the best record into scalar columns for the stocks table,
    plus the legacy booleans the old UI and alerts still read. Legacy
    flags now only fire for patterns that actually passed scoring, so
    everything downstream gets the cleaner list for free.
    """
    out = {
        'pattern_key': None, 'pattern_name': None, 'pattern_dir': None,
        'pattern_family': None, 'pattern_score': None, 'pattern_grade': None,
        'pattern_status': None, 'pattern_trigger': None, 'pattern_stop': None,
        'pattern_target': None, 'pattern_rr': None, 'pattern_bars': None,
        'pattern_count': 0,
        'is_head_shoulders': False, 'is_inv_head_shoulders': False,
        'is_double_top': False, 'is_double_bottom': False,
        'triangle_type': None, 'wedge_type': None,
        'is_flag_bullish': False, 'is_flag_bearish': False, 'is_pennant': False,
        'chart_pattern_fired': False,
    }
    if not records:
        return out
    best = records[0]
    out.update({
        'pattern_key': best['key'], 'pattern_name': best['name'],
        'pattern_dir': best['dir'], 'pattern_family': best['family'],
        'pattern_score': best['score'], 'pattern_grade': best['grade'],
        'pattern_status': best['status'], 'pattern_trigger': best['trigger'],
        'pattern_stop': best['stop'], 'pattern_target': best['target'],
        'pattern_rr': best['rr'], 'pattern_bars': best['bars'],
        'pattern_count': len(records),
    })
    for r in records:
        k = r['key']
        if k == 'head_shoulders':
            out['is_head_shoulders'] = True
        elif k == 'inv_head_shoulders':
            out['is_inv_head_shoulders'] = True
        elif k == 'double_top':
            out['is_double_top'] = True
        elif k == 'double_bottom':
            out['is_double_bottom'] = True
        elif k == 'asc_triangle':
            out['triangle_type'] = 'ascending'
        elif k == 'desc_triangle':
            out['triangle_type'] = 'descending'
        elif k == 'sym_triangle':
            out['triangle_type'] = 'symmetrical'
        elif k == 'rising_wedge':
            out['wedge_type'] = 'rising'
        elif k == 'falling_wedge':
            out['wedge_type'] = 'falling'
        elif k in ('bull_flag', 'high_tight_flag'):
            out['is_flag_bullish'] = True
        elif k == 'bear_flag':
            out['is_flag_bearish'] = True
        elif k == 'bull_pennant':
            out['is_pennant'] = True
        if r['status'] == 'fired':
            out['chart_pattern_fired'] = True
    return out
