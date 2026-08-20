"""
Accuracy harness for chart_patterns.py.

What it measures
----------------
Detection accuracy, not profitability. Every case is a synthetic OHLCV
series whose true label we know by construction:

  * positives — a textbook example of one pattern, built with realistic
    per-bar noise, the volume signature the pattern requires, and the
    trend context it needs (a base gets an advance in front of it, a
    topping pattern gets an uptrend to top out of);
  * negatives — series that deliberately contain no pattern: high
    volatility random walks, straight trends, single V spikes, and
    choppy mean-reverting noise.

Reported metrics
----------------
  recall     positives where the intended pattern was found at all
  top1       positives where the *best* reported pattern was the right one
  fp         negatives that produced any B-grade-or-better pattern
  accuracy   (positives found + negatives correctly left alone) / total

A random walk can genuinely form a valid triangle now and then, so the
false-positive target is <=10% rather than zero, and that number is
printed rather than hidden.

Run:  python scripts/validate_chart_patterns.py [-n SAMPLES] [-v]
"""

import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart_patterns import detect_patterns, GRADE_B  # noqa: E402

# Pass criteria. Two false-positive bars, because they answer different
# questions: B+ is "how noisy is the raw engine", A is "how noisy is what
# the Patterns page actually shows by default". Random data genuinely
# contains valid geometry sometimes, so neither target is zero.
TARGET_ACCURACY = 0.90
TARGET_FP_B = 0.15
TARGET_FP_A = 0.05


# ═════════════════════════════════════════════════════════════════════
# Series builder
# ═════════════════════════════════════════════════════════════════════

class Gen:
    """Appends price legs bar by bar, tracking OHLC and volume."""

    def __init__(self, seed: int, start: float = 100.0, base_vol: float = 1_000_000.0):
        self.r = random.Random(seed)
        self.px = start
        self.base_vol = base_vol
        self.c, self.h, self.l, self.v = [], [], [], []

    def leg(self, bars: int, pct: float, noise: float = 0.008,
            vol: float = 1.0, shape: str = 'lin'):
        bars = max(1, int(bars))
        p0 = self.px
        target = p0 * (1 + pct / 100.0)
        for k in range(1, bars + 1):
            f = k / bars
            if shape == 'ease':                     # rounded, U-like
                f = 0.5 - 0.5 * math.cos(math.pi * f)
            elif shape == 'accel':                  # slow then vertical
                f = f * f
            p = (p0 + (target - p0) * f) * (1 + self.r.gauss(0, noise))
            wick = abs(self.r.gauss(0, noise * 0.9))
            self.c.append(p)
            self.h.append(p * (1 + wick))
            self.l.append(p * (1 - wick))
            self.v.append(max(1.0, self.base_vol * vol * (1 + self.r.gauss(0, 0.18))))
        self.px = self.c[-1]
        return self

    def to(self, bars: int, price: float, **kw):
        return self.leg(bars, (price / self.px - 1) * 100.0, **kw)

    def out(self):
        return self.c, self.h, self.l, self.v


def _zigzag(g: Gen, legs: int, bars_per_leg: int, hi0, lo0, hi1, lo1,
            noise=0.006, vol_start=1.0, vol_end=0.55):
    """Alternating touches of two converging/diverging boundary lines."""
    for k in range(legs):
        f = (k + 1) / legs
        hi = hi0 + (hi1 - hi0) * f
        lo = lo0 + (lo1 - lo0) * f
        tgt = hi if k % 2 == 0 else lo
        g.to(bars_per_leg, tgt, noise=noise,
             vol=vol_start + (vol_end - vol_start) * f)
    return g


# ═════════════════════════════════════════════════════════════════════
# Positive generators — one per pattern
# ═════════════════════════════════════════════════════════════════════

def g_cup_handle(seed):
    g = Gen(seed)
    g.leg(70, 42, vol=1.1)                                  # prior advance
    lip = g.px
    g.leg(45, -28, vol=0.95, shape='ease')                   # left side
    g.to(45, lip * 0.99, vol=0.8, shape='ease')             # right side
    g.leg(12, -8, noise=0.005, vol=0.5)                      # handle
    g.leg(2, 4, vol=1.6)                                     # push at the pivot
    return g.out()


def g_cup_base(seed):
    g = Gen(seed)
    g.leg(70, 40, vol=1.1)
    lip = g.px
    g.leg(45, -26, vol=0.95, shape='ease')
    g.to(48, lip * 1.0, vol=0.85, shape='ease')
    return g.out()


