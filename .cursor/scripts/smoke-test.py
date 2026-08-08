#!/usr/bin/env python3
"""Cloud Agent smoke test using the frontend's committed Supabase .env.

Backend Railway workers still expect process env vars when you run them; this
smoke path does not require separate Cloud Agent secrets because Lakshmimata
already ships VITE_SUPABASE_* (and can read owner_token from Supabase).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import aiohttp

SERVER_ROOT = Path(__file__).resolve().parents[2]


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


def bootstrap_from_frontend_env(frontend: Path) -> tuple[str, str]:
    """Map frontend Vite env into names shared.py expects (read-only use)."""
    env = load_dotenv(frontend / ".env")
    url = os.getenv("SUPABASE_URL") or env.get("VITE_SUPABASE_URL") or ""
    key = (
        os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("VITE_SUPABASE_ANON_KEY")
        or env.get("VITE_SUPABASE_ANON_KEY")
        or ""
    )
    token = (
        os.getenv("UPSTOX_ANALYTICS_TOKEN")
        or os.getenv("VITE_OWNER_UPSTOX_TOKEN")
        or env.get("VITE_OWNER_UPSTOX_TOKEN")
        or ""
    )
    if not url or not key:
        raise SystemExit(
            f"Missing Supabase URL/key in {frontend / '.env'} "
            "(expected VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY)"
        )
    os.environ.setdefault("SUPABASE_URL", url)
    os.environ.setdefault("SUPABASE_SERVICE_KEY", key)
    # shared.py imports require this name even for local helpers.
    os.environ.setdefault("UPSTOX_ANALYTICS_TOKEN", token or "unused-for-local-helpers")
    return url, key


async def check_supabase(session: aiohttp.ClientSession, url: str, key: str) -> int:
    endpoint = f"{url.rstrip('/')}/rest/v1/stock_fundamentals?select=sym&limit=1"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with session.get(endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        rows = await resp.json()
    return len(rows)


async def check_owner_token(session: aiohttp.ClientSession, url: str, key: str) -> None:
    endpoint = f"{url.rstrip('/')}/rest/v1/owner_token?select=id&limit=1"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with session.get(endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        rows = await resp.json()
    if not rows:
        raise SystemExit("owner_token table readable but empty")
    print("Supabase owner_token readable")


def check_frontend_build(frontend: Path) -> None:
    subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    dist = frontend / "dist" / "index.html"
    if not dist.is_file():
        raise SystemExit(f"frontend build missing {dist}")
    print(f"frontend build ok ({dist})")


async def main() -> None:
    frontend = find_frontend()
    if frontend is None:
        raise SystemExit("frontend checkout not found — run .cursor/scripts/install.sh first")

    url, key = bootstrap_from_frontend_env(frontend)
    print(f"using Supabase credentials from {frontend / '.env'}")

    sys.path.insert(0, str(SERVER_ROOT))
    from shared import get_sector, is_market_open

    assert get_sector("TCS") == "IT"
    market_open = is_market_open()
    print(f"shared helpers ok (TCS sector=IT, market_open={market_open})")

    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        row_count = await check_supabase(session, url, key)
        print(f"Supabase read ok (stock_fundamentals sample rows={row_count})")
        await check_owner_token(session, url, key)

    check_frontend_build(frontend)
    print("smoke test passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
