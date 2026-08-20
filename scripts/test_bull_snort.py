#!/usr/bin/env python3
"""Regression checks for the scanner/alert Bull Snort contract."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from bull_snort import detect_bull_snort  # noqa: E402


def series(*, last_close=106.5, last_volume=4.0):
    closes = [100.0] * 49 + [last_close]
    volumes = [1.0] * 49 + [last_volume]
    highs = [110.0] * 50
    lows = [100.0] * 50
    opens = [100.0] * 50
    return opens, highs, lows, closes, volumes


class BullSnortRuleTests(unittest.TestCase):
    def test_passes_three_x_50_bar_dcr_65_and_up_close(self):
        result = detect_bull_snort(*series())
        self.assertTrue(result['is_bull_snort'])
        self.assertGreaterEqual(result['bull_snort_vol_ratio'], 3.0)

    def test_rejects_volume_below_three_x_50_bar_average(self):
        result = detect_bull_snort(*series(last_volume=3.0))
        self.assertFalse(result['is_bull_snort'])

    def test_rejects_dcr_below_65_percent(self):
        result = detect_bull_snort(*series(last_close=106.4))
        self.assertFalse(result['is_bull_snort'])

    def test_rejects_close_not_above_prior_close(self):
        opens, highs, lows, closes, volumes = series(last_close=100.0)
        result = detect_bull_snort(opens, highs, lows, closes, volumes)
        self.assertFalse(result['is_bull_snort'])

    def test_requires_full_50_bar_window(self):
        result = detect_bull_snort(
            [100.0] * 49,
            [110.0] * 49,
            [100.0] * 49,
            [100.0] * 48 + [106.5],
            [1.0] * 48 + [4.0],
        )
        self.assertFalse(result['is_bull_snort'])


if __name__ == '__main__':
    unittest.main()