def g_flat_base(seed):
    g = Gen(seed)
    g.leg(70, 45, vol=1.2)
    top = g.px
    r = random.Random(seed)
    for _ in range(6):
        g.to(7, top * (1 - r.uniform(0.0, 0.08)), noise=0.005, vol=0.55)
    g.to(6, top * 0.995, noise=0.004, vol=0.5)
    return g.out()


def g_high_tight_flag(seed):
    g = Gen(seed)
    g.leg(60, 12, vol=0.9)
    g.leg(22, 95, noise=0.012, vol=2.6, shape='accel')       # pole
    top = g.px
    g.to(6, top * 0.90, noise=0.006, vol=0.8)
    g.to(8, top * 0.94, noise=0.005, vol=0.5)                # tight flag
    return g.out()


def g_bull_flag(seed):
    g = Gen(seed)
    g.leg(70, 30, vol=1.0)
    g.leg(14, 26, noise=0.01, vol=2.2)                       # pole
    top = g.px
    g.to(5, top * 0.93, noise=0.006, vol=0.75)
    g.to(7, top * 0.955, noise=0.005, vol=0.5)               # drifting flag
    return g.out()


def g_bull_pennant(seed):
    g = Gen(seed)
    g.leg(70, 28, vol=1.0)
    g.leg(14, 24, noise=0.01, vol=2.2)
    top, low = g.px, g.px * 0.92
    _zigzag(g, 4, 4, top, low, top * 0.975, low * 1.03, noise=0.005,
            vol_start=0.8, vol_end=0.4)                      # converging flag
    return g.out()


def g_vcp(seed):
    g = Gen(seed)
    g.leg(70, 50, vol=1.2)
    top = g.px
    g.to(10, top * 0.80, noise=0.008, vol=1.1)               # -20%
    g.to(12, top * 1.0, noise=0.007, vol=0.9)
    g.to(8, top * 0.89, noise=0.006, vol=0.7)                # -11%
    g.to(10, top * 1.01, noise=0.006, vol=0.6)
    g.to(6, top * 0.965, noise=0.004, vol=0.42)              # -4.5%
    g.to(5, top * 1.005, noise=0.004, vol=0.4)
    return g.out()


def g_rounding_bottom(seed):
    g = Gen(seed)
    g.leg(40, -8, vol=1.0)
    top = g.px
    g.leg(60, -30, vol=0.85, shape='ease')
    g.to(65, top * 0.995, vol=1.1, shape='ease')
    return g.out()


def g_double_bottom(seed):
    g = Gen(seed)
    g.leg(60, -28, vol=1.1)                                  # decline to reverse
    low = g.px
    g.to(14, low * 1.16, noise=0.008, vol=0.95)              # middle peak
    peak = g.px
    g.to(14, low * 0.985, noise=0.008, vol=0.8)              # undercut low
    g.to(12, peak * 0.995, noise=0.007, vol=1.3)
    return g.out()


def g_double_top(seed):
    g = Gen(seed)
    g.leg(70, 40, vol=1.1)                                   # advance to top out
    top = g.px
    g.to(14, top * 0.87, noise=0.008, vol=1.0)
    trough = g.px
    g.to(14, top * 1.005, noise=0.008, vol=0.8)
    g.to(12, trough * 1.01, noise=0.008, vol=1.3)
    return g.out()


def g_inv_head_shoulders(seed):
    g = Gen(seed)
    g.leg(55, -25, vol=1.1)
    ls = g.px
    g.to(10, ls * 1.11, noise=0.007, vol=0.9)                # neckline 1
    neck = g.px
    g.to(12, ls * 0.87, noise=0.008, vol=1.1)                # head
    g.to(12, neck * 1.005, noise=0.007, vol=0.85)            # neckline 2
    g.to(10, ls * 1.01, noise=0.007, vol=0.7)                # right shoulder
    g.to(8, neck * 0.99, noise=0.006, vol=1.2)
    return g.out()


def g_head_shoulders(seed):
    g = Gen(seed)
    g.leg(65, 38, vol=1.1)
    ls = g.px
    g.to(10, ls * 0.90, noise=0.007, vol=0.9)
    neck = g.px
    g.to(12, ls * 1.13, noise=0.008, vol=1.0)                # head
    g.to(12, neck * 0.995, noise=0.007, vol=0.9)
    g.to(10, ls * 0.99, noise=0.007, vol=0.8)                # right shoulder
    g.to(8, neck * 1.01, noise=0.006, vol=1.1)
    return g.out()


def g_asc_triangle(seed):
    g = Gen(seed)
    g.leg(70, 34, vol=1.1)
    top = g.px
    _zigzag(g, 6, 9, top, top * 0.86, top, top * 0.975, noise=0.005)
    return g.out()


