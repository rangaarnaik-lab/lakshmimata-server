#!/usr/bin/env python3
"""Bull Snort detection — the single Python source of truth.

Dependency-free on purpose so both the live scan and one-off backfill
scripts can import it.

Thresholds mirror the frontend's Lakshmi Volume pane
(snortAvgLen / bullSnortMult / bullSnortDcr) and
src/scanners/chartAnalysis.js BULL_SNORT_DEFAULTS. If these drift, the
alert fires on bars where the chart draws no icon.
"""
from __future__ import annotations

BULL_SNORT_AVG_LEN = 50   # volume SMA length, includes the current bar
BULL_SNORT_MIN_REL_VOL = 3.0
BULL_SNORT_MIN_DCR = 0.65  # close must sit in the top 35% of the bar's range


def detect_bull_snort(opens: list, highs: list, lows: list, closes: list, volumes: list) -> dict:
    """Bull Snort on the latest bar of the given series.

    Same rules as the chart (Lakshmi Volume pane / detectBullSnortDays):
    relative volume >= 3x the 50-bar volume SMA, close above the prior
    close, and a daily closing range (DCR) of at least 65%.

    `opens` is unused — the up-day test is close vs prior close, matching
    the chart — but stays in the signature for existing callers.
    """
    return detect_bull_snort_at(opens, highs, lows, closes, volumes, -1)


def detect_bull_snort_at(opens: list, highs: list, lows: list, closes: list,
                         volumes: list, index: int = -1) -> dict:
    """Same rule, evaluated at any bar. `index` may be negative."""
    result = {'is_bull_snort': False, 'bull_snort_vol_ratio': 0.0}
    n = len(closes) if closes else 0
    if not n or not volumes or len(volumes) < n:
        return result
    i = n + index if index < 0 else index
    if i < 1 or i >= n or i < BULL_SNORT_AVG_LEN - 1:
        return result

    def _at(arr, idx, fallback):
        """Prefer last-n aligned series even when lengths drifted after trim."""
        if not arr:
            return fallback
        if len(arr) == n:
            v = arr[idx]
            return fallback if v is None else v
        if len(arr) > n:
            v = arr[len(arr) - n + idx]
            return fallback if v is None else v
        # shorter than closes — use last available if this is the final bar
        if idx == n - 1 and arr:
            v = arr[-1]
            return fallback if v is None else v
        return fallback

    cl = closes[i]
    vol = volumes[i]
    prev_cl = closes[i - 1]
    if cl is None or vol is None or prev_cl is None:
        return result
    hi = _at(highs, i, cl)
    lo = _at(lows, i, cl)

    # Volume ratio is reported even when the price gates fail, so the
    # screener can still show how heavy the session was.
    window = volumes[i - (BULL_SNORT_AVG_LEN - 1):i + 1]
    if len(window) < BULL_SNORT_AVG_LEN or any(v is None for v in window):
        return result
    avg = sum(window) / BULL_SNORT_AVG_LEN
    if avg <= 0:
        return result
    ratio = vol / avg
    result['bull_snort_vol_ratio'] = round(ratio, 2)

    if cl <= prev_cl:
        return result
    day_range = hi - lo
    # No range (illiquid bar, or a live quote with H==L) means DCR is
    # undefined; the chart draws no icon there, so don't fire either.
    if day_range <= 0:
        return result
    if (cl - lo) / day_range < BULL_SNORT_MIN_DCR:
        return result

    result['is_bull_snort'] = ratio >= BULL_SNORT_MIN_REL_VOL
    return result
