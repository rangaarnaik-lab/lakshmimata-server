#!/usr/bin/env python3
"""
PocketRS Pro — Fundamentals + Announcements Worker (worthy-simplicity)
"""
import os
import gc
import sys
import time
import json
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta

from shared import *

log = logging.getLogger('pocketrs')

# ══ FUNDAMENTALS-WORKER-ONLY FUNCTIONS ══

async def _announcements_loop(session: aiohttp.ClientSession):
    """Fetches NSE's corporate announcements feed (all equities in one
    call) every 15 minutes and upserts new ones to Supabase. See
    fetch_nse_announcements's docstring for the caveat about field names
    not being independently verified yet — first several cycles log the
    raw response shape for confirmation."""
    CHECK_INTERVAL = 5 * 60  # 5 minutes — near-real-time for watchlist alerts, while staying polite to NSE (faster polling risks an IP block, after which the feed goes silent entirely)
    table_ready = await ensure_announcements_table(session)
    if not table_ready:
        log.error("corporate_announcements table unavailable — announcements loop cannot proceed.")
        return
    cycle = 0
    while True:
        try:
            cycle += 1
            rows = await fetch_nse_announcements(session, debug=(cycle <= 3))
            if rows:
                await enrich_and_save_announcements(session, rows)
            else:
                log.info("📢 No announcements fetched this cycle (empty result or fetch failed).")
        except Exception as e:
            import traceback
            log.error(f"Announcements loop cycle failed: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(CHECK_INTERVAL)


def _dedupe_by_key(rows: list, keys: tuple) -> list:
    """NSE feeds sometimes contain the same item twice in one response;
    Postgres rejects a bulk upsert that touches the same unique key twice
    (error 21000). Keep the last occurrence of each key."""
    seen = {}
    for r in rows:
        k = tuple(str(r.get(x) or '') for x in keys)
        seen[k] = r
    return list(seen.values())


def _extract_period_ended_from_text(text: str):
    """Pull the reporting period's end date straight out of an
    announcement's own subject. Rebuilt as one comprehensive parser
    after two rounds of reactive single-format patching (the original
    only handled 'Month Day, Year'; a second round added 'Day Month
    Year' and 'Day-Mon-Year' after real filings fell through both).
    Rather than keep patching format-by-format as new examples surface,
    this anchors on the word 'ended' generically (not a fixed prefix
    like 'quarter/period/year ended', which would miss 'half year
    ended', 'nine months ended', 'FY ended') and tries every date shape
    tested against: Month-name Day Year, Day Month-name Year (both with
    optional comma and ordinal suffixes like '30th'), Day-Mon-Year
    dashed, numeric DD-MM-YYYY / DD/MM/YYYY, and ISO YYYY-MM-DD.
    Verified against 14 real and constructed test cases before shipping
    — see the fix commit for the full battery. Returns a 'DD-Mon-YYYY'
    string (a format _norm_date already parses) or None if truly
    nothing recognizable follows 'ended' in the text."""
    if not text:
        return None
    m_anchor = re.search(r'\bended\s+(?:on\s+)?(.{4,25})', text, re.IGNORECASE)
    if not m_anchor:
        return None
    frag = m_anchor.group(1).strip()

    patterns = [
        (r'^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})', 'dmy_name'),
        (r'^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})', 'mdy_name'),
        (r'^(\d{1,2})-([A-Za-z]+)-(\d{4})', 'dmy_dash'),
        (r'^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})', 'dmy_numeric'),
        (r'^(\d{4})-(\d{1,2})-(\d{1,2})', 'ymd_numeric'),
    ]
    for pat, kind in patterns:
        m = re.match(pat, frag)
        if not m:
            continue
        try:
            if kind in ('dmy_name', 'dmy_dash'):
                month_num = _MONTH_NAMES.get(m.group(2).lower())
                if not month_num:
                    continue
                return datetime(int(m.group(3)), month_num, int(m.group(1))).strftime('%d-%b-%Y')
            if kind == 'mdy_name':
                month_num = _MONTH_NAMES.get(m.group(1).lower())
                if not month_num:
                    continue
                return datetime(int(m.group(3)), month_num, int(m.group(2))).strftime('%d-%b-%Y')
            if kind == 'dmy_numeric':
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime('%d-%b-%Y')
            if kind == 'ymd_numeric':
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime('%d-%b-%Y')
        except ValueError:
            continue
    return None


def _extract_value_crore(text: str):
    """Best-effort extraction of a monetary value in ₹ crore from filing
    text — regex over the common Indian formats ('Rs. 450 crore',
    '₹450Cr', 'INR 45,000 lakhs', '2.5 billion'). Returns the largest
    value found (filings often mention the headline number alongside
    smaller components), or None."""
    pattern = re.compile(
        r'(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\b|lakhs?|million|mn\b|billions?|bn\b)',
        re.IGNORECASE)
    best = None
    for num, unit in pattern.findall(text or ''):
        try:
            v = float(num.replace(',', ''))
        except ValueError:
            continue
        u = unit.lower()
        if u.startswith('lakh'):
            v *= 0.01
        elif u.startswith(('million', 'mn')):
            v *= 0.1
        elif u.startswith(('billion', 'bn')):
            v *= 100.0
        if v > 0 and (best is None or v > best):
            best = v
    return best


async def _fundamentals_loop(session: aiohttp.ClientSession):
    """Reuses fetch_upstox_fundamentals / fetch_fundamentals_screener /
    load_fundamentals_from_supabase / load_fundamentals_batch exactly as
    they already exist — nothing duplicated, this is genuinely the same
    code, just invoked from a different entry point in a different
    process. Checking once daily is already far more often than needed for
    data that changes quarterly.

    Like _market_cap_catchup_loop, only actually fetches while the
    market is closed — same reasoning: don't compete with the live-scan
    service's own network/rate-limit budget during trading hours."""
    CHECK_INTERVAL = 86400  # 24 hours — fundamentals data changes quarterly at most, daily check is plenty
    table_ready = await ensure_fundamentals_table(session)
    if not table_ready:
        log.error("stock_fundamentals table unavailable — fundamentals loop cannot proceed.")
        return
    while True:
        if is_market_open():
            await asyncio.sleep(300)
            continue
        try:
            stale_or_missing = await load_fundamentals_from_supabase(session)
            if stale_or_missing:
                log.info(f"📊 Fetching fundamentals for {len(stale_or_missing)} stocks…")
                await load_fundamentals_batch(session, stale_or_missing)
            else:
                log.info("✅ All fundamentals current — nothing to fetch this cycle.")
        except Exception as e:
            import traceback
            log.error(f"Fundamentals loop cycle failed: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(CHECK_INTERVAL)


def _is_results_announcement(row: dict) -> bool:
    """Same category/subject matching the frontend's Results tab uses,
    kept in sync deliberately so 'what counts as a results filing' is
    identical on both ends."""
    text = ((row.get('category') or '') + ' ' + (row.get('subject') or '')).lower()
    if any(x in text for x in _RESULTS_ANN_EXCLUDE):
        return False
    return any(x in text for x in _RESULTS_ANN_KEYWORDS)


async def _market_cap_catchup_loop(session: aiohttp.ClientSession):
    """Runs hourly, separate from the once-daily FULL fundamentals
    refresh (_fundamentals_loop below) — re-attempts ONLY stocks still
    missing market_cap, a much smaller and shrinking list rather than
    the full ~2400-stock universe. Screener.in/Upstox rate-limiting
    means a single daily attempt often isn't enough to clear the whole
    backlog in one pass (confirmed via logs: heavy 429s/timeouts,
    <500/1300 succeeding some cycles) — checking back hourly for just
    the still-missing subset closes that gap faster.

    Deliberately capped modest (100/hour, not higher) and kept
    completely separate from the daily loop's cadence — the tradeoff
    here is real: more frequent retries mean faster coverage, but also
    more requests per hour to sites that are already rate-limiting
    heavily under the current load. If 429s get noticeably worse after
    this ships, lowering MAX_PER_CYCLE or CHECK_INTERVAL further is the
    first thing to try, not reverting outright — some retry cadence is
    still better than waiting a full day.

    Runs ONLY while the market is closed — Screener.in/Upstox scraping
    is heavy enough (many concurrent requests, occasional multi-second
    stalls) that doing it during trading hours risks competing for the
    same network/rate-limit budget as the live-scan service's own price
    fetching, right when price freshness matters most. Checks every
    5 min while open so it starts promptly the moment the market shuts,
    rather than waiting for its own full hourly cadence to roll around."""
    CHECK_INTERVAL = 3600  # 1 hour
    MAX_PER_CYCLE = 200  # bumped from 100 given a confirmed large backlog
                          # (1087/2411 stocks missing market cap) — if the
                          # 📋 Fetch outcome breakdown log shows 429s
                          # climbing noticeably after this, dial back to
                          # 100-150 rather than push further.
    while True:
        if is_market_open():
            await asyncio.sleep(300)  # check back every 5 min while open
            continue
        try:
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            url = (f"{SUPABASE_URL}/rest/v1/stock_fundamentals"
                   f"?select=sym&market_cap=is.null&limit={MAX_PER_CYCLE}")
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    missing = [row['sym'] for row in await r.json()]
                else:
                    missing = []
                    log.warning(f"Market cap catchup: symbol query failed ({r.status})")
            if missing:
                log.info(f"💰 Market cap catchup: retrying {len(missing)} stocks still missing market cap…")
                await load_fundamentals_batch(session, missing)
            else:
                log.info("💰 Market cap catchup: nothing missing — all caught up.")
        except Exception as e:
            log.error(f"Market cap catchup loop failed: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


def _norm_date(s):
    """NSE's two endpoints format the same date differently — the
    results LIST feed gives 'toDate' as e.g. '31-Dec-2024' (title-case
    month) while the per-symbol comparison endpoint gives 'to_date' as
    e.g. '01-OCT-2024' (upper-case month). A plain string compare
    between the two silently fails almost every time even for the exact
    same date, which is why sales/pat/eps were coming back empty for
    nearly every row despite the loop reporting success. Parse both
    into a canonical YYYY-MM-DD before comparing; datetime.strptime's
    %b is case-insensitive so this handles both cases, with a couple of
    fallback formats in case NSE varies it further. Module-level (not
    nested in fetch_nse_results_numbers, where this originally lived)
    since fetch_xbrl_url_for_symbol needs it too."""
    s = (s or '').strip()
    if not s:
        return ''
    for fmt in ('%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return s.upper()  # last resort — at least case-insensitive

# ── Direct XBRL parsing — a company's real filed numbers, structured,
# available the moment the filing lands, rather than waiting on NSE's
# separate results-comparision API to sync (confirmed via production
# log: that sync can lag many hours behind the public announcement —
# COFORGE's June-2026 numbers stayed unavailable there for 7+ hours
# after the press release went public with the same figures). The
# announcements feed only gives a hasXbrl boolean and the PDF link, not
# the XBRL file itself — the real link lives in the results-LIST feed's
# 'xbrl' field (see fetch_nse_financial_results), so this cross-
# references that feed by symbol+period first.

def _nse_local_to_utc_iso(raw) -> str:
    """NSE's timestamp fields (an_dt, broadCastDate, filingDate, etc.)
    are always India-local wall-clock time with NO timezone marker
    attached — e.g. '26-Jul-2026 18:15:00' genuinely means 18:15 IST,
    not 18:15 UTC. Stored into a `timestamptz` column as that bare
    string, Postgres has no way to know it's IST and silently assumes
    UTC, shifting every displayed time by 5:30 hours once the frontend
    converts it back to IST for display — exactly the 'time looks wrong'
    symptom reported against a live announcement. This explicitly parses
    the known NSE formats AS India time and returns a proper
    timezone-aware ISO string, so there's no ambiguity left for Postgres
    to guess wrong on. Returns the input unchanged if it doesn't match
    any known format, rather than silently corrupting it."""
    if not raw:
        return raw
    raw = str(raw).strip()
    for fmt in ('%d-%b-%Y %H:%M:%S', '%d-%b-%Y %H:%M', '%d-%m-%Y %H:%M:%S',
                '%d-%m-%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=IST)
            return dt.isoformat()
        except ValueError:
            continue
    return raw


async def _results_loop(session: aiohttp.ClientSession):
    """Polls NSE's structured financial-results feed every 30 min — new
    results only land around filing bursts (post-board-meeting evenings
    in results season), so this doesn't need the announcements loop's
    5-min cadence.

    IMPORTANT #1: calling fetch_nse_financial_results with NO date range
    does NOT return "latest first" as might be assumed — confirmed via
    raw logs that a bare call kept returning the same old items (e.g. a
    Dec-2024-quarter Videocon filing) run after run, hours apart, while
    same-day real filings never appeared in this feed's response at all.

    IMPORTANT #2: a genuine multi-day range (from_date != to_date) was
    tried next and confirmed via production logs to reliably return
    'got 0 item(s)' — five separate cycles over 1h40m, all zero, despite
    real filings existing in that window (independently confirmed via
    the Announcements feed same-day). The ONLY range shape anywhere in
    this codebase that's actually confirmed working is the announcements
    backfill's day-by-day loop, where from_date always equals to_date —
    a genuine range was never empirically verified for either endpoint,
    it was an untested assumption. Matching that exact proven shape
    here: loop over the last few days individually instead of one wide
    range call."""
    CHECK_INTERVAL = 30 * 60
    cycle = 0
    while True:
        try:
            debug = cycle < 3
            today = datetime.now(timezone.utc)
            rows = []
            for days_back in range(5):  # today + previous 4 days, one call each
                day_str = (today - timedelta(days=days_back)).strftime('%d-%m-%Y')
                day_rows = await fetch_nse_financial_results(
                    session, debug=(debug and days_back < 2), from_date=day_str, to_date=day_str)
                if day_rows:
                    rows.extend(day_rows)
                await asyncio.sleep(0.5)
            if rows:

                rows = _dedupe_by_key(rows, ('symbol', 'period_ended', 'result_type'))
                # The list feed has no numbers — fill them from the
                # per-symbol comparison endpoint, capped per cycle and
                # paced with sleeps so a results-season burst doesn't
                # turn into a request storm at NSE. Shares
                # _results_attempt_times with the announcement-driven
                # trigger above — same cooldown reasoning, see its
                # comment for the production evidence.
                headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
                now_ts = time.time()
                to_fill = [r for r in rows
                          if now_ts - _results_attempt_times.get((r['symbol'], r['period_ended']), 0)
                             >= _RESULTS_ATTEMPT_COOLDOWN_SEC][:25]
                for i, r in enumerate(to_fill):
                    _results_attempt_times[(r['symbol'], r['period_ended'])] = now_ts
                    nums = await fetch_nse_results_numbers(
                        session, headers, r['symbol'], r['period_ended'], debug=(debug and i < 2))
                    if nums:
                        r.update({k: v for k, v in nums.items() if v is not None})
                    await asyncio.sleep(1.5)
                await save_financial_results_to_db(session, rows)
            cycle += 1
        except Exception as e:
            log.error(f"Results loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def backfill_announcements_history(session: aiohttp.ClientSession, days: int = 30):
    """One-time backfill of the last `days` days of NSE announcements,
    looped one day at a time (rather than a single wide date-range
    request) so each request stays a reasonable size and NSE doesn't see
    one big burst — a short sleep between requests keeps this polite.
    Safe to run more than once: everything downstream still upserts on
    (symbol, subject, announced_at), so re-running just re-saves the same
    rows rather than duplicating them."""
    log.info(f"📚 Starting {days}-day announcements backfill…")
    today = datetime.now(timezone.utc)
    total_saved = 0
    for i in range(days):
        day = today - timedelta(days=i)
        date_str = day.strftime('%d-%m-%Y')
        try:
            rows = await fetch_nse_announcements_for_range(session, date_str, date_str, debug=(i < 2))
            if rows:
                await enrich_and_save_announcements(session, rows)
                total_saved += len(rows)
                log.info(f"  📚 Backfilled {date_str}: {len(rows)} announcements")
            else:
                log.info(f"  📚 Backfilled {date_str}: 0 announcements")
        except Exception as e:
            log.warning(f"  ⚠️ Backfill failed for {date_str}: {e}")
        await asyncio.sleep(2)  # polite pacing between days
    log.info(f"📚 Backfill complete: {total_saved} announcement-rows processed across {days} days")

# ── NSE quarterly financial results (structured, no PDF parsing) ────────

async def backfill_results_history(session: aiohttp.ClientSession, days: int = 30):
    """One-time backfill of quarterly results filed in the last `days`
    days, via the list feed's date-range params. Day-by-day calls
    (from_date==to_date each time) rather than one wide-range call — see
    _results_loop's docstring for why: a genuine multi-day range was an
    untested assumption that production logs later confirmed reliably
    returns 0 items, while day-by-day is the one shape actually proven
    to work (matching the announcements backfill). Numbers require one
    per-symbol request each, so they're fetched newest-first with polite
    pacing and capped (RESULTS_NUMBERS_MAX, default 400) — rows past the
    cap still save with period/type/link and get numbers organically if
    they reappear in the live loop's window. Idempotent like the
    announcements backfill: re-running just re-upserts."""
    log.info(f"📚 Starting {days}-day results backfill…")
    today = datetime.now(timezone.utc)
    rows = []
    for days_back in range(days):
        day_str = (today - timedelta(days=days_back)).strftime('%d-%m-%Y')
        day_rows = await fetch_nse_financial_results(
            session, debug=(days_back < 3), from_date=day_str, to_date=day_str)
        if day_rows:
            rows.extend(day_rows)
            log.info(f"  📚 Results list {day_str}: {len(day_rows)} filing(s)")
        await asyncio.sleep(0.5)
    if not rows:
        log.warning("📚 Results backfill: list fetch returned nothing")
        return
    rows = _dedupe_by_key(rows, ('symbol', 'period_ended', 'result_type'))
    try:
        rows.sort(key=lambda r: r.get('filed_at') or '', reverse=True)
    except Exception:
        pass
    cap = int(os.getenv('RESULTS_NUMBERS_MAX', '400'))
    headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
    filled = 0
    for i, r in enumerate(rows[:cap]):
        nums = await fetch_nse_results_numbers(session, headers, r['symbol'],
                                               r['period_ended'], debug=(i < 2))
        if nums:
            r.update({k: v for k, v in nums.items() if v is not None})
            filled += 1
        await asyncio.sleep(1.2)
        if (i + 1) % 50 == 0:
            log.info(f"  📚 Results backfill numbers: {i+1}/{min(cap,len(rows))} fetched…")
    await save_financial_results_to_db(session, rows)
    log.info(f"📚 Results backfill complete: {len(rows)} filings saved, numbers filled for {filled}")

# ── Main loop ─────────────────────────────────────────────────────────

async def enrich_and_save_announcements(session: aiohttp.ClientSession, rows: list):
    """Wraps save_announcements_to_db with sector/industry/market_cap
    enrichment. Kept as a separate wrapper (rather than editing
    save_announcements_to_db directly) so the original upsert function
    stays untouched and easy to reason about."""
    if not rows:
        return
    enriched = [enrich_announcement_row(dict(r)) for r in rows]
    enriched = await rate_announcements_with_ai(session, enriched)
    enriched = tag_order_size(enriched)
    # PostgREST bulk upserts require every row in the request to have an
    # identical key set (PGRST102 otherwise) — but ai_summary/order_size
    # are only set on rows where a value could be extracted. Normalize:
    # give every row every key, None where absent.
    all_keys = set()
    for r in enriched:
        all_keys.update(r.keys())
    for r in enriched:
        for k in all_keys:
            r.setdefault(k, None)
    enriched = _dedupe_by_key(enriched, ('symbol', 'subject', 'announced_at'))
    await save_announcements_to_db(session, enriched)

    # Results numbers are triggered directly off results-type
    # announcements landing here, rather than a separate scheduled poll
    # against NSE's results-list feed — see this section's header
    # comment above for why. Capped per call to stay polite to NSE
    # during results-season bursts (a single day can have 600+
    # announcements, only a handful of which are results filings, but
    # worth a ceiling regardless).
    #
    # Cooldown check: confirmed via production log that without this,
    # the SAME (symbol, period) gets re-attempted every single 5-min
    # cycle for as long as the announcement stays in the 'recent' window
    # — one case ran 18+ identical attempts over 3 hours, all returning
    # the same stale prior-quarter data because NSE simply hadn't
    # published that specific quarter's numbers yet. Not a bug in the
    # fetch itself, just missing backoff. _results_attempt_times is
    # shared with _results_loop below so neither path duplicates the
    # other's work.
    results_rows = [r for r in enriched if _is_results_announcement(r)]
    if results_rows:
        headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
        filled = 0
        attempted = 0
        now_ts = time.time()
        for r in results_rows:
            period_ended = _extract_period_ended_from_text(r.get('subject') or '')
            key = (r.get('symbol'), period_ended)
            if period_ended and now_ts - _results_attempt_times.get(key, 0) < _RESULTS_ATTEMPT_COOLDOWN_SEC:
                continue
            if attempted >= 20:
                break
            try:
                _results_attempt_times[key] = now_ts
                if await fetch_and_save_result_for_announcement(session, headers, r, debug=(attempted < 2)):
                    filled += 1
            except Exception as e:
                log.warning(f"⚠️ Results-from-announcement fetch failed for {r.get('symbol')}: {e}")
            attempted += 1
            await asyncio.sleep(1.2)
        log.info(f"  📊 Results-from-announcement: {filled}/{attempted} numbers filled "
                 f"({len(results_rows)} results-type announcement(s) this batch, "
                 f"{len(results_rows)-attempted} skipped via cooldown)")


def enrich_announcement_row(row: dict) -> dict:
    """Stamps one announcement row with the stock's sector, industry, and
    market cap (₹ Cr) so the frontend can filter the Announcements tab by
    those, the same way the Sectors/Watchlist tabs already do — without
    needing a live join against the stocks table on every page load.
    Reuses the same SECTOR_INDUSTRY_LOOKUP / fundamentals_cache the live
    scan already relies on, so no extra fetching here."""
    sym = row.get('symbol')
    row['sector'] = get_sector(sym) if sym else None
    row['industry'] = get_industry(sym) if sym else None
    row['market_cap'] = fundamentals_cache.get(sym, {}).get('market_cap') if sym else None
    return row

_ANN_POSITIVE_PATTERNS = [
    'award of order', 'work order', 'purchase order', 'order worth', 'order valued',
    'bagged', 'bagging', 'receiving of order', 'receipt of order', 'receiving of contract',
    'receipt of contract', 'secures order', 'secured order', 'wins order', 'won order', 'new order',
    'letter of intent', 'contract awarded', 'awarded a contract', 'received an order',
    'order received from', 'capacity expansion', 'commercial production', 'usfda',
    'approval received', 'patent granted', 'buyback', 'bonus issue', 'stock split',
    'reduced to nil', 'in favour of the company', 'in favor of the company',
    'settled in favour', 'demand quashed', 'preferential allotment completed',
]
_ANN_NEGATIVE_PATTERNS = [
    'resignation of', 'resigned', 'penalty', 'show cause', 'gst demand', 'tax demand',
    'demand order', 'search and seizure', 'default', 'downgrade', 'fire at', 'accident at',
    'plant shutdown', 'suspension of operations', 'insolvency', 'nclt admission',
    'fraud', 'auditor has resigned', 'pledge of shares', 'shares pledged',
    'disqualified', 'sebi order against', 'debarred',
    'cancellation of order', 'cancellation of work order', 'cancellation of purchase order',
    'order cancelled', 'work order cancelled', 'purchase order cancelled',
    'cancellation of contract', 'contract cancelled', 'termination of contract',
    'contract terminated', 'termination of work order', 'work order terminated',
    'rescission of', 'order rescinded', 'contract rescinded', 'order withdrawn',
    'withdrawal of order', 'loss of order', 'order lost', 'annulment of',
]
# Order-win phrases (_ORDER_WIN_PATTERNS below) can appear inside the text of
# an order CANCELLATION too — e.g. "Cancellation of Work Order by Reliance
# Industries Limited" contains "work order". Any row matching one of these
# cancel/termination phrases must never be treated as an order win, no matter
# what else it contains. Kept in sync with _ANN_NEGATIVE_PATTERNS' cancel
# terms above (a superset — this one doesn't need the resignation/penalty/etc
# entries that are irrelevant to order-win detection).
_ORDER_CANCEL_PATTERNS = [
    'cancellation of order', 'cancellation of work order', 'cancellation of purchase order',
    'order cancelled', 'work order cancelled', 'purchase order cancelled',
    'cancellation of contract', 'contract cancelled', 'termination of contract',
    'contract terminated', 'termination of work order', 'work order terminated',
    'rescission of', 'order rescinded', 'contract rescinded', 'order withdrawn',
    'withdrawal of order', 'loss of order', 'order lost', 'annulment of',
]


async def ensure_announcements_table(session: aiohttp.ClientSession,
                                      retries: int = 6, delay: float = 10.0) -> bool:
    """Same self-healing pattern as ensure_fundamentals_table — see that
    function for why the retry loop is needed (PostgREST schema cache lag
    after creating a table via the SQL Editor)."""
    url = f"{SUPABASE_URL}/rest/v1/corporate_announcements?limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return True
                if r.status == 404 and attempt < retries - 1:
                    await asyncio.sleep(delay)
                    continue
                log.error(f"corporate_announcements table check failed: {r.status}")
                return False
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(delay)
                continue
            log.error(f"corporate_announcements table check failed: {e}")
            return False
    return False


async def fetch_and_parse_xbrl(session: aiohttp.ClientSession, url: str, debug: bool = False) -> dict:
    """Downloads and parses an NSE XBRL filing directly — structured
    XML, no AI/PDF-OCR needed. Matches purely on local element name,
    ignoring namespace, for robustness across different filer
    taxonomies. Logs every distinct tag name found on debug calls so
    the candidate lists above can be corrected against reality."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                if debug:
                    log.info(f"  📄 XBRL fetch failed: status {r.status} for {url}")
                return {}
            content = await r.read()
    except Exception as e:
        if debug:
            log.info(f"  📄 XBRL fetch exception: {type(e).__name__}: {e}")
        return {}

    try:
        root = ET.fromstring(content)
    except Exception as e:
        if debug:
            log.info(f"  📄 XBRL parse exception: {type(e).__name__}: {e}")
        return {}

    def local_name(tag):
        return tag.split('}')[-1] if '}' in tag else tag

    all_tags = {}
    for el in root.iter():
        name = local_name(el.tag)
        text = (el.text or '').strip()
        if text:
            all_tags.setdefault(name, []).append(text)

    if debug:
        sample = {k: v[0] for k, v in list(all_tags.items())[:40]}
        log.info(f"  📄 XBRL tags found ({len(all_tags)} distinct): {json.dumps(sample)[:1500]}")

    def first_matching(candidates):
        for c in candidates:
            if c in all_tags and all_tags[c]:
                try:
                    return float(all_tags[c][0].replace(',', ''))
                except ValueError:
                    continue
        return None

    sales, pat, eps = first_matching(_XBRL_SALES_TAGS), first_matching(_XBRL_PAT_TAGS), first_matching(_XBRL_EPS_TAGS)
    # XBRL reports raw currency units (INR), not crore — divide by 1e7
    # to match the rest of the app's crore-based fields. EPS is already
    # a per-share rupee value, no scaling needed.
    result = {}
    if sales is not None:
        result['sales'] = round(sales / 1e7, 2)
    if pat is not None:
        result['pat'] = round(pat / 1e7, 2)
    if eps is not None:
        result['eps'] = round(eps, 2)
    return result


async def fetch_and_save_result_for_announcement(session: aiohttp.ClientSession, headers: dict,
                                                  row: dict, debug: bool = False) -> bool:
    """Given ONE results-type announcement, fetch that stock's numbers
    and save them. Tries direct XBRL parsing FIRST (available
    immediately when filed, structured, no sync lag) — only falls back
    to the results-comparision endpoint (confirmed to lag NSE's own
    public announcements by hours in some cases) if no XBRL is found or
    parsing yields nothing useful. Returns False (and saves nothing) if
    the period can't be parsed out of the subject text at all — better
    to skip than guess wrong."""
    period_ended = _extract_period_ended_from_text(row.get('subject') or '')
    symbol = row.get('symbol')
    if not period_ended or not symbol:
        return False

    nums = {}
    xbrl_url = await fetch_xbrl_url_for_symbol(session, symbol, period_ended, debug=debug)
    if xbrl_url:
        nums = await fetch_and_parse_xbrl(session, xbrl_url, debug=debug)
    if not nums:
        nums = await fetch_nse_results_numbers(session, headers, symbol, period_ended, debug=debug)

    result_row = {
        'symbol': symbol,
        'period_ended': period_ended,
        'result_type': 'Consolidated',
        'sales': None, 'pat': None, 'eps': None,
        'filed_at': row.get('announced_at'),
        'attachment_url': row.get('attachment_url'),
    }
    if nums:
        result_row.update({k: v for k, v in nums.items() if v is not None})
    await save_financial_results_to_db(session, [result_row])
    return bool(nums)


async def fetch_nse_announcements(session: aiohttp.ClientSession, debug: bool = False) -> list:
    """
    Fetch recent corporate announcements across ALL NSE-listed equities in
    one call (not per-symbol — the endpoint already covers everything).

    NSE's site blocks requests without a valid session cookie (same class
    of bot-detection as Screener.in). Cookie-priming URL, referer, and
    headers below are NOT guessed — verified by directly installing and
    reading the source of an established, actively-maintained open-source
    NSE API library (135+ GitHub followers, several related projects by
    the same author) rather than assuming: the priming request specifically
    hits /option-chain (not the homepage), and the referer on the actual
    data request is a specific equity quote page, not the announcements
    page itself. Both differ from what an initial, untested guess used.

    Field names below (symbol/desc/attchmntFile/an_dt) are still based on
    the documented shape from established open-source NSE API libraries,
    not independently verified against a live response — this sandbox
    can't reach nseindia.com to test directly (not in the allowed network
    list). Defensively checks a few plausible name variants per field,
    and logs the raw response shape for the first several calls so the
    real shape can be confirmed/corrected from actual Railway logs.
    """
    global _nse_announcements_debug_count
    headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
    try:
        # Cookie-priming request — must happen first, same session object,
        # so the cookies aiohttp receives here get sent automatically on
        # the second request below. /option-chain specifically, verified
        # against the reference library's source — not the homepage.
        async with session.get("https://www.nseindia.com/option-chain", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r0:
            if debug:
                log.info(f"  🔍 NSE cookie-priming request: status={r0.status}")

        async with session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                key = f'nse_announcements_status_{r.status}'
                _fetch_error_counts[key] = _fetch_error_counts.get(key, 0) + 1
                if debug:
                    log.info(f"  🔍 NSE announcements: non-200 status ({r.status})")
                return []
            data = await r.json()
    except Exception as e:
        _fetch_error_counts[f'nse_announcements_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'nse_announcements_{type(e).__name__}', 0) + 1
        if debug:
            log.info(f"  🔍 NSE announcements fetch exception: {type(e).__name__}: {e}")
        return []

    if _nse_announcements_debug_count < 3:
        _nse_announcements_debug_count += 1
        log.info(f"  🔍 NSE announcements raw response (first item): "
                 f"{json.dumps(data[0] if isinstance(data, list) and data else data)[:1500]}")

    items = data if isinstance(data, list) else data.get('data', []) if isinstance(data, dict) else []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get('symbol') or item.get('smbl')
        # NSE tags each announcement with a category separately from the
        # free-text description (e.g. category="Financial Results" or
        # "Award of Order / Receipt of Order", subject=the actual
        # announcement text) — kept split so the frontend's category
        # sub-tabs (Results/Concall/Order Book/etc.) can filter on the
        # category instead of substring-matching a paragraph of prose.
        category = item.get('desc') or item.get('smSubject') or ''
        subject = (item.get('attchmntText') or item.get('subject') or category or '')
        attachment = item.get('attchmntFile') or item.get('attachmentFile') or item.get('fileUrl')
        announced_at = (item.get('an_dt') or item.get('sort_date') or item.get('sortDate')
                        or item.get('broadcastdate') or item.get('date'))
        if not symbol or not subject:
            continue
        results.append({
            'symbol': symbol.strip(),
            'category': category.strip()[:200] if category else None,
            'subject': subject.strip()[:500],
            'attachment_url': attachment.strip() if attachment else None,
            'announced_at': _nse_local_to_utc_iso(announced_at),
        })
    return results


async def fetch_nse_announcements_for_range(session: aiohttp.ClientSession, from_date: str,
                                             to_date: str, debug: bool = False) -> list:
    """Same NSE corporate-announcements endpoint as fetch_nse_announcements,
    but with an explicit from_date/to_date window (DD-MM-YYYY, per NSE's
    documented date-range format) for one-time historical backfills —
    kept as its own function rather than adding params to
    fetch_nse_announcements so the always-on 15-min polling path is
    never at risk of an accidental regression from this."""
    headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
    try:
        async with session.get("https://www.nseindia.com/option-chain", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r0:
            if debug:
                log.info(f"  🔍 NSE cookie-priming (range {from_date}→{to_date}): status={r0.status}")

        url = (f"https://www.nseindia.com/api/corporate-announcements?index=equities"
               f"&from_date={from_date}&to_date={to_date}")
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                key = f'nse_announcements_range_status_{r.status}'
                _fetch_error_counts[key] = _fetch_error_counts.get(key, 0) + 1
                if debug:
                    log.info(f"  🔍 NSE announcements range fetch: non-200 status ({r.status})")
                return []
            data = await r.json()
    except Exception as e:
        _fetch_error_counts[f'nse_announcements_range_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'nse_announcements_range_{type(e).__name__}', 0) + 1
        if debug:
            log.info(f"  🔍 NSE announcements range fetch exception: {type(e).__name__}: {e}")
        return []

    items = data if isinstance(data, list) else data.get('data', []) if isinstance(data, dict) else []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get('symbol') or item.get('smbl')
        category = item.get('desc') or item.get('smSubject') or ''
        subject = (item.get('attchmntText') or item.get('subject') or category or '')
        attachment = item.get('attchmntFile') or item.get('attachmentFile') or item.get('fileUrl')
        announced_at = (item.get('an_dt') or item.get('sort_date') or item.get('sortDate')
                        or item.get('broadcastdate') or item.get('date'))
        if not symbol or not subject:
            continue
        results.append({
            'symbol': symbol.strip(),
            'category': category.strip()[:200] if category else None,
            'subject': subject.strip()[:500],
            'attachment_url': attachment.strip() if attachment else None,
            'announced_at': _nse_local_to_utc_iso(announced_at),
        })
    return results


async def fetch_nse_financial_results(session: aiohttp.ClientSession, debug: bool = False,
                                      from_date: str = None, to_date: str = None) -> list:
    """Latest quarterly financial results from NSE's structured
    corporates-financial-results feed — the same Sales/PAT/EPS numbers
    companies file (as XBRL) alongside the results PDF, so no PDF
    parsing is needed. Field names below are best-effort guesses in the
    same style that worked for announcements: the raw first item is
    logged for the first few cycles so the mapping can be verified
    against reality and corrected if needed."""
    headers = random.choice(_NSE_ANNOUNCEMENTS_HEADER_SETS)
    try:
        async with session.get("https://www.nseindia.com/option-chain", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r0:
            pass
        url = "https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly"
        if from_date and to_date:
            url += f"&from_date={from_date}&to_date={to_date}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                _fetch_error_counts[f'nse_results_status_{r.status}'] = \
                    _fetch_error_counts.get(f'nse_results_status_{r.status}', 0) + 1
                if debug:
                    log.info(f"  🔍 NSE financial-results fetch: non-200 status ({r.status})")
                return []
            data = await r.json()
    except Exception as e:
        _fetch_error_counts[f'nse_results_{type(e).__name__}'] = \
            _fetch_error_counts.get(f'nse_results_{type(e).__name__}', 0) + 1
        if debug:
            log.info(f"  🔍 NSE financial-results fetch exception: {type(e).__name__}: {e}")
        return []

    items = data if isinstance(data, list) else data.get('data', []) if isinstance(data, dict) else []
    if debug:
        log.info(f"  🔍 NSE financial-results fetch: url={url}, got {len(items)} item(s)")
    if debug and items:
        log.info(f"  🔍 NSE financial-results raw response (first item): {json.dumps(items[0])[:1200]}")

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get('symbol') or item.get('smbl')
        period = (item.get('toDate') or item.get('to_date') or item.get('qe_Date')
                  or item.get('period_ended') or '')
        if not symbol or not period:
            continue
        results.append({
            'symbol': str(symbol).strip(),
            'period_ended': str(period).strip()[:40],
            # Real feed shape (verified from raw log 25-Jul): 'consolidated'
            # is "Consolidated"/"Non-Consolidated" and 'audited' is
            # "Audited"/"Un-Audited" — combine both so revisions don't
            # collide on the unique key.
            'result_type': f"{item.get('consolidated') or ''}|{item.get('audited') or ''}"[:40],
            'sales': None,  # numbers come from the per-symbol detail call below
            'pat': None,
            'eps': None,
            'filed_at': _nse_local_to_utc_iso(item.get('broadCastDate') or item.get('filingDate')
                         or item.get('exchdisstime') or item.get('creation_Date')),
            'attachment_url': item.get('xbrl') or item.get('attchmntFile') or item.get('fileName'),
            'xbrl_url': item.get('xbrl'),  # None unless NSE actually has a real XBRL file for
                                            # this filing — kept separate from attachment_url's
                                            # PDF fallback so the XBRL-first parsing path below
                                            # can tell "no XBRL" apart from "has a PDF instead".
        })
    return results


async def fetch_nse_results_numbers(session: aiohttp.ClientSession, headers: dict,
                                    symbol: str, period_ended: str, debug: bool = False) -> dict:
    """The results LIST feed carries no numbers (verified from the raw
    response) — Sales/PAT/EPS live behind NSE's per-symbol
    results-comparision endpoint. Fetches that and picks the period
    matching period_ended. Field names here are best-effort against
    community-documented shapes (re_net_sale / proLossAftTax / re_eps
    etc.); raw response is logged during debug cycles for verification,
    same playbook as the announcements fields."""
    try:
        url = f"https://www.nseindia.com/api/results-comparision?symbol={symbol}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                if debug:
                    log.info(f"  🔍 results-comparision {symbol}: status={r.status}")
                return {}
            data = await r.json()
    except Exception as e:
        if debug:
            log.info(f"  🔍 results-comparision {symbol} exception: {type(e).__name__}: {e}")
        return {}
    items = (data.get('resCmpData') if isinstance(data, dict) else None) or \
            (data.get('data') if isinstance(data, dict) else None) or \
            (data if isinstance(data, list) else [])
    if debug and items:
        log.info(f"  🔍 results-comparision raw (first item, {symbol}): {json.dumps(items[0])[:1000]}")

    def _num(item, *keys):
        for k in keys:
            v = item.get(k)
            if v in (None, '', '-', 'NA'):
                continue
            try:
                return float(str(v).replace(',', ''))
            except (ValueError, TypeError):
                continue
        return None

    target_period = _norm_date(period_ended)
    available_periods = []
    for item in items:
        if not isinstance(item, dict):
            continue
        p = str(item.get('to_date') or item.get('toDate') or item.get('qe_Date') or '').strip()
        if p:
            available_periods.append(p)
        if p and target_period and _norm_date(p) != target_period:
            continue
        # Verified from raw response (VIDEOIND, 25-Jul log): fields are
        # re_-prefixed and values are in ₹ LAKHS (re_con_pro_loss
        # -248985.6 ≈ ₹-2,490 Cr matches Videocon's real losses), so
        # sales/PAT divide by 100 to store crore. EPS is already
        # per-share rupees — no conversion.
        sales_l = _num(item, 're_net_sale', 're_tot_inc_frm_oprs', 're_rev_frm_opr',
                       're_total_inc', 'income', 'net_sales', 'revenue', 'totalIncome')
        pat_l = _num(item, 're_con_pro_loss', 're_pro_loss_aft_tax', 'proLossAftTax',
                     'netProfitLoss', 'pat')
        return {
            'sales': round(sales_l / 100.0, 2) if sales_l is not None else None,
            'pat': round(pat_l / 100.0, 2) if pat_l is not None else None,
            'eps': _num(item, 're_basic_eps', 're_basic_eps_for_cont_dic_opr', 'eps',
                        'basicEPS', 're_dil_eps', 'diluted_eps'),
        }
    if debug:
        # No match found across the WHOLE array, not just item[0] — this
        # is the answer to "is it really not published yet, or did we
        # just miss it": if target_period genuinely isn't among
        # available_periods, NSE hasn't published this quarter's
        # comparison data yet. If it IS in the list but still didn't
        # match, that's a real bug in the date-normalization worth
        # revisiting, not a data-availability issue.
        log.info(f"  🔍 results-comparision {symbol}: no match for target={target_period} "
                 f"among {len(items)} item(s), available periods={sorted(set(available_periods))}")
    return {}


async def fetch_xbrl_url_for_symbol(session: aiohttp.ClientSession, symbol: str,
                                     period_ended: str, debug: bool = False) -> str:
    """Searches the last few days of the results-LIST feed (day-by-day
    calls — a genuine multi-day range is confirmed broken on this
    endpoint, see _results_loop's docstring) for this symbol+period's
    real XBRL file URL."""
    today = datetime.now(timezone.utc)
    target_norm = _norm_date(period_ended)
    any_symbol_match = False
    for days_back in range(3):
        day_str = (today - timedelta(days=days_back)).strftime('%d-%m-%Y')
        rows = await fetch_nse_financial_results(session, debug=False, from_date=day_str, to_date=day_str)
        for r in rows:
            if r.get('symbol') != symbol:
                continue
            any_symbol_match = True
            if debug:
                log.info(f"  📄 {symbol} found in results-list feed ({day_str}): "
                         f"xbrl_url={'yes' if r.get('xbrl_url') else 'NONE'}, "
                         f"period_ended={r.get('period_ended')!r} (norm={_norm_date(r.get('period_ended') or '')!r}) "
                         f"vs target={period_ended!r} (norm={target_norm!r})")
            if r.get('xbrl_url') and _norm_date(r.get('period_ended') or '') == target_norm:
                if debug:
                    log.info(f"  📄 Found XBRL URL for {symbol} ({period_ended}): {r['xbrl_url']}")
                return r['xbrl_url']
        await asyncio.sleep(0.3)
    if debug and not any_symbol_match:
        log.info(f"  📄 {symbol} not found in results-list feed at all across the last 3 days — "
                 f"either it hasn't synced there yet, or this filing type isn't in that feed.")
    return None

# Candidate XBRL element local-names (namespace-agnostic — matched by
# tag name only, ignoring the namespace prefix, since different filers
# can use different taxonomy namespaces for conceptually the same
# concept). These are best-effort guesses at standard Ind-AS/SEBI XBRL
# taxonomy names; logged verbatim on first use so they can be
# verified/corrected against a real filing, same pattern used for
# every other NSE field-mapping in this file.
_XBRL_SALES_TAGS = ['RevenueFromOperations', 'Revenue', 'TotalIncome', 'IncomeFromOperations']
_XBRL_PAT_TAGS = ['ProfitLossForPeriod', 'ProfitLoss', 'NetProfitLoss',
                  'ProfitLossForPeriodFromContinuingOperations']
_XBRL_EPS_TAGS = ['BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations',
                  'BasicEarningsPerShare', 'BasicEPS']


async def fundamentals_worker_main():
    """
    Standalone fundamentals + announcements worker — SERVICE_MODE=
    fundamentals runs ONLY this, never the live scan loop. Meant to run
    as a SEPARATE Railway service (same repo, same codebase, different
    start command), so neither of these can ever block the live scan no
    matter how slow or rate-limited Screener.in/Upstox/NSE get — true
    process isolation, not just a background task sharing the same event
    loop.

    Runs two independent loops concurrently (via asyncio.gather), each
    on its own cadence — announcements are more time-sensitive than
    fundamentals (today's board meeting outcome matters; a slightly
    stale P/E ratio doesn't), so they're checked far more often.
    """
    log.info("=" * 60)
    log.info("  Fundamentals + Announcements Worker — standalone process")
    log.info("  (SERVICE_MODE=fundamentals — live scan runs in a separate service)")
    log.info("=" * 60)

    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await load_instrument_master(session)  # needed for ISIN lookups (Upstox fundamentals API)
        if os.getenv('BACKFILL_ANNOUNCEMENTS_DAYS'):
            try:
                backfill_days = int(os.getenv('BACKFILL_ANNOUNCEMENTS_DAYS'))
                await backfill_announcements_history(session, days=backfill_days)
            except Exception as e:
                log.error(f"Announcements backfill failed: {e}")
        if os.getenv('BACKFILL_RESULTS_DAYS'):
            try:
                await backfill_results_history(session, days=int(os.getenv('BACKFILL_RESULTS_DAYS')))
            except Exception as e:
                log.error(f"Results backfill failed: {e}")
        await asyncio.gather(
            _fundamentals_loop(session),
            _market_cap_catchup_loop(session),
            _announcements_loop(session),
            _results_loop(session),
        )


def rate_announcements_free(rows: list) -> list:
    """Zero-cost rule-based fallback for when ANTHROPIC_API_KEY isn't
    set: keyword rules for positive/negative/neutral, plus regex value
    extraction with big/small judgement against market cap. Cruder than
    the AI path (no real language understanding, no counterparty names)
    but free, instant, and covers the common cases."""
    for r in rows:
        text = ((r.get('category') or '') + ' ' + (r.get('subject') or '')).lower()
        if any(p in text for p in _ANN_NEGATIVE_PATTERNS):
            r['ai_rating'] = 'negative'
        elif any(p in text for p in _ANN_POSITIVE_PATTERNS):
            r['ai_rating'] = 'positive'
        else:
            r['ai_rating'] = 'neutral'
        val = _extract_value_crore(r.get('subject') or '')
        if val and val >= 0.5:
            mcap = r.get('market_cap')
            if mcap and mcap > 0:
                pct = val / mcap * 100
                sig = 'large' if pct >= 10 else 'notable' if pct >= 2 else 'minor'
                r['ai_summary'] = f"₹{val:,.0f} Cr mentioned — {sig} (~{pct:.1f}% of ₹{mcap:,.0f} Cr mcap)"
            else:
                r['ai_summary'] = f"₹{val:,.0f} Cr mentioned in filing"
    return rows


async def rate_announcements_with_ai(session: aiohttp.ClientSession, rows: list) -> list:
    """Tags each announcement with an AI sentiment rating ('positive' /
    'neutral' / 'negative') via one batched Anthropic API call per polling
    cycle (~20 announcements/call, Haiku model — cheap and fast). Entirely
    optional: if ANTHROPIC_API_KEY isn't set, or the call fails for any
    reason, announcements save without a rating rather than being delayed
    or dropped — the rating is decoration on top of the feed, never a
    gate in front of it."""
    api_key = os.getenv('ANTHROPIC_API_KEY', '')
    if not rows:
        return rows
    if not api_key:
        return rate_announcements_free(rows)
    listing = "\n".join(
        f"{i+1}. [{r.get('symbol')}, mcap ₹{int(r['market_cap']) if r.get('market_cap') else '?'} Cr] "
        f"{(r.get('category') or '')}: {(r.get('subject') or '')[:300]}"
        for i, r in enumerate(rows)
    )
    prompt = (
        "You are analyzing Indian stock-exchange corporate announcements for retail investors. "
        "Each numbered line shows the stock's market cap in ₹ crore (or ? if unknown), then the filing text. "
        "For each, return:\n"
        "- r: rating — exactly one of positive, negative, neutral. Use neutral for routine/procedural "
        "filings (certificates, compliance intimations, trading-window closures, record dates, AGM notices, "
        "newspaper-publication copies). Tax/regulatory orders AGAINST the company are negative unless the "
        "outcome favors the company (e.g. demand reduced to NIL — that's positive).\n"
        "- s: a crisp summary of max 110 characters extracting the key facts: order/contract value in ₹ Cr, "
        "number of projects/units, counterparty, and — when both an order value and market cap are known — "
        "significance, e.g. 'large vs ₹850Cr mcap' or 'minor (~0.5% of mcap)'. If the filing has no such "
        "specifics, omit s entirely.\n"
        "Respond ONLY with a JSON array like "
        "[{\"n\":1,\"r\":\"neutral\"},{\"n\":2,\"r\":\"positive\",\"s\":\"₹450Cr NHAI road order — large vs ₹2,100Cr mcap\"}] "
        "— no other text.\n\n"
        + listing
    )
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.warning(f"⚠️ AI rating call failed ({r.status}): {body[:200]} — saving unrated")
                return rows
            data = await r.json()
        text = "".join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
        text = text.replace('```json', '').replace('```', '').strip()
        ratings = json.loads(text)
        valid = {'positive', 'negative', 'neutral'}
        for item in ratings:
            idx = item.get('n')
            rating = (item.get('r') or '').lower().strip()
            summary = (item.get('s') or '').strip()
            if isinstance(idx, int) and 1 <= idx <= len(rows) and rating in valid:
                rows[idx - 1]['ai_rating'] = rating
                if summary:
                    rows[idx - 1]['ai_summary'] = summary[:160]
        rated = sum(1 for r in rows if r.get('ai_rating'))
        log.info(f"  🤖 AI-rated {rated}/{len(rows)} announcements")
    except Exception as e:
        log.warning(f"⚠️ AI rating failed ({type(e).__name__}: {e}) — saving unrated")
    return rows

_ORDER_WIN_PATTERNS = [
    'award of order', 'work order', 'purchase order', 'order worth', 'order valued',
    'bagged', 'bagging', 'receiving of order', 'receipt of order', 'receiving of contract',
    'receipt of contract', 'secures order', 'secured order', 'wins order', 'won order', 'new order',
    'letter of intent', 'contract awarded', 'awarded a contract', 'received an order',
    'order received from',
]


async def save_announcements_to_db(session: aiohttp.ClientSession, rows: list):
    """Upsert on (symbol, subject, announced_at) so re-fetching the same
    announcement across polling cycles doesn't create duplicates."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/corporate_announcements"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    try:
        async with session.post(f"{url}?on_conflict=symbol,subject,announced_at",
                                headers=headers, json=rows,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status not in (200, 201):
                body = await r.text()
                log.warning(f"⚠️ Announcements upsert failed ({r.status}): {body[:300]}")
            else:
                log.info(f"  📢 Upserted {len(rows)} announcements to Supabase")
    except Exception as e:
        log.warning(f"⚠️ Announcements upsert exception: {e}")



async def save_financial_results_to_db(session: aiohttp.ClientSession, rows: list):
    """Upserts structured quarterly results on (symbol, period_ended,
    result_type). Key-normalized for PostgREST's all-keys-must-match
    requirement, same as announcements."""
    if not rows:
        return
    rows = _dedupe_by_key(rows, ('symbol', 'period_ended', 'result_type'))
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    for r in rows:
        for k in all_keys:
            r.setdefault(k, None)
    url = f"{SUPABASE_URL}/rest/v1/financial_results?on_conflict=symbol,period_ended,result_type"
    headers = {
        'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates',
    }
    try:
        async with session.post(url, headers=headers, json=rows,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status in (200, 201, 204):
                log.info(f"  📊 Upserted {len(rows)} financial results to Supabase")
            else:
                body = await r.text()
                log.warning(f"⚠️ Financial results upsert failed ({r.status}): {body[:300]}")
    except Exception as e:
        log.warning(f"⚠️ Financial results upsert error: {type(e).__name__}: {e}")


def tag_order_size(rows: list) -> list:
    """For business order-win announcements, tags order_size as
    'big' (≥10% of market cap), 'medium' (2–10%), or 'small' (<2%) —
    powering the Order Book tab's Big/Medium/Small sub-filters. Runs
    after rating regardless of whether the AI or free path did the
    rating, so the tag exists either way. Order-win detection reuses the
    same phrase list as the frontend's Order Book tab, so a filing
    tagged here is one that tab will actually show."""
    for r in rows:
        text = ((r.get('category') or '') + ' ' + (r.get('subject') or '')).lower()
        if not any(p in text for p in _ORDER_WIN_PATTERNS):
            continue
        if any(p in text for p in _ORDER_CANCEL_PATTERNS):
            continue
        val = _extract_value_crore(r.get('subject') or '')
        if not val:
            # No ₹ figure anywhere in the text — there's no way to size
            # this at all, with or without market cap (e.g. "...has
            # informed the Exchange about Bagging/Receiving of orders"
            # with no amount stated). Stays untagged, correctly shows
            # only in "All Sizes", never Big/Medium/Small — this is a
            # real limitation of the source data, not something any
            # amount of code can work around.
            continue
        mcap = r.get('market_cap')
        if mcap and mcap > 0:
            pct = val / mcap * 100
            r['order_size'] = 'big' if pct >= 10 else 'medium' if pct >= 2 else 'small'
        else:
            # Market cap not known for this stock yet — fall back to
            # absolute ₹ value thresholds so the filing still gets SOME
            # size tag instead of silently never appearing in
            # Big/Medium/Small. Less precise than %-of-mcap (₹50 Cr is
            # huge for a micro-cap, trivial for a giant), but far better
            # than excluding every stock whose market cap hasn't been
            # fetched yet — which was most stocks for a lot of this
            # session before the market-cap fixes.
            r['order_size'] = 'big' if val >= 500 else 'medium' if val >= 50 else 'small'
    return rows

