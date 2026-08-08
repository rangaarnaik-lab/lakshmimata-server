#!/usr/bin/env python3
"""Read-only Cloud Agent smoke test for Lakshmimata server (+ optional frontend)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import aiohttp

REQUIRED = ("UPSTOX_ANALYTICS_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")
SERVER_ROOT = Path(__file__).resolve().parents[2]


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
    return sum(
        1
        for i in instruments
        if i.get("exchange") == "NSE"
        and i.get("instrument_type") == "EQ"
        and i.get("trading_symbol")
    )


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


def find_frontend() -> Path | None:
    candidates = [
        SERVER_ROOT.parent / "Lakshmimata",
        SERVER_ROOT / "Lakshmimata",
        Path("/workspace/Lakshmimata"),
        Path("/opt/cursor/lakshmimata-stack/Lakshmimata"),
    ]
    for path in candidates:
        if (path / "package.json").is_file():
            return path.resolve()
    return None


def check_frontend_build(frontend: Path) -> None:
    env = os.environ.copy()
    env.setdefault("VITE_SUPABASE_URL", "https://example.supabase.co")
    env.setdefault("VITE_SUPABASE_ANON_KEY", "placeholder")
    subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend,
        env=env,
        check=True,
    )
    dist = frontend / "dist" / "index.html"
    if not dist.is_file():
        raise SystemExit(f"frontend build missing {dist}")
    print(f"frontend build ok ({dist})")


async def main() -> None:
    check_env()

    sys.path.insert(0, str(SERVER_ROOT))
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

    frontend = find_frontend()
    if frontend is None:
        print("frontend not found; skipped frontend build check")
    else:
        check_frontend_build(frontend)

    print("smoke test passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
