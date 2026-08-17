#!/usr/bin/env python3
"""
Backfill stock_intraday_1m from Upstox 1-minute historical candles.

Live scans only write bars while the server is running. This one-shot job
fills the last ~30 calendar days (Upstox 1m max window ≈ 1 month) so Our
Chart 1/3/5/15/30/60 intervals have history immediately.

Env (same as live_scan):
  UPSTOX_ANALYTICS_TOKEN
  SUPABASE_URL
  SUPABASE_SERVICE_KEY

Usage:
  cd lakshmimata-server
  python scripts/backfill_intraday_1m.py
  python scripts/backfill_intraday_1m.py --days 30 --concurrency 6
  python scripts/backfill_intraday_1m.py --sym RELIANCE --sym TCS
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

# Allow `python scripts/backfill_intraday_1m.py` from repo root or scripts/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import (  # noqa: E402
    ALL_STOCKS,
    ANALYTICS_TOKEN,
    IST,
    instrument_key_map,
    load_instrument_master,
)
from intraday_1m import _upsert, ensure_table, is_intraday_1m_enabled  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('backfill_1m')


def _encode_key(key: str) -> str:
    return key.replace('|', '%7C').replace(' ', '%20').replace('&', '%26')


def _normalize_ts(raw) -> str | None:
    """Candle ts → IST minute ISO matching live persist_bars."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        # Upstox: "2024-01-15T09:15:00+05:30" or "...Z"
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)
        return dt.replace(second=0, microsecond=0).isoformat()
    except Exception:
        return None


async def fetch_1m_candles(
    session: aiohttp.ClientSession,
    sym: str,
    instrument_key: str,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """
    GET /v2/historical-candle/{key}/1minute/{to}/{from}
    Candle: [ts, open, high, low, close, volume, oi], newest first.
    """
    encoded = _encode_key(instrument_key)
    url = f'https://api.upstox.com/v2/historical-candle/{encoded}/1minute/{to_date}/{from_date}'
    headers = {
        'Authorization': f'Bearer {ANALYTICS_TOKEN}',
        'Accept': 'application/json',
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                text = await r.text()
                if r.status == 404:
                    return []
                log.warning('%s fetch failed: %s %s', sym, r.status, text[:180])
                return []
            data = await r.json()
            candles = list(reversed(data.get('data', {}).get('candles', []) or []))
    except Exception as e:
        log.warning('%s fetch error: %s', sym, e)
        return []

    rows = []
    for c in candles:
        if not c or len(c) < 6:
            continue
        ts = _normalize_ts(c[0])
        if not ts:
            continue
        try:
            o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            vol = int(c[5] or 0)
        except (TypeError, ValueError):
            continue
        if cl <= 0:
            continue
        rows.append({
            'sym': sym,
            'ts': ts,
            'open': round(o, 4),
            'high': round(h, 4),
            'low': round(l, 4),
            'close': round(cl, 4),
            'volume': vol,
        })
    return rows


async def backfill_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    sym: str,
    from_date: str,
    to_date: str,
) -> tuple[str, int]:
    async with sem:
        key = instrument_key_map.get(sym) or f'NSE_EQ|{sym}'
        rows = await fetch_1m_candles(session, sym, key, from_date, to_date)
        if not rows:
            return sym, 0
        ok = await _upsert(session, rows)
        return sym, len(rows) if ok else -1


async def run(args: argparse.Namespace) -> None:
    if not is_intraday_1m_enabled():
        log.error('ENABLE_INTRADAY_1M is off — set ENABLE_INTRADAY_1M=1 or unset it')
        sys.exit(1)

    to_dt = datetime.now(IST).date()
    from_dt = to_dt - timedelta(days=max(1, args.days))
    to_date = to_dt.strftime('%Y-%m-%d')
    from_date = from_dt.strftime('%Y-%m-%d')

    async with aiohttp.ClientSession() as session:
        if not await ensure_table(session):
            log.error('stock_intraday_1m table missing — run 013_stock_intraday_1m.sql first')
            sys.exit(1)

        await load_instrument_master(session)

        if args.sym:
            symbols = [s.upper() for s in args.sym]
        else:
            symbols = [s for s in ALL_STOCKS if s in instrument_key_map] or list(ALL_STOCKS)

        if args.limit and args.limit > 0:
            symbols = symbols[: args.limit]

        log.info(
            'Backfilling 1m for %s symbols, %s → %s (concurrency=%s)',
            len(symbols), from_date, to_date, args.concurrency,
        )

        sem = asyncio.Semaphore(max(1, args.concurrency))
        ok_syms = 0
        total_bars = 0
        fail = 0
        empty = 0
        done = 0

        # Process in batches so gather doesn't hold millions of row refs at once
        batch_size = max(1, args.concurrency * 4)
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            results = await asyncio.gather(*[
                backfill_one(session, sem, sym, from_date, to_date)
                for sym in batch
            ])
            for sym, n in results:
                done += 1
                if n > 0:
                    ok_syms += 1
                    total_bars += n
                elif n == 0:
                    empty += 1
                else:
                    fail += 1
            log.info(
                '  progress %s/%s — bars=%s ok=%s empty=%s fail=%s',
                done, len(symbols), total_bars, ok_syms, empty, fail,
            )
            await asyncio.sleep(0.25)

        log.info(
            '✅ 1m backfill done: %s symbols with data, %s bars, empty=%s fail=%s',
            ok_syms, total_bars, empty, fail,
        )


def main() -> None:
    p = argparse.ArgumentParser(description='Backfill stock_intraday_1m from Upstox 1m candles')
    p.add_argument('--days', type=int, default=30, help='Calendar days back (default 30, Upstox ~1 month max)')
    p.add_argument('--concurrency', type=int, default=4, help='Parallel symbol fetches')
    p.add_argument('--sym', action='append', help='Limit to symbol(s); repeatable')
    p.add_argument('--limit', type=int, default=0, help='Only first N symbols (smoke test)')
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
