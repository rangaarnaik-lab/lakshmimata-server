"""
Intraday 1-minute OHLCV store for Our Chart (client rolls up to 3/5/15).

Kill switch (no code delete needed):
  Railway env  ENABLE_INTRADAY_1M=0   → stop writing + publish features.intraday_1m=false
  Railway env  ENABLE_INTRADAY_1M=1   → write bars + publish features.intraday_1m=true

Frontend hides 1/3/5/15 when scan_meta.features.intraday_1m is false
(or VITE_ENABLE_INTRADAY_CHART=0). Existing daily charts are unaffected.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import aiohttp

from shared import IST, SUPABASE_KEY, SUPABASE_URL, is_market_open

log = logging.getLogger('pocketrs')

# ── Feature flag ──────────────────────────────────────────────────────
def _env_truthy(name: str, default: str = '0') -> bool:
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def is_intraday_1m_enabled() -> bool:
    """Master kill switch. Default OFF until explicitly enabled on Railway."""
    return _env_truthy('ENABLE_INTRADAY_1M', '0')


def feature_flags_payload() -> dict:
    """Published on scan_meta.features so the UI can hide intervals live."""
    on = is_intraday_1m_enabled()
    return {
        'intraday_1m': on,
        'intraday_intervals': ['1', '3', '5', '15', '30', '60'] if on else [],
    }


# ── Module state ──────────────────────────────────────────────────────
_open_bars: dict = {}           # sym -> current-minute bar
_prev_session_vol: dict = {}    # sym -> last session volume
_table_ok = None                # None | True | False
_retention_day = None
RETENTION_DAYS = int(os.environ.get('INTRADAY_1M_RETENTION_DAYS', '30') or '30')


def _headers(prefer: str | None = None) -> dict:
    h = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }
    if prefer:
        h['Prefer'] = prefer
    return h


async def ensure_table(session: aiohttp.ClientSession) -> bool:
    """Probe stock_intraday_1m. Does not CREATE — run 013_stock_intraday_1m.sql."""
    global _table_ok
    if not is_intraday_1m_enabled():
        _table_ok = False
        return False
    try:
        async with session.get(
            f'{SUPABASE_URL}/rest/v1/stock_intraday_1m?select=sym&limit=1',
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status in (200, 206):
                _table_ok = True
                log.info('✅ stock_intraday_1m table OK (feature ON)')
                return True
            text = await r.text()
            _table_ok = False
            log.error('❌ stock_intraday_1m missing: %s %s', r.status, text[:200])
    except Exception as e:
        _table_ok = False
        log.error('❌ stock_intraday_1m probe failed: %s', e)
    log.error('   Run 013_stock_intraday_1m.sql, then: NOTIFY pgrst, \'reload schema\';')
    log.error('   Or set ENABLE_INTRADAY_1M=0 to silence this.')
    return False


async def _upsert(session: aiohttp.ClientSession, rows: list) -> bool:
    if not rows:
        return True
    url = f'{SUPABASE_URL}/rest/v1/stock_intraday_1m?on_conflict=sym,ts'
    headers = _headers('resolution=merge-duplicates')
    failures = 0
    chunk_size = 500
    sem = __import__('asyncio').Semaphore(4)

    async def one(chunk):
        nonlocal failures
        async with sem:
            try:
                async with session.post(
                    url, headers=headers, json=chunk,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status not in (200, 201, 204):
                        failures += 1
                        text = await r.text()
                        log.warning('stock_intraday_1m upsert failed: %s %s', r.status, text[:300])
            except Exception as e:
                failures += 1
                log.error('stock_intraday_1m upsert error: %s', e)

    import asyncio
    chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
    await asyncio.gather(*[one(c) for c in chunks])
    return failures == 0


async def retain(session: aiohttp.ClientSession) -> None:
    global _retention_day
    if not is_intraday_1m_enabled():
        return
    today = datetime.now(IST).strftime('%Y-%m-%d')
    if _retention_day == today:
        return
    cutoff = (datetime.now(IST) - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        async with session.delete(
            f'{SUPABASE_URL}/rest/v1/stock_intraday_1m?ts=lt.{cutoff}',
            headers=_headers('return=minimal'),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            if r.status in (200, 204):
                _retention_day = today
                log.info('🧹 stock_intraday_1m: dropped bars older than %sd', RETENTION_DAYS)
            else:
                text = await r.text()
                log.warning('stock_intraday_1m retention failed: %s %s', r.status, text[:200])
    except Exception as e:
        log.warning('stock_intraday_1m retention error: %s', e)


async def persist_bars(session: aiohttp.ClientSession, live_data: dict) -> None:
    """No-op when ENABLE_INTRADAY_1M is off. Safe to call every scan."""
    global _table_ok
    if not is_intraday_1m_enabled():
        return
    if not live_data or not is_market_open():
        return
    if _table_ok is False:
        return
    if _table_ok is None:
        if not await ensure_table(session):
            return

    now = datetime.now(IST)
    minute_ts = now.replace(second=0, microsecond=0)
    ts_iso = minute_ts.isoformat()
    rows = []

    for sym, quote in live_data.items():
        if not isinstance(quote, dict):
            continue
        price = quote.get('last_price')
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        try:
            sess_vol = int(quote.get('volume') or 0)
        except (TypeError, ValueError):
            sess_vol = 0

        prev = _prev_session_vol.get(sym)
        if prev is None:
            delta = 0
        elif sess_vol >= prev:
            delta = sess_vol - prev
        else:
            delta = 0
        _prev_session_vol[sym] = sess_vol

        bar = _open_bars.get(sym)
        if not bar or bar.get('ts') != ts_iso:
            bar = {
                'sym': sym,
                'ts': ts_iso,
                'open': round(price, 4),
                'high': round(price, 4),
                'low': round(price, 4),
                'close': round(price, 4),
                'volume': int(delta),
            }
            _open_bars[sym] = bar
        else:
            bar['high'] = round(max(bar['high'], price), 4)
            bar['low'] = round(min(bar['low'], price), 4)
            bar['close'] = round(price, 4)
            bar['volume'] = int(bar.get('volume', 0) + delta)

        rows.append({
            'sym': bar['sym'],
            'ts': bar['ts'],
            'open': bar['open'],
            'high': bar['high'],
            'low': bar['low'],
            'close': bar['close'],
            'volume': bar['volume'],
        })

    if not rows:
        return

    ok = await _upsert(session, rows)
    if ok:
        log.info('⏱ stock_intraday_1m: %s bars @ %s', len(rows), minute_ts.strftime('%H:%M'))
    else:
        _table_ok = None

    await retain(session)


async def on_startup(session: aiohttp.ClientSession) -> None:
    if not is_intraday_1m_enabled():
        log.info('⏸ Intraday 1m store DISABLED (ENABLE_INTRADAY_1M=0)')
        return
    log.info('▶ Intraday 1m store ENABLED (retention=%sd)', RETENTION_DAYS)
    await ensure_table(session)
