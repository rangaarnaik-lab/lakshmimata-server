"""
Download daily index history from Yahoo Finance's public chart API and write
CSV files. Yahoo removed the "Download" button from the history page, but the
chart endpoint below still works without a login or API key.

Usage:
    python scripts/fetch_index_history_csv.py
    python scripts/fetch_index_history_csv.py --years 15 --out data/indices
    python scripts/fetch_index_history_csv.py --index "Midcap 150"

Output: one CSV per index with columns date,open,high,low,close,volume
(oldest row first), ready for a one-time load into index_price_history.

Note: Yahoo silently returns monthly bars for range=max, so this requests an
explicit period1/period2 window instead, which keeps interval=1d honest.
"""

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Yahoo symbols. Fallbacks are tried in order — Yahoo renames index tickers
# occasionally and a stale symbol returns 404 rather than an empty series.
INDEX_TICKERS = {
    "Nifty 50": ["^NSEI"],
    "Midcap 150": ["NIFTYMIDCAP150.NS", "^NIMDCP150"],
    "Smallcap 250": ["NIFTYSMLCAP250.NS", "^NSMCP250"],
}

CHART_URL = ("https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
             "?interval=1d&period1={start}&period2={end}")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def fetch_series(ticker, start_epoch, end_epoch, timeout=20):
    url = CHART_URL.format(
        ticker=urllib.parse.quote(ticker, safe=""),
        start=start_epoch,
        end=end_epoch,
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)

    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    block = result[0]
    stamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]

    rows = []
    for i, ts in enumerate(stamps):
        close = (quote.get("close") or [None] * len(stamps))[i]
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append({
            "date": day,
            "open": (quote.get("open") or [None] * len(stamps))[i],
            "high": (quote.get("high") or [None] * len(stamps))[i],
            "low": (quote.get("low") or [None] * len(stamps))[i],
            "close": close,
            "volume": (quote.get("volume") or [None] * len(stamps))[i],
        })
    return rows


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=20,
                    help="How far back to request (default: 20)")
    ap.add_argument("--out", default="data/indices", help="Output directory")
    ap.add_argument("--index", action="append",
                    help="Index name to fetch; repeatable. Default: all")
    args = ap.parse_args()

    end_epoch = int(time.time())
    start_epoch = end_epoch - args.years * 366 * 24 * 3600
    wanted = args.index or list(INDEX_TICKERS)
    for name in wanted:
        tickers = INDEX_TICKERS.get(name)
        if not tickers:
            print(f"Unknown index '{name}'. Known: {', '.join(INDEX_TICKERS)}")
            continue

        rows = []
        for ticker in tickers:
            try:
                rows = fetch_series(ticker, start_epoch, end_epoch)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
                print(f"  {name} via {ticker}: {exc}")
                rows = []
            if rows:
                print(f"  {name}: {len(rows)} days from {ticker}")
                break
            time.sleep(0.5)

        if not rows:
            print(f"  {name}: no data (try fewer --years or a different source)")
            continue

        slug = name.lower().replace(" ", "_")
        path = os.path.join(args.out, f"{slug}.csv")
        write_csv(path, rows)
        print(f"  saved {path} ({rows[0]['date']} to {rows[-1]['date']})")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
