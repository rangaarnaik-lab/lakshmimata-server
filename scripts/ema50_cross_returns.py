#!/usr/bin/env python3
"""List stocks that crossed ABOVE their 50-day EMA within the last N trading
days, and the return they've provided since the cross.

Reads stock_full_history (the same series the chart draws) from Supabase,
computes each symbol's EMA(50) series, finds the most recent cross-above
(previous close at/below EMA50, current close above it), and for crosses that
happened within the last N bars reports the % price change from the cross
bar's close to the latest close.

Usage:
  python scripts/ema50_cross_returns.py                # report last 2 trading days, default
  python scripts/ema50_cross_returns.py --days 1        # only crosses as of today
  python scripts/ema50_cross_returns.py --days 5        # crosses in last 5 trading days
  python scripts/ema50_cross_returns.py --pivot-pocket  # require the cross day to also be a
                                                        #   pocket pivot (up-day > 10/50-day SMAs,
                                                        #   volume beats prior 10-day worst down-day)
  python scripts/ema50_cross_returns.py --min-return 3  # only crosses that gained >=3%
  python scripts/ema50_cross_returns.py --top 20        # print only the top 20 by return
  python scripts/ema50_cross_returns.py --csv out.csv   # also write a CSV

Report only — writes nothing to the database.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

ENV_CANDIDATES = (
    SERVER_ROOT / ".env",
    SERVER_ROOT / "Lakshmimata" / ".env",
    SERVER_ROOT.parent / "Lakshmimata" / ".env",
)
PAGE = 200
EMA_N = 50


def load_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY")
    for env_file in ENV_CANDIDATES:
        if url and key:
            break
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ("SUPABASE_URL", "VITE_SUPABASE_URL") and not url:
                url = v
            if k in ("SUPABASE_SERVICE_KEY", "VITE_SUPABASE_ANON_KEY") and not key:
                key = v
    if not url or not key:
        sys.exit("Missing Supabase URL/key (SUPABASE_URL + SUPABASE_SERVICE_KEY)")
    return url.rstrip("/"), key


def _request(base: str, key: str, path: str) -> list:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(f"{base}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else []


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    return v if isinstance(v, list) else []


def ema_arr(prices: list, n: int) -> list:
    """Full EMA series, same algorithm as live_scan.ema_arr."""
    result = [None] * len(prices)
    if len(prices) < n:
        return result
    k = 2 / (n + 1)
    e = sum(prices[:n]) / n
    result[n - 1] = round(e, 2)
    for i in range(n, len(prices)):
        e = prices[i] * k + e * (1 - k)
        result[i] = round(e, 2)
    return result


def sma(prices: list, n: int):
    """Simple moving average over the trailing n bars (live_scan.sma)."""
    if len(prices) < n:
        return None
    return sum(prices[-n:]) / n


def is_pocket_pivot(prices: list, volumes: list, idx: int) -> bool:
    """Mirrors live_scan.detect_pp's is_pp_at: an up-day whose volume beats the
    worst down-day volume of the preceding 10 bars, while price sits above its
    10-day and 50-day SMAs and within 8% above the 10-day SMA."""
    if idx < 11:
        return False
    today, yesterday = prices[idx], prices[idx - 1]
    if today <= yesterday:
        return False
    ma10 = sma(prices[:idx + 1], 10)
    ma50 = sma(prices[:idx + 1], min(50, idx + 1))
    if not ma10 or not ma50:
        return False
    if not (today > ma10 and today < ma10 * 1.08 and today > ma50):
        return False
    p10 = prices[idx - 10:idx]
    v10 = volumes[idx - 10:idx]
    max_down = max((v10[i] for i in range(1, len(p10)) if p10[i] < p10[i - 1]), default=0)
    if max_down == 0:
        max_down = sum(v10) / len(v10) if v10 else 0
    return volumes[idx] > max_down


def last_cross_above(prices: list, ema: list) -> int | None:
    """Index of the most recent bar that closed above EMA(n) while the prior
    bar closed at/below it. Returns None if no cross exists in the series."""
    best = None
    for i in range(1, len(prices)):
        prev_close = prices[i - 1]
        prev_ema = ema[i - 1]
        close = prices[i]
        cur_ema = ema[i]
        if prev_close is None or prev_ema is None or close is None or cur_ema is None:
            continue
        if prev_close <= prev_ema and close > cur_ema:
            best = i  # later bar wins
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=2,
                        help="crosses within the last N trading days (default 2)")
    parser.add_argument("--min-return", type=float, default=None,
                        help="only include crosses with return >= this %%")
    parser.add_argument("--max-return", type=float, default=None,
                        help="only include crosses with return <= this %%")
    parser.add_argument("--top", type=int, default=None,
                        help="print only the top N by return")
    parser.add_argument("--pivot-pocket", action="store_true",
                        help="only include crosses where the cross day is also a "
                             "pocket pivot (up-day above its 10/50-day SMAs, volume "
                             "beating the prior 10-day worst down-day)")
    parser.add_argument("--csv", default=None, help="optional CSV output path")
    args = parser.parse_args()
    if args.days < 1:
        sys.exit("--days must be >= 1")
    mode = "(cross day must ALSO be a pocket pivot)" if args.pivot_pocket else ""

    base, key = load_env()
    print(f"Scanning stock_full_history for EMA{EMA_N} crosses within the last "
          f"{args.days} trading day(s) {mode}…")

    hits = []
    offset = 0
    short_history = 0
    scanned = 0
    while True:
        rows = _request(
            base, key,
            "/rest/v1/stock_full_history"
            "?select=sym,dates,opens,highs,lows,prices,volumes"
            f"&limit={PAGE}&offset={offset}",
        )
        if not rows:
            break
        for row in rows:
            sym = str(row.get("sym") or "").strip().upper()
            if not sym:
                continue
            closes = as_list(row.get("prices"))
            volumes = as_list(row.get("volumes"))
            dates = as_list(row.get("dates"))
            if len(closes) < EMA_N + 1 or len(dates) != len(closes) or len(volumes) != len(closes):
                short_history += 1
                continue
            scanned += 1
            ema = ema_arr(closes, EMA_N)
            idx = last_cross_above(closes, ema)
            if idx is None:
                continue
            bars_since = len(closes) - 1 - idx
            if bars_since >= args.days:
                continue  # crossed too long ago to be "within the last N days"
            # Optional pocket-pivot gate: the cross bar itself must be a pivot.
            if args.pivot_pocket and not is_pocket_pivot(closes, volumes, idx):
                continue
            cross_close = closes[idx]
            latest_close = closes[-1]
            if not cross_close:
                continue
            ret = (latest_close / cross_close - 1.0) * 100.0
            if args.min_return is not None and ret < args.min_return:
                continue
            if args.max_return is not None and ret > args.max_return:
                continue
            cross_date = dates[idx] if idx < len(dates) else None
            hits.append({
                "sym": sym,
                "bars_since": bars_since,
                "cross_date": cross_date,
                "cross_close": cross_close,
                "latest_close": latest_close,
                "ema50": ema[-1],
                "pct_above_ema50": (latest_close / ema[-1] - 1.0) * 100.0 if ema[-1] else None,
                "ret_pct": round(ret, 2),
                "pocket_pivot": bool(is_pocket_pivot(closes, volumes, idx)),
            })
        if len(rows) < PAGE:
            break
        offset += PAGE

    hits.sort(key=lambda h: h["ret_pct"], reverse=True)
    if args.top:
        hits = hits[: args.top]

    print(f"\nScanned {scanned} symbols ({short_history} skipped for "
          f"<{EMA_N + 1} bars or missing dates).")
    print(f"Crossed above EMA{EMA_N} within last {args.days} trading day(s): {len(hits)}")

    if not hits:
        print("\nNo symbols matched.")
        return

    header = ("SYM     cross_date   bars_since  cross_close  latest  ret%   "
              "%vsEMA50  PP")
    print("\n" + header)
    print("-" * len(header))
    for h in hits:
        ema_text = f"{h['pct_above_ema50']:.1f}" if h["pct_above_ema50"] is not None else "—"
        pp_text = "P" if h["pocket_pivot"] else "·"
        print(f"{h['sym']:<7} {str(h['cross_date']):<12} {h['bars_since']:<10} "
              f"{h['cross_close']:<12.2f} {h['latest_close']:<7.2f} "
              f"{h['ret_pct']:>6.2f} {ema_text:>7} {pp_text}")

    rets = [h["ret_pct"] for h in hits]
    if rets:
        avg = sum(rets) / len(rets)
        up = sum(1 for r in rets if r > 0)
        down = sum(1 for r in rets if r < 0)
        print(f"\nSummary: avg return {avg:.2f}% | "
              f"{up} up / {down} down / {len(rets) - up - down} flat")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(hits[0].keys()))
            w.writeheader()
            w.writerows(hits)
        print(f"CSV written to {args.csv}")


if __name__ == "__main__":
    main()