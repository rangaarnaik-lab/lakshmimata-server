#!/usr/bin/env python3
"""
PocketRS Pro — Service Dispatcher
===================================
This used to be one 8600-line file shared by both Railway services
(angelic-strength, the live scanner, and worthy-simplicity, the
fundamentals+announcements worker), branching internally on the
SERVICE_MODE environment variable. Split on 2026-07-30 into three
files for cleaner separation and lower risk of one service's changes
accidentally affecting the other (this is what caused the
stock_history schema-mismatch bug: fields added for angelic-strength's
AI Best Picks feature broke worthy-simplicity's... actually the
reverse — fields added to the shared `processed` dict broke a table
only angelic-strength writes to. Splitting the files doesn't remove
every such risk by itself, but it does make each service's actual
code footprint smaller and clearer, and removes the possibility of
one service's code accidentally referencing something the other
doesn't have loaded):

  shared.py               - constants, config, and functions genuinely
                             used by both services (sector/industry
                             lookup, fundamentals fetching + the shared
                             DB table, is_market_open, etc.)
  live_scan.py             - angelic-strength only: all technical
                             analysis (RS/VCP/patterns/etc.), run_scan,
                             AI Best Picks, all the daily self-healing
                             backfills, and the live-scan service loop.
  fundamentals_worker.py   - worthy-simplicity only: NSE announcements,
                             XBRL/results parsing, and their loops.

This file just reads the same SERVICE_MODE env var both Railway
services have always used and calls the right one — no Railway
configuration changes needed, this is a drop-in replacement for the
old main.py.
"""
import os
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('pocketrs')

if __name__ == '__main__':
    if os.getenv('SERVICE_MODE', '').lower() == 'fundamentals':
        from fundamentals_worker import fundamentals_worker_main
        asyncio.run(fundamentals_worker_main())
    else:
        from live_scan import run_live_scan_service
        asyncio.run(run_live_scan_service())
