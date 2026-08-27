"""Private daily OHLCV on the worker disk — used only to compute RS/stage.

This is not a redistribution path. The frontend never reads these files.
Charts and live LTP stay on each user's own broker token.

On Railway the container disk is wiped on every deploy unless you mount a
volume. Set HISTORY_DIR to that mount (e.g. /data/ohlcv).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from shared import IST, historical_cache, history_dates_cache, opens_cache

log = logging.getLogger('pocketrs')

_SAFE_SYM = re.compile(r'[^A-Za-z0-9._-]+')


def history_dir() -> Path:
    raw = os.getenv('HISTORY_DIR', 'data/ohlcv').strip() or 'data/ohlcv'
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def persist_ohlcv_to_supabase() -> bool:
    return os.getenv('STORE_OHLCV_IN_SUPABASE', '0').strip() not in (
        '0', 'false', 'False', 'no', 'NO',
    )


def seed_ohlcv_from_supabase() -> bool:
    return os.getenv('SEED_OHLCV_FROM_SUPABASE', '1').strip() not in (
        '0', 'false', 'False', 'no', 'NO',
    )


def _path_for(sym: str) -> Path:
    clean = _SAFE_SYM.sub('_', str(sym or '').strip().upper()) or '_'
    return history_dir() / f'{clean}.json.gz'


def persist_symbol(sym: str, dates, prices, volumes, highs, lows, opens) -> None:
    if not sym or not prices:
        return
    payload = {
        'sym': str(sym).upper(),
        'dates': list(dates or []),
        'prices': list(prices or []),
        'volumes': [v if v is not None else 0 for v in (volumes or [])],
        'highs': list(highs or []),
        'lows': list(lows or []),
        'opens': list(opens or []),
        'saved_at': datetime.now(IST).isoformat(),
    }
    dest = _path_for(sym)
    tmp = dest.with_suffix(dest.suffix + '.tmp')
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    tmp.write_bytes(gzip.compress(raw, compresslevel=6))
    tmp.replace(dest)


def persist_cache_symbol(sym: str) -> None:
    hist = historical_cache.get(sym) or {}
    persist_symbol(
        sym,
        history_dates_cache.get(sym, []),
        hist.get('prices', []),
        hist.get('volumes', []),
        hist.get('highs', []),
        hist.get('lows', []),
        opens_cache.get(sym, []),
    )


def persist_cache_symbols(symbols) -> int:
    n = 0
    for sym in symbols:
        if sym in historical_cache:
            persist_cache_symbol(sym)
            n += 1
    return n


def persist_db_rows(rows: list) -> int:
    """Same payload shape as the old stock_full_history upsert."""
    n = 0
    for row in rows or []:
        sym = row.get('sym')
        if not sym:
            continue
        def parse(v):
            if v is None:
                return []
            return json.loads(v) if isinstance(v, str) else v
        persist_symbol(
            sym,
            parse(row.get('dates')),
            parse(row.get('prices')),
            parse(row.get('volumes')),
            parse(row.get('highs')),
            parse(row.get('lows')),
            parse(row.get('opens')),
        )
        n += 1
    return n


def _install(sym, dates, prices, volumes, highs, lows, opens) -> bool:
    if not prices or len(prices) < 100:
        return False
    highs = [h if h is not None else p for h, p in zip(highs or prices, prices)]
    lows = [l if l is not None else p for l, p in zip(lows or prices, prices)]
    historical_cache[sym] = {
        'prices': prices,
        'volumes': [v if v is not None else 0 for v in (volumes or [0] * len(prices))],
        'highs': highs,
        'lows': lows,
    }
    history_dates_cache[sym] = dates or []
    opens_cache[sym] = opens or []
    return True


def symbols_needing_fetch(universe: list) -> list:
    """Symbols in RAM that are missing, thin, or older than a week."""
    today = datetime.now(IST).date()
    out = []
    for sym in universe:
        hist = historical_cache.get(sym) or {}
        prices = hist.get('prices') or []
        if len(prices) < 100:
            out.append(sym)
            continue
        dates = history_dates_cache.get(sym) or []
        last_date_str = dates[-1] if dates else None
        if not last_date_str:
            out.append(sym)
            continue
        try:
            last_date = datetime.strptime(str(last_date_str)[:10], '%Y-%m-%d').date()
            if (today - last_date).days > 7:
                out.append(sym)
        except Exception:
            out.append(sym)
    return out


def load_all_ohlcv_from_disk(universe: list) -> list:
    """Load private history into RAM. Returns symbols that still need a fetch."""
    today = datetime.now(IST).date()
    loaded = 0
    found = set()
    stale_or_missing = []
    folder = history_dir()

    for path in folder.glob('*.json.gz'):
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode('utf-8'))
        except Exception:
            continue
        sym = str(payload.get('sym') or path.stem).upper()
        dates = payload.get('dates') or []
        prices = payload.get('prices') or []
        if not _install(
            sym, dates, prices,
            payload.get('volumes') or [],
            payload.get('highs') or [],
            payload.get('lows') or [],
            payload.get('opens') or [],
        ):
            stale_or_missing.append(sym)
            continue
        found.add(sym)
        loaded += 1
        last_date_str = dates[-1] if dates else None
        is_stale = True
        if last_date_str:
            try:
                last_date = datetime.strptime(str(last_date_str)[:10], '%Y-%m-%d').date()
                is_stale = (today - last_date).days > 7
            except Exception:
                is_stale = True
        if is_stale:
            stale_or_missing.append(sym)

    missing = [s for s in universe if s not in found]
    stale_or_missing.extend(missing)
    log.info(
        f"📦 Loaded {loaded} stocks from local disk {folder} — "
        f"{len(stale_or_missing)} need a fetch (missing or stale)"
    )
    return stale_or_missing