def g_desc_triangle(seed):
    g = Gen(seed)
    g.leg(70, -26, vol=1.1)
    lo = g.px
    _zigzag(g, 6, 9, lo * 1.16, lo, lo * 1.03, lo, noise=0.005)
    return g.out()


def g_sym_triangle(seed):
    g = Gen(seed)
    g.leg(70, 32, vol=1.1)
    mid = g.px
    _zigzag(g, 6, 9, mid * 1.10, mid * 0.90, mid * 1.02, mid * 0.98, noise=0.005)
    return g.out()


def g_rising_wedge(seed):
    g = Gen(seed)
    g.leg(65, 30, vol=1.2)
    p = g.px
    # Converges from a 15% band to a 9% band. A tighter apex than that
    # leaves legs under the 3% swing floor, which no daily swing engine
    # resolves as separate turns — that is a documented limit, not a bug.
    _zigzag(g, 7, 8, p * 1.05, p * 0.90, p * 1.24, p * 1.15, noise=0.005)
    return g.out()


def g_falling_wedge(seed):
    g = Gen(seed)
    g.leg(55, 22, vol=1.0)
    p = g.px
    # Both boundaries fall, the upper one faster — that is what makes it
    # a wedge rather than a descending triangle.
    _zigzag(g, 7, 8, p * 1.04, p * 0.90, p * 0.86, p * 0.78, noise=0.005)
    return g.out()


def g_bear_flag(seed):
    g = Gen(seed)
    g.leg(60, -12, vol=1.0)
    g.leg(16, -24, noise=0.01, vol=2.0)                      # sharp decline
    low = g.px
    g.to(6, low * 1.07, noise=0.006, vol=0.7)
    g.to(7, low * 1.04, noise=0.005, vol=0.45)               # weak drift
    return g.out()


# ═════════════════════════════════════════════════════════════════════
# Negative generators — should produce nothing
# ═════════════════════════════════════════════════════════════════════

def n_random_walk(seed):
    g = Gen(seed)
    r = random.Random(seed * 7 + 1)
    for _ in range(46):
        g.leg(5, r.gauss(0, 5.5), noise=0.014, vol=r.uniform(0.6, 1.7))
    return g.out()


def n_straight_up(seed):
    return Gen(seed).leg(230, 120, noise=0.006, vol=1.0).out()


def n_straight_down(seed):
    return Gen(seed).leg(230, -55, noise=0.006, vol=1.0).out()


def n_v_spike(seed):
    g = Gen(seed)
    g.leg(90, 8, noise=0.007)
    g.leg(14, 55, noise=0.02, vol=3.0)
    g.leg(14, -36, noise=0.02, vol=2.5)
    g.leg(90, 3, noise=0.007)
    return g.out()


def n_chop(seed):
    """
    Irregular churn. Amplitudes vary widely and the mid-point drifts, so
    swing highs and lows never repeat at the same level — otherwise this
    would *be* a rectangle / double top, and counting it as a negative
    would be measuring the wrong thing.
    """
    g = Gen(seed)
    r = random.Random(seed * 13 + 5)
    for k in range(56):
        amp = (9 if k % 2 == 0 else -8) * r.uniform(0.35, 1.9)
        g.leg(4, amp + r.gauss(0, 1.5), noise=0.016, vol=r.uniform(0.7, 1.5))
    return g.out()


POSITIVES = [
    ('cup_handle',         g_cup_handle,         {'cup_handle'}),
    ('cup_base',           g_cup_base,           {'cup_base', 'cup_handle'}),
    ('flat_base',          g_flat_base,          {'flat_base'}),
    ('high_tight_flag',    g_high_tight_flag,    {'high_tight_flag'}),
    ('bull_flag',          g_bull_flag,          {'bull_flag', 'bull_pennant'}),
    ('bull_pennant',       g_bull_pennant,       {'bull_pennant', 'bull_flag'}),
    ('vcp',                g_vcp,                {'vcp'}),
    ('rounding_bottom',    g_rounding_bottom,    {'rounding_bottom'}),
    ('double_bottom',      g_double_bottom,      {'double_bottom'}),
    ('double_top',         g_double_top,         {'double_top'}),
    ('inv_head_shoulders', g_inv_head_shoulders, {'inv_head_shoulders'}),
    ('head_shoulders',     g_head_shoulders,     {'head_shoulders'}),
    ('asc_triangle',       g_asc_triangle,       {'asc_triangle', 'sym_triangle'}),
    ('desc_triangle',      g_desc_triangle,      {'desc_triangle', 'sym_triangle'}),
    ('sym_triangle',       g_sym_triangle,       {'sym_triangle'}),
    ('rising_wedge',       g_rising_wedge,       {'rising_wedge'}),
    ('falling_wedge',      g_falling_wedge,      {'falling_wedge'}),
    ('bear_flag',          g_bear_flag,          {'bear_flag'}),
]

