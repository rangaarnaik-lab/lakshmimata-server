#!/usr/bin/env python3
"""Backfill financial_results.opm_pct from PBT − other income when null.

Production computeResultRating() only reads stored opm_pct (not derived OPM),
so null OPM lets strong YoY print as Excellent (e.g. APOLLO). Safe to re-run.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ENV = SERVER_ROOT / "Lakshmimata" / ".env"


def load_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY")
    if FRONTEND_ENV.is_file():
        for line in FRONTEND_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "VITE_SUPABASE_URL" and not url:
                url = v
            if k == "VITE_SUPABASE_ANON_KEY" and not key:
                key = v
    if not url or not key:
        sys.exit("Missing Supabase URL/key")
    return url.rstrip("/"), key


def api_get(base: str, key: str, path: str) -> list:
    req = urllib.request.Request(
        f"{base}{path}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def api_patch(base: str, key: str, path: str, body: dict) -> None:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=60):
        pass


def derive_opm(row: dict) -> float | None:
    sales = row.get("sales")
    pbt = row.get("pbt")
    oi = row.get("other_income")
    if sales in (None, 0) or pbt is None or oi is None:
        return None
    return round((pbt - oi) / sales * 100, 2)


def main() -> None:
    base, key = load_env()
    offset = 0
    page = 500
    updated = 0
    skipped = 0
    while True:
        q = (
            "/rest/v1/financial_results"
            "?select=symbol,period_ended,sales,pbt,other_income,opm_pct"
            "&opm_pct=is.null"
            "&sales=not.is.null"
            "&pbt=not.is.null"
            "&other_income=not.is.null"
            f"&limit={page}&offset={offset}"
        )
        rows = api_get(base, key, q)
        if not rows:
            break
        for row in rows:
            opm = derive_opm(row)
            if opm is None:
                skipped += 1
                continue
            sym = urllib.parse.quote(str(row["symbol"]), safe="")
            pe = urllib.parse.quote(str(row["period_ended"]), safe="")
            api_patch(
                base,
                key,
                f"/rest/v1/financial_results?symbol=eq.{sym}&period_ended=eq.{pe}",
                {"opm_pct": opm},
            )
            updated += 1
            if updated % 200 == 0:
                print(f"  … {updated} rows updated", flush=True)
        if len(rows) < page:
            break
        offset += page
    print(f"Done: updated {updated}, skipped {skipped}")


if __name__ == "__main__":
    main()
