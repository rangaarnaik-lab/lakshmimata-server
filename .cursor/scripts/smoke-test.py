#!/usr/bin/env python3
"""Read-only Cloud Agent smoke test for Lakshmimata Server."""
import asyncio
import os
import sys

import aiohttp


REQUIRED = ("UPSTOX_ANALYTICS_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")


def check_env() -> None:
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing required secrets: {', '.join(missing)}")


async def check_upstox(session: aiohttp.ClientSession) -> int:
    token = os.environ["UPSTOX_ANALYTICS_TOKEN"]
    url = "https://api.upstox.com/v2/instruments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    instruments = data.get("data", [])
    nse_eq = [
        i["trading_symbol"]
        for i in instruments
        if i.get("exchange") == "NSE"
        and i.get("instrument_type") == "EQ"
        and i.get("trading_symbol")
    ]
    return len(nse_eq)


async def check_supabase(session: aiohttp.ClientSession) -> int:
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/stock_fundamentals?select=sym&limit=1"
    headers = {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
    }
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        rows = await resp.json()
    return len(rows)


async def main() -> None:
    check_env()

    # Import after env check so missing secrets fail with a clear message.
    from shared import get_sector, is_market_open

    assert get_sector("TCS") == "IT"
    market_open = is_market_open()
    print(f"shared helpers ok (TCS sector=IT, market_open={market_open})")

    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        nse_count = await check_upstox(session)
        print(f"Upstox instruments ok ({nse_count} NSE equities)")

        row_count = await check_supabase(session)
        print(f"Supabase read ok (stock_fundamentals sample rows={row_count})")

    print("smoke test passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