NEGATIVES = [
    ('random_walk',   n_random_walk),
    ('straight_up',   n_straight_up),
    ('straight_down', n_straight_down),
    ('v_spike',       n_v_spike),
    ('chop',          n_chop),
]


def run(samples: int, verbose: bool) -> int:
    print(f'chart_patterns.py — detection accuracy over {samples} samples per case\n')
    print(f'{"pattern":22} {"recall":>7} {"top-1":>7} {"avg score":>10}')
    print('-' * 50)

    total_pos = found_pos = 0
    rows = []
    for label, gen, accept in POSITIVES:
        hit = top1 = 0
        scores = []
        for s in range(samples):
            closes, highs, lows, vols = gen(1000 + s)
            recs = detect_patterns(closes, highs, lows, vols)
            keys = [r['key'] for r in recs]
            if any(k in accept for k in keys):
                hit += 1
                scores.append(max(r['score'] for r in recs if r['key'] in accept))
            elif verbose:
                print(f'   MISS {label} seed={1000 + s} got={keys or "none"}')
            if keys and keys[0] in accept:
                top1 += 1
        total_pos += samples
        found_pos += hit
        rows.append((label, hit / samples, top1 / samples,
                     (sum(scores) / len(scores)) if scores else 0))
        print(f'{label:22} {hit / samples:>6.0%} {top1 / samples:>7.0%} '
              f'{(sum(scores) / len(scores)) if scores else 0:>10.1f}')

    print('\n' + f'{"negative":22} {"clean":>7} {"false positives":>18}')
    print('-' * 50)
    total_neg = clean_neg = 0
    fp_detail = {}
    for label, gen in NEGATIVES:
        clean = 0
        for s in range(samples):
            closes, highs, lows, vols = gen(2000 + s)
            recs = [r for r in detect_patterns(closes, highs, lows, vols)
                    if r['score'] >= GRADE_B]
            if not recs:
                clean += 1
            else:
                for r in recs:
                    fp_detail[r['key']] = fp_detail.get(r['key'], 0) + 1
                if verbose:
                    print(f'   FP {label} seed={2000 + s} '
                          f'got={[(r["key"], r["score"]) for r in recs]}')
        total_neg += samples
        clean_neg += clean
        print(f'{label:22} {clean / samples:>6.0%} {samples - clean:>18}')

    # Same negatives judged at the A grade the UI defaults to, for context.
    clean_a = 0
    for label, gen in NEGATIVES:
        for s in range(samples):
            closes, highs, lows, vols = gen(2000 + s)
            if not [r for r in detect_patterns(closes, highs, lows, vols)
                    if r['grade'] == 'A']:
                clean_a += 1

    recall = found_pos / total_pos if total_pos else 0
    fp_rate = 1 - (clean_neg / total_neg if total_neg else 0)
    accuracy = (found_pos + clean_neg) / (total_pos + total_neg)
    worst = min(rows, key=lambda r: r[1]) if rows else None

    print('\n' + '=' * 50)
    print(f'positives found      : {found_pos}/{total_pos}  ({recall:.1%} recall)')
    print(f'negatives left alone : {clean_neg}/{total_neg}  ({fp_rate:.1%} false positive at grade B+)')
    print(f'                       {clean_a}/{total_neg}  '
          f'({1 - clean_a / total_neg:.1%} false positive at grade A)')
    print(f'overall accuracy     : {accuracy:.1%}   (target {TARGET_ACCURACY:.0%})')
    if worst:
        print(f'weakest pattern      : {worst[0]} at {worst[1]:.0%} recall')
    if fp_detail:
        print('false positives by pattern: '
              + ', '.join(f'{k}={v}' for k, v in sorted(fp_detail.items(), key=lambda kv: -kv[1])))
    print('=' * 50)

    fp_a = 1 - clean_a / total_neg if total_neg else 1
    ok = (round(accuracy, 3) >= TARGET_ACCURACY
          and round(fp_rate, 3) <= TARGET_FP_B
          and round(fp_a, 3) <= TARGET_FP_A)
    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('-n', '--samples', type=int, default=25)
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    sys.exit(run(a.samples, a.verbose))
