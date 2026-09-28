#!/usr/bin/env python3
"""combined_exec/report.py — positions + per-leg P&L for the combined book.

Leg attribution comes from the executor's state file (Bybit's position and
closed-PnL lists carry no order tags). Works in paper and live mode.

Usage:
  python3 -m combined_exec.report [--days 30] [--json]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
for d in (os.environ.get("DMA_ENGINE_DIR", ""), "/opt/bybit-execution-engine",
          "/home/jose/workspace/bybit-execution-engine"):
    if d and d not in sys.path and os.path.isdir(os.path.join(d, "bybit_exec")):
        sys.path.insert(0, d)
        break

from combined_exec.broker import BybitBroker, PaperBroker  # noqa: E402
from combined_exec.config import combined_config  # noqa: E402
from combined_exec.market import Market  # noqa: E402
from combined_exec.state import State  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = combined_config()
    market = Market(cfg["data_url"])
    broker = BybitBroker(cfg, print) if cfg["auto_trade"] else PaperBroker(cfg, market, print)
    state = State.load(os.path.join(cfg["state_dir"], f"state_{broker.mode}.json"))
    held = state.positions
    marks = broker.marks(set(held))
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()

    legs = {}
    for h in state.data["history"]:
        if (h.get("exit_date") or "") < since:
            continue
        g = legs.setdefault(h["leg"], {"closed": 0, "pnl": 0.0, "wins": 0})
        g["closed"] += 1
        g["pnl"] += h.get("pnl") or 0.0
        g["wins"] += (h.get("pnl") or 0) > 0
    open_rows = []
    for s, h in held.items():
        px = marks.get(s)
        sign = 1 if h["side"] == "LONG" else -1
        upnl = h["qty"] * (px - h["entry_price"]) * sign if px else None
        open_rows.append({"symbol": s, "leg": h["leg"], "side": h["side"], "qty": h["qty"],
                          "entry": h["entry_price"], "entry_date": h["entry_date"],
                          "mark": px, "stop": h["stop"], "upnl": upnl,
                          "r_open": upnl / h["risk"] if upnl is not None and h.get("risk") else None})
    out = {"mode": broker.mode, "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "equity": broker.equity(), "last_signal": state.data.get("last_signal_asof"),
           "last_run": state.data.get("last_run"), "open": open_rows,
           f"closed_{args.days}d_by_leg": legs}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"COMBINED book [{out['mode']}] {out['asof']}  equity {out['equity']:.2f}  "
          f"last signal {out['last_signal']}  last run {out['last_run']}")
    print(f"closed in last {args.days}d by leg:")
    for leg, g in sorted(legs.items()):
        print(f"  {leg:<7} {g['closed']:>3} trades  pnl {g['pnl']:+.2f}  wins {g['wins']}")
    print(f"open positions ({len(open_rows)}):")
    for r in sorted(open_rows, key=lambda r: (r["leg"], r["symbol"])):
        upnl = f"{r['upnl']:+.2f}" if r["upnl"] is not None else "n/a"
        rr = f"{r['r_open']:+.2f}R" if r["r_open"] is not None else ""
        print(f"  {r['leg']:<7} {r['side']:<5} {r['symbol']:<16} qty {r['qty']:<12g} "
              f"entry {r['entry']:<12g} mark {r['mark']} stop {r['stop']:<12g} uPnL {upnl} {rr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
