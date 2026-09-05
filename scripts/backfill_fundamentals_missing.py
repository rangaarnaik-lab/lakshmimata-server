#!/usr/bin/env python3
"""One-off backfill: load missing P/E + market cap for all NSE stocks.

Runs the exact production startup flow (load_fundamentals_from_supabase →
load_fundamentals_batch) so it flags every row that is blank or missing
market_cap, then re-fetches it through the layered fallbacks already in
shared.py:

  1. Upstox key-ratios        -> P/E (primary)
  2. NSE quote-equity         -> P/E + industry (official, when Upstox gaps)
  3. Yahoo quoteSummary       -> market_cap + shares_outstanding + P/E

market_cap is then kept daily-fresh by run_scan (shares_outstanding × live
price), so this only needs to run once per stock (or whenever fields are
newly missing). Idempotent and safe to re-run: it respects the TTL cache and
the existing missing-market-cap catch-up conditions.

Usage:
    python scripts/backfill_fundamentals_missing.py [--limit N] [--only SYM,...]

Requires env / frontend .env with:
    SUPABASE_URL, SUPABASE_SERVICE_KEY, UPSTOX_ANALYTICS_TOKEN
"""
from __future__ import annotations

import asyncio
import argparse
import os
import sys
from pathlib import Path

import aiohttp

SERVER_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def bootstrap_env() -> None:
    """Map frontend Vite env (or real process env) into shared.py's names."""
    candidates = [
        SERVER_ROOT.parent / "Lakshmimata",
        SERVER_ROOT / "Lakshmimata",
    ]
    env: dict[str, str] = {}
    for path in candidates:
        env.update(load_dotenv(path / ".env"))

    def pick(process_key, vite_key, env_key, fallback=""):
        return (os.getenv(process_key)
                or os.getenv(vite_key)
                or env.get(vite_key)
                or env.get(env_key)
                or fallback)

    url = pick("SUPABASE_URL", "VITE_SUPABASE_URL", "SUPABASE_URL")
    key = pick("SUPABASE_SERVICE_KEY", "VITE_SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY")
    token = pick("UPSTOX_ANALYTICS_TOKEN", "VITE_OWNER_UPSTOX_TOKEN", "UPSTOX_ANALYTICS_TOKEN")

    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (or VITE_SUPABASE_*.).")
    os.environ.setdefault("SUPABASE_URL", url)
    os.environ.setdefault("SUPABASE_SERVICE_KEY", key)
    # shared.py requires this even for local helpers.
    os.environ.setdefault("UPSTOX_ANALYTICS_TOKEN", token or "unused-for-local-helpers")


async def main() -> None:
    bootstrap_env()

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N symbols (0 = all missing)")
    parser.add_argument("--only", default="",
                        help="comma-separated symbol list to force-refresh (overrides --limit)")
    args = parser.parse_args()

    sys.path.insert(0, str(SERVER_ROOT))
    from shared import (ALL_STOCKS, load_fundamentals_from_supabase,
                        load_fundamentals_batch, log)

    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        stale = await load_fundamentals_from_supabase(session)

        forced = [s.strip().upper() for s in args.only.split(",") if s.strip()]
        if forced:
            targets = forced
            log.info(f"Force-refreshing {len(targets)} symbols: {targets}")
        else:
            targets = stale
            log.info(f"{len(stale)} symbols need fetching (blank / missing / stale).")

        if args.limit and not forced and len(targets) > args.limit:
            targets = sorted(targets)[:args.limit]
            log.info(f"Limited to first {len(targets)} symbols.")

        if not targets:
            log.info("Nothing to fetch.")
            return

        log.info(f"→ Backfilling fundamentals for {len(targets)} stocks with "
                 f"layered Upstox/NSE/Yahoo fallbacks…")
        await load_fundamentals_batch(session, targets)

    log.info("Done. Verify market_cap / pe counts in stock_fundamentals.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)