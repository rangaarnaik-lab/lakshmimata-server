#!/usr/bin/env python3
"""Recompute stored Bull Snort data with the corrected rule.

Bull Snort shipped with looser thresholds than the chart's Lakshmi Volume
pane (2x of a 20-bar volume average and DCR 70, vs 3x of a 50-bar average
and DCR 65). Every stocks row and every fired Bull Snort alert written
before that fix used the loose rule, so the screener disagreed with the
chart and alerts fired on bars carrying no icon.

This reads stock_full_history (the same series the chart draws), reapplies
bull_snort.detect_bull_snort, and patches public.stocks. Safe to re-run —
the live scan recomputes the same values on its next pass.

Usage:
  python scripts/backfill_bull_snort.py                 # report only
  python scripts/backfill_bull_snort.py --apply         # write stocks
  python scripts/backfill_bull_snort.py --apply --purge-alerts
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from bull_snort import (  # noqa: E402  (needs sys.path above)
    BULL_SNORT_AVG_LEN,
    BULL_SNORT_MIN_DCR,
    BULL_SNORT_MIN_REL_VOL,
    detect_bull_snort,
)

# The frontend checkout sits beside this repo on some machines and inside it
# on others; try both before giving up.
ENV_CANDIDATES = (
    SERVER_ROOT / ".env",
    SERVER_ROOT / "Lakshmimata" / ".env",
    SERVER_ROOT.parent / "Lakshmimata" / ".env",
)
PAGE = 200


def load_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY")
    for env_file in ENV_CANDIDATES:
        if url and key:
            break
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ("SUPABASE_URL", "VITE_SUPABASE_URL") and not url:
                url = v
            if k in ("SUPABASE_SERVICE_KEY", "VITE_SUPABASE_ANON_KEY") and not key:
                key = v
    if not url or not key:
        sys.exit("Missing Supabase URL/key (SUPABASE_URL + SUPABASE_SERVICE_KEY)")
    return url.rstrip("/"), key


def _request(base: str, key: str, path: str, method: str = "GET", body=None) -> list:
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=minimal"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else []


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    return v if isinstance(v, list) else []


def main() -> None:
    apply = "--apply" in sys.argv
    purge_alerts = "--purge-alerts" in sys.argv
    base, key = load_env()

    print(
        f"Rule: rel vol >= {BULL_SNORT_MIN_REL_VOL}x the {BULL_SNORT_AVG_LEN}-bar "
        f"volume average, close above prior close, DCR >= {BULL_SNORT_MIN_DCR * 100:.0f}%"
    )
    print("Mode:", "APPLY (writing to Supabase)" if apply else "DRY RUN (report only)")

    current = {}
    offset = 0
    while True:
        rows = _request(
            base, key,
            "/rest/v1/stocks?select=sym,is_bull_snort,bull_snort_vol_ratio"
            f"&limit=500&offset={offset}",
        )
        if not rows:
            break
        for r in rows:
            current[str(r.get("sym") or "").strip().upper()] = r
        if len(rows) < 500:
            break
        offset += 500
    print(f"Loaded {len(current)} stocks rows")

    scanned = turned_on = turned_off = unchanged = short_history = 0
    updated = 0
    flipped_syms: list[str] = []
    offset = 0
    while True:
        rows = _request(
            base, key,
            "/rest/v1/stock_full_history"
            "?select=sym,opens,highs,lows,prices,volumes"
            f"&limit={PAGE}&offset={offset}",
        )
        if not rows:
            break
        for row in rows:
            sym = str(row.get("sym") or "").strip().upper()
            if not sym:
                continue
            closes = as_list(row.get("prices"))
            volumes = as_list(row.get("volumes"))
            if len(closes) < BULL_SNORT_AVG_LEN + 1 or len(volumes) < BULL_SNORT_AVG_LEN + 1:
                short_history += 1
                continue
            scanned += 1
            res = detect_bull_snort(
                as_list(row.get("opens")),
                as_list(row.get("highs")),
                as_list(row.get("lows")),
                closes,
                volumes,
            )
            was = current.get(sym) or {}
            was_on = bool(was.get("is_bull_snort"))
            now_on = bool(res["is_bull_snort"])
            was_ratio = was.get("bull_snort_vol_ratio")
            try:
                ratio_same = was_ratio is not None and abs(float(was_ratio) - res["bull_snort_vol_ratio"]) < 0.01
            except (TypeError, ValueError):
                ratio_same = False

            if now_on and not was_on:
                turned_on += 1
                flipped_syms.append(f"+{sym}")
            elif was_on and not now_on:
                turned_off += 1
                flipped_syms.append(f"-{sym}")
            elif ratio_same:
                unchanged += 1
                continue

            if apply and sym in current:
                _request(
                    base, key,
                    f"/rest/v1/stocks?sym=eq.{urllib.parse.quote(sym, safe='')}",
                    method="PATCH",
                    body={
                        "is_bull_snort": now_on,
                        "bull_snort_vol_ratio": res["bull_snort_vol_ratio"],
                    },
                )
                updated += 1
                if updated % 200 == 0:
                    print(f"  … {updated} stocks patched", flush=True)
        if len(rows) < PAGE:
            break
        offset += PAGE

    print(
        f"\nScanned {scanned} symbols "
        f"({short_history} skipped for <{BULL_SNORT_AVG_LEN + 1} bars of history)"
    )
    print(f"  now Bull Snort (was not): {turned_on}")
    print(f"  no longer Bull Snort:     {turned_off}")
    print(f"  identical flag and ratio: {unchanged}")
    if flipped_syms:
        print("  flips:", " ".join(flipped_syms[:40]) + (" …" if len(flipped_syms) > 40 else ""))
    print(f"  stocks rows patched:      {updated}")

    stale = _request(
        base, key,
        "/rest/v1/squeeze_alerts?select=id,sym,fired_at,fire_type"
        "&fire_type=ilike.*Bull*Snort*&limit=2000",
    )
    print(f"\nBull Snort rows in squeeze_alerts: {len(stale)}")
    if stale and not purge_alerts:
        print("  These fired under the old loose rule. Re-run with --purge-alerts to delete them.")
    elif stale and purge_alerts:
        if not apply:
            print("  --purge-alerts needs --apply too; nothing deleted.")
        else:
            _request(
                base, key,
                "/rest/v1/squeeze_alerts?fire_type=ilike.*Bull*Snort*",
                method="DELETE",
            )
            print(f"  Deleted {len(stale)} stale Bull Snort alert rows.")

    if not apply:
        print("\nNothing was written. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
