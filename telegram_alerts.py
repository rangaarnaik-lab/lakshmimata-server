"""Personal Telegram alerts: link via /start <code>, fan-out labeled fires."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from shared import IST, SUPABASE_KEY, SUPABASE_URL, TELEGRAM_BOT_TOKEN

log = logging.getLogger('pocketrs')

TELEGRAM_BOT_USERNAME = os.getenv('TELEGRAM_BOT_USERNAME', '').lstrip('@').strip()
_TG_API = 'https://api.telegram.org/bot{token}/{method}'
_update_offset = 0
_last_poll = 0.0
_last_publish_settings = 0.0
_last_conflict_log = 0.0


def pref_keys_for_fire_type(fire_type: str) -> list[str]:
    """Keep in sync with App.jsx alertPrefKeysForFireType."""
    t = fire_type or ''
    keys = []
    if re.search(r'\bHY\b', t):
        keys.append('hy')
    if re.search(r'\bHT\b', t):
        keys.append('ht')
    if re.search(r'\bPP\b', t):
        keys.append('pp')
    if re.search(r'Bull\s*Snort', t, re.I):
        keys.append('bullsnort')
    if re.search(r'Squeeze|VCP', t, re.I):
        keys.append('squeeze')
    if re.search(r'Stage\s*2|\bS2\b', t, re.I):
        keys.append('stage2')
    if re.search(r'Guppy', t, re.I):
        keys.append('guppy')
    if re.search(r'RS\s*[>≥]=?\s*70', t, re.I):
        keys.append('rs70')
    return keys


def fire_type_enabled(fire_type: str, prefs: dict) -> bool:
    keys = pref_keys_for_fire_type(fire_type)
    if not keys:
        return True
    return any(prefs.get(k) is not False for k in keys)


TELEGRAM_MAX_TYPES = 2


def telegram_type_allowed(fire_type: str, prefs: dict) -> bool:
    """Telegram can be narrowed to at most TELEGRAM_MAX_TYPES signals.

    An empty selection means "whatever the in-app 🔔 menu says", which is
    what every existing user has, so nobody goes quiet after this ships.
    """
    picks = prefs.get('telegramTypes')
    if isinstance(picks, list) and picks:
        wanted = {str(p) for p in picks[:TELEGRAM_MAX_TYPES]}
        return bool(set(pref_keys_for_fire_type(fire_type)) & wanted)
    return fire_type_enabled(fire_type, prefs)


def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }


def _esc(v) -> str:
    return html.escape('' if v is None else str(v))


def format_alert_html(fire: dict) -> str:
    kind = fire.get('fire_type') or 'Alert'
    sym = (fire.get('sym') or '').upper()
    rs = fire.get('rs_tv') if fire.get('rs_tv') is not None else fire.get('rs')
    chg = fire.get('chg_pct')
    price = fire.get('last_price')
    sector = fire.get('sector') or ''
    chg_s = ''
    if isinstance(chg, (int, float)):
        chg_s = f"{chg:+.1f}%"
    price_s = ''
    if isinstance(price, (int, float)) and price > 0:
        price_s = f"₹{price:,.0f}" if price >= 100 else f"₹{price:g}"
    bits = []
    if rs is not None:
        bits.append(f"RS {_esc(rs)}")
    if chg_s:
        bits.append(_esc(chg_s))
    meta = '   '.join(bits)
    loc = ' · '.join(x for x in (sector, price_s) if x)
    return (
        f"<b>Lakshmimata</b> · {_esc(kind)}\n"
        f"<b>{_esc(sym)}</b>"
        + (f"   {meta}" if meta else "")
        + (f"\n{_esc(loc)}" if loc else "")
        + "\n<i>Research alert — not advice</i>"
    )


# One digest per user (10 lines in 1 DM), paced at ~25/sec so 1,000 users
# finish in ~40s instead of 10,000 individual sends (~167/sec, Telegram 429).
_TG_SEND_PER_SEC = 25
_TG_DIGEST_LINES = 10
_send_q: asyncio.Queue = asyncio.Queue()
_drain_lock = asyncio.Lock()
_drain_running = False


async def telegram_api(session: aiohttp.ClientSession, method: str, payload=None, params=None):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = _TG_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        if payload is not None:
            async with session.post(url, json=payload, timeout=timeout) as r:
                data = await r.json(content_type=None)
        else:
            async with session.get(url, params=params, timeout=timeout) as r:
                data = await r.json(content_type=None)
        if not data.get('ok'):
            retry_after = (data.get('parameters') or {}).get('retry_after')
            if data.get('error_code') == 429 and retry_after:
                return {'_retry_after': float(retry_after)}
            # 409 on getUpdates means a webhook owns this bot's updates, so
            # polling can never succeed. Silent-ish warnings made this look
            # like "the bot is just quiet" for a long time.
            if data.get('error_code') == 409:
                global _last_conflict_log
                if time.time() - _last_conflict_log > 300:
                    _last_conflict_log = time.time()
                    log.error(
                        f"Telegram {method} conflict (409): {data.get('description')}. "
                        "A webhook or a second poller owns this bot. Clear it with "
                        "https://api.telegram.org/bot<TOKEN>/deleteWebhook — /start "
                        "linking cannot work until then."
                    )
                return None
            log.warning(f"Telegram {method} failed: {data}")
            return None
        return data.get('result')
    except Exception as e:
        log.warning(f"Telegram {method} error: {e}")
        return None


async def send_telegram_chat(session: aiohttp.ClientSession, chat_id, text: str) -> bool:
    if not chat_id or not text:
        return False
    for attempt in range(4):
        result = await telegram_api(session, 'sendMessage', payload={
            'chat_id': chat_id,
            'text': text[:4090],
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        })
        if isinstance(result, dict) and '_retry_after' in result:
            await asyncio.sleep(min(30.0, result['_retry_after']))
            continue
        return result is not None
    return False


def format_digest_html(fires: list[dict], extra: int = 0, window: str = '') -> str:
    """One message: up to _TG_DIGEST_LINES stocks, rest pointed to the app."""
    total = len(fires) + extra
    head = f"<b>Lakshmimata</b> · {total} alert{'s' if total != 1 else ''}"
    if window:
        head += f" · {_esc(window)}"
    rows = [head]
    for fire in fires:
        kind = fire.get('fire_type') or 'Alert'
        sym = (fire.get('sym') or '').upper()
        rs = fire.get('rs_tv') if fire.get('rs_tv') is not None else fire.get('rs')
        chg = fire.get('chg_pct')
        bits = []
        if rs is not None:
            bits.append(f"RS {rs}")
        if isinstance(chg, (int, float)):
            bits.append(f"{chg:+.1f}%")
        meta = '  '.join(bits)
        line = f"• <b>{_esc(sym)}</b> · {_esc(kind)}"
        if meta:
            line += f"  {_esc(meta)}"
        rows.append(line)
    if extra > 0:
        rows.append(f"<i>+{extra} more in the app</i>")
    rows.append("<i>Research alert — not advice</i>")
    return '\n'.join(rows)


async def _drain_telegram_queue(session: aiohttp.ClientSession):
    """Pace outbound DMs at _TG_SEND_PER_SEC. Scan loop does not wait."""
    global _drain_running
    interval = 1.0 / _TG_SEND_PER_SEC
    last = 0.0
    sent = 0
    try:
        while True:
            try:
                chat_id, text = _send_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            wait = interval - (time.time() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            if await send_telegram_chat(session, chat_id, text):
                sent += 1
            last = time.time()
            _send_q.task_done()
        if sent:
            log.info(f"  📬 Telegram: {sent} digest(s) delivered (paced {_TG_SEND_PER_SEC}/s)")
    finally:
        async with _drain_lock:
            _drain_running = False
            leftover = not _send_q.empty()
        if leftover:
            asyncio.create_task(_kick_telegram_drain(session))


async def _kick_telegram_drain(session: aiohttp.ClientSession):
    global _drain_running
    async with _drain_lock:
        if _drain_running:
            return
        _drain_running = True
    asyncio.create_task(_drain_telegram_queue(session))


async def publish_bot_username(session: aiohttp.ClientSession):
    """So Account → Connect Telegram works without a Vercel env rebuild."""
    global _last_publish_settings
    if not TELEGRAM_BOT_USERNAME:
        return
    if time.time() - _last_publish_settings < 3600:
        return
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        async with session.post(
            url,
            headers={**_headers(), 'Prefer': 'resolution=merge-duplicates'},
            json={'key': 'telegram_bot_username', 'value': TELEGRAM_BOT_USERNAME},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status in (200, 201):
                _last_publish_settings = time.time()
            elif r.status == 404:
                log.warning("app_settings missing — run add_user_telegram.sql")
    except Exception as e:
        log.warning(f"Could not publish telegram_bot_username: {e}")


async def _sb_get(session, path: str):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    async with session.get(url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return None
        return await r.json()


async def _sb_patch(session, table: str, match: str, body: dict) -> int:
    """Returns the number of rows actually written, or -1 on error.

    Deliberately asks for the updated rows back: with Prefer=return=minimal
    PostgREST answers 204 even when the filter matched nothing, so a link that
    silently wrote zero rows still looked like a success.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match}"
    async with session.patch(
        url,
        headers={**_headers(), 'Prefer': 'return=representation'},
        json=body,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        if r.status not in (200, 201):
            log.warning(f"Supabase patch {table} failed: {r.status} {(await r.text())[:200]}")
            return -1
        rows = await r.json(content_type=None)
        return len(rows) if isinstance(rows, list) else 0


async def _sb_delete(session, table: str, match: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match}"
    async with session.delete(
        url,
        headers={**_headers(), 'Prefer': 'return=minimal'},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        return r.status in (200, 204)


async def complete_link(session, code: str, chat_id: str, username: Optional[str]) -> bool:
    code = (code or '').strip()
    if not code or len(code) > 32:
        return False
    rows = await _sb_get(
        session,
        "user_telegram?select=user_id,link_code,link_code_expires_at"
        f"&link_code=eq.{code}",
    )
    if rows is None:
        log.error(
            "Telegram link lookup failed — the scanner could not read "
            "user_telegram. Check SUPABASE_SERVICE_KEY is the service-role key "
            "and that add_user_telegram.sql has been run."
        )
        return False
    if not rows:
        log.info(f"Telegram link code not found: {code}")
        return False
    row = rows[0]
    exp = row.get('link_code_expires_at')
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp.replace('Z', '+00:00'))
            if exp_dt < datetime.now(timezone.utc):
                log.info(f"Telegram link code expired: {code}")
                return False
        except Exception:
            pass
    uid = row['user_id']

    # chat_id is uniquely indexed, so a chat still bound to a different account
    # (common while testing, or when a user re-registers) would make the write
    # below fail. Release the stale binding first.
    existing = await _sb_get(
        session,
        f"user_telegram?select=user_id&chat_id=eq.{chat_id}&user_id=neq.{uid}",
    )
    if existing:
        for other in existing:
            await _sb_patch(session, 'user_telegram', f"user_id=eq.{other['user_id']}", {
                'chat_id': None,
                'telegram_username': None,
                'linked_at': None,
            })
        log.info(f"Telegram chat {chat_id} moved from {len(existing)} previous account(s)")

    written = await _sb_patch(session, 'user_telegram', f'user_id=eq.{uid}', {
        'chat_id': str(chat_id),
        'telegram_username': (username or '')[:64] or None,
        'enabled': True,
        'link_code': None,
        'link_code_expires_at': None,
        'linked_at': datetime.now(timezone.utc).isoformat(),
    })
    if written <= 0:
        log.error(
            f"Telegram link for user {uid} wrote {written} rows — the bot must not "
            "report success. If this persists, a trigger or RLS is stripping chat_id."
        )
        return False
    return True


async def poll_telegram_links(session: aiohttp.ClientSession):
    """Short poll from the live-scan idle loop. Only one process should poll."""
    global _update_offset, _last_poll
    if not TELEGRAM_BOT_TOKEN:
        return
    now = time.time()
    if now - _last_poll < 4:
        return
    _last_poll = now
    await publish_bot_username(session)
    params = {'timeout': 0, 'limit': 50}
    if _update_offset:
        params['offset'] = _update_offset
    result = await telegram_api(session, 'getUpdates', params=params)
    if not isinstance(result, list):
        return
    for upd in result:
        uid = upd.get('update_id')
        if isinstance(uid, int):
            _update_offset = max(_update_offset, uid + 1)
        msg = upd.get('message') or upd.get('edited_message') or {}
        text = (msg.get('text') or '').strip()
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        from_user = msg.get('from') or {}
        uname = from_user.get('username')
        if not chat_id or not text.startswith('/start'):
            continue
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ''
        if not payload:
            await send_telegram_chat(
                session, chat_id,
                "Open <b>Lakshmimata → Account → Telegram alerts</b> and tap "
                "<b>Connect Telegram</b> so I can link this chat to your account.",
            )
            continue
        ok = await complete_link(session, payload, str(chat_id), uname)
        if ok:
            await send_telegram_chat(
                session, chat_id,
                "✅ Linked to Lakshmimata.\n"
                "You will get labeled alerts (Squeeze, HY, Stage 2, …) matching "
                "your 🔔 preferences.\n"
                "Change types in the app — this chat is outbound only.",
            )
            log.info(f"Telegram linked chat {chat_id} (@{uname or '?'})")
        else:
            await send_telegram_chat(
                session, chat_id,
                "That connect code is unknown or expired. Generate a new one in "
                "Account → Telegram alerts.",
            )


# Delivery cadence, picked per user in Account → Telegram alerts. 'live'
# is the original behaviour: one digest per scan, so at most one a minute.
# The others batch the very same fires and send on a timer. Fires are
# buffered once for the whole day rather than per user, so memory tracks
# market activity instead of user count.
_CADENCE_SEC = {'live': 0, '5m': 300, '1h': 3600}
_CADENCE_LINES = {'live': _TG_DIGEST_LINES, '5m': 12, '1h': 20, 'eod': 30}
_CADENCE_WINDOW = {'5m': 'last 5 min', '1h': 'last hour', 'eod': 'today'}
_EOD_FLUSH_AFTER = (15, 35)   # IST — a few minutes past the close
_DAY_BUFFER_MAX = 5000

_day_buffer: list[tuple[float, dict]] = []
_buffer_day = ''
_last_flush: dict[str, float] = {}
_eod_flushed_on: dict[str, str] = {}
_recipients_cache: tuple[float, list, dict] = (0.0, [], {})
_last_flush_check = 0.0


def _cadence(prefs: dict) -> str:
    c = str(prefs.get('telegramCadence') or 'live').lower()
    return c if c in ('live', '5m', '1h', 'eod') else 'live'


def _rs_of(fire: dict):
    v = fire.get('rs_tv') if fire.get('rs_tv') is not None else fire.get('rs')
    try:
        return float(v) if v is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


def _buffer_fires(fires: list[dict]):
    global _buffer_day, _day_buffer
    today = datetime.now(IST).strftime('%Y-%m-%d')
    if today != _buffer_day:
        # Yesterday's fires must never appear in today's digest.
        _buffer_day = today
        _day_buffer = []
        _last_flush.clear()
    now = time.time()
    _day_buffer.extend((now, f) for f in fires)
    if len(_day_buffer) > _DAY_BUFFER_MAX:
        del _day_buffer[:len(_day_buffer) - _DAY_BUFFER_MAX]


async def _recipients(session: aiohttp.ClientSession) -> tuple[list, dict]:
    """Linked chats + their prefs, cached briefly.

    Both the per-scan fan-out and the cadence flush need this, and the
    flush runs from the idle loop every few seconds, so without a cache
    this would hammer Supabase with the same two reads.
    """
    global _recipients_cache
    ts, links, prefs_by = _recipients_cache
    if time.time() - ts < 45:
        return links, prefs_by
    links = await _sb_get(
        session,
        'user_telegram?select=user_id,chat_id,enabled&enabled=eq.true&chat_id=not.is.null',
    ) or []
    prefs_rows = await _sb_get(session, 'user_alert_prefs?select=user_id,prefs') or []
    prefs_by = {str(r['user_id']): (r.get('prefs') or {}) for r in prefs_rows}
    _recipients_cache = (time.time(), links, prefs_by)
    return links, prefs_by


def _iter_chats(links: list, prefs_by: dict):
    """Yields (uid, chat_id, prefs) once per chat, skipping paused users."""
    seen: set[str] = set()
    for link in links:
        chat_id = link.get('chat_id')
        if not chat_id:
            continue
        # chat_id is uniquely indexed, but guard anyway: one Telegram chat
        # must never receive two digests because it maps to two accounts.
        key = str(chat_id)
        if key in seen:
            log.warning(f"Telegram chat {key} linked to more than one account — sending once")
            continue
        seen.add(key)
        prefs = prefs_by.get(str(link.get('user_id') or '')) or {}
        if prefs.get('telegramEnabled') is False:
            continue
        yield str(link.get('user_id') or ''), chat_id, prefs


def _matched_for(fires: list[dict], prefs: dict) -> list[dict]:
    out = [f for f in fires if telegram_type_allowed(f.get('fire_type') or '', prefs)]
    # "Watchlist only" has to be applied here, since the scanner sends these
    # messages and cannot see the browser's watchlist. A missing key means an
    # older client that never published one, so filtering is skipped rather
    # than silently muting the user.
    watchlist = prefs.get('watchlistSyms')
    if prefs.get('watchlistOnly') is True and watchlist is not None:
        wanted = {str(s).upper() for s in watchlist}
        out = [f for f in out if str(f.get('sym') or '').upper() in wanted]
    return out


async def fanout_telegram_alerts(session: aiohttp.ClientSession, fires: list[dict]):
    """Queue one digest per 'live' user; drain at 25/s off the scan loop.

    1,000 users × 10 alerts → 1,000 messages (~40s), not 10,000. Users on a
    batched cadence are served by flush_due_telegram_digests instead.
    """
    if not TELEGRAM_BOT_TOKEN or not fires:
        return
    _buffer_fires(fires)
    links, prefs_by = await _recipients(session)
    if not links:
        return

    queued = 0
    for _uid, chat_id, prefs in _iter_chats(links, prefs_by):
        if _cadence(prefs) != 'live':
            continue
        matched = _matched_for(fires, prefs)
        if not matched:
            continue
        shown = matched[:_TG_DIGEST_LINES]
        extra = len(matched) - len(shown)
        await _send_q.put((chat_id, format_digest_html(shown, extra)))
        queued += 1
    if queued:
        log.info(f"  📬 Telegram: queued {queued} digest(s) for {len(links)} linked chat(s)")
        await _kick_telegram_drain(session)


async def flush_due_telegram_digests(session: aiohttp.ClientSession):
    """Send the batched cadences (5m / 1h / end of day) from the idle loop.

    Driven from the loop rather than from run_scan because the end-of-day
    digest has to go out after the close, when scans have already stopped.
    """
    global _last_flush_check
    if not TELEGRAM_BOT_TOKEN or not _day_buffer:
        return
    now = time.time()
    if now - _last_flush_check < 30:
        return
    _last_flush_check = now
    links, prefs_by = await _recipients(session)
    if not links:
        return

    ist = datetime.now(IST)
    after_close = (ist.hour, ist.minute) >= _EOD_FLUSH_AFTER
    today = ist.strftime('%Y-%m-%d')
    queued = 0
    for uid, chat_id, prefs in _iter_chats(links, prefs_by):
        cad = _cadence(prefs)
        if cad == 'live':
            continue
        if cad == 'eod':
            if not after_close or _eod_flushed_on.get(uid) == today:
                continue
            since = 0.0
        else:
            last = _last_flush.get(uid)
            if last is not None and now - last < _CADENCE_SEC[cad]:
                continue
            since = last or 0.0
        matched = _matched_for([f for ts, f in _day_buffer if ts > since], prefs)
        if not matched:
            # Nothing to say. Still move the clock, so the next window is
            # measured from now instead of replaying old fires later.
            _last_flush[uid] = now
            continue
        # A batched digest is a shortlist, so lead with the strongest names
        # rather than whichever fired first.
        matched.sort(key=_rs_of, reverse=True)
        cap = _CADENCE_LINES.get(cad, _TG_DIGEST_LINES)
        shown = matched[:cap]
        await _send_q.put((
            chat_id,
            format_digest_html(shown, len(matched) - len(shown), _CADENCE_WINDOW.get(cad, '')),
        ))
        _last_flush[uid] = now
        if cad == 'eod':
            _eod_flushed_on[uid] = today
        queued += 1
    if queued:
        log.info(f"  📬 Telegram: queued {queued} batched digest(s)")
        await _kick_telegram_drain(session)
