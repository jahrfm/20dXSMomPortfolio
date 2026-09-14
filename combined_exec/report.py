#!/usr/bin/env python3
"""combined_exec/report.py — position + PnL report for the combined sub-account.

Reads live positions + closed PnL + open orders via the Bybit client and
prints a per-leg summary (CHAND4 vs XSMOM attribution by orderLinkId prefix).

Usage:
  python3 -m combined_exec.report [--days 7]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
for d in ("/opt/bybit-execution-engine", "/home/jose/workspace/bybit-execution-engine"):
    if d not in sys.path and os.path.isdir(os.path.join(d, "bybit_exec")):
        sys.path.insert(0, d)
        break

from combined_exec.config import combined_config
from bybit_exec.bybit_client import BybitClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = combined_config()
    client = BybitClient(cfg)

    bal = client.wallet_balance()
    positions = client.positions()
    orders = client.order_realtime()
    closed = client.closed_pnl(days=args.days)

    # attribute by orderLinkId prefix where available; fallback by symbol
    def leg_of(item):
        link = (item.get("orderLinkId") or item.get("orderId") or "")
        if link.startswith("CH4-"):
            return "CHAND4"
        if link.startswith("XSM-"):
            return "XSMOM"
        return "UNKNOWN"

    leg_pnl = {"CHAND4": 0.0, "XSMOM": 0.0, "UNKNOWN": 0.0}
    for c in closed:
        leg_pnl[leg_of(c)] += float(c.get("closedPnl") or 0)

    leg_pos = {"CHAND4": 0, "XSMOM": 0, "UNKNOWN": 0}
    for p in positions:
        leg_pos[leg_of(p)] += 1

    out = {
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "balance": bal,
        "open_positions": len(positions),
        "open_orders": len(orders),
        f"closed_pnl_{args.days}d": leg_pnl,
        "positions_by_leg": leg_pos,
        "positions_detail": positions,
    }
    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"COMBINED sub-account report ({out['asof']})")
    print(f"  balance: totalEquity={bal.get('totalEquity')} "
          f"available={bal.get('availableBalance')} uPnL={bal.get('unrealisedPnl')}")
    print(f"  open positions: {len(positions)} | open orders: {len(orders)}")
    print(f"  closed PnL ({args.days}d): CHAND4={leg_pnl['CHAND4']:.2f} "
          f"XSMOM={leg_pnl['XSMOM']:.2f} other={leg_pnl['UNKNOWN']:.2f}")
    print(f"  positions by leg: {leg_pos}")
    for p in positions:
        print(f"    {p['symbol']:<14} {p['side']:<5} size={p['size']:<12} "
              f"avg={p['avgPrice']} mark={p['markPrice']} "
              f"uPnL={p['unrealisedPnl']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
