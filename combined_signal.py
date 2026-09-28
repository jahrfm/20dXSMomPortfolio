#!/usr/bin/env python3
"""combined_signal.py — generate the daily COMBINED signal book (schema 2).

Runs on the Hermes box shortly after 00:00 UTC. Fetches public Bybit daily
klines, drops the still-forming candle, and calls strategy.build_signal() —
the exact function the backtest (backtest/combined_backtest.py) uses — for
the last completed UTC day.

Output: combined_signals_<asof>.json
  {
    "schema": 2, "asof": "YYYY-MM-DD", "trade_date": asof+1, "generated_at",
    "universe": [...top-50 by 30d turnover...],
    "chand4": {"longs": [...], "shorts": [...]},   # ranked; each has
              # symbol, signal_close, initial_stop, atr14, strength
    "xsmom":  {"is_rebalance": bool, "rebalance_date", "longs": [...]},
    "meta": {...}
  }

The VPS executor only ever trades a signal whose asof is yesterday (UTC).

Usage:
  python3 combined_signal.py [--asof YYYY-MM-DD] [--out DIR]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from combined_exec.market import Market  # noqa: E402
from combined_exec.strategy import DEFAULTS, build_signal, utc_today  # noqa: E402

PREFILTER = 120      # fetch klines for the top-N by 24h turnover, then rank by 30d


def log(msg):
    print(f"[combined_signal] {msg}", flush=True)


def generate(asof=None, market=None, p=DEFAULTS):
    market = market or Market(os.environ.get("COMBINED_DATA_URL", "https://api.bybit.com"))
    today = utc_today()
    asof = asof or (today - timedelta(days=1)).isoformat()
    before = (datetime.fromisoformat(asof).date() + timedelta(days=1)).isoformat()
    tradable = set(market.tradable_usdt_perps())
    tick = market.tickers()
    cands = sorted((s for s in tick if s in tradable),
                   key=lambda s: -tick[s]["turnover24h"])[:PREFILTER]
    series, failed = {}, []
    for sym in cands:
        try:
            s = market.series(sym, limit=300, before=before)
        except Exception as e:
            failed.append(f"{sym}: {e}")
            continue
        if len(s) and s.dates[-1] == asof:
            series[sym] = s
    if len(series) < DEFAULTS["universe_size"]:
        raise RuntimeError(f"only {len(series)} symbols have a completed {asof} bar "
                           f"({len(failed)} fetch failures) — refusing to publish")
    sig = build_signal(series, asof, p)
    sig.update({
        "schema": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": {"fetched": len(series), "fetch_failures": failed[:20],
                 "params": {k: v for k, v in p.items() if not k.endswith("risk_pct")}},
    })
    return sig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", "--date", dest="asof", default=None,
                    help="completed UTC day to signal on (default: yesterday)")
    ap.add_argument("--out", default=os.environ.get(
        "THESES_DIR", "/home/jose/workspace/hypertracker-data"))
    args = ap.parse_args()

    sig = generate(args.asof)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"combined_signals_{sig['asof']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sig, f, indent=2)
    os.replace(tmp, path)
    c, x = sig["chand4"], sig["xsmom"]
    log(f"wrote {path}")
    log(f"  universe {len(sig['universe'])} | CHAND4 {len(c['longs'])} long / "
        f"{len(c['shorts'])} short breakouts: "
        + ", ".join(f"{k['symbol']}({k['direction'][0]})" for k in (c['longs'] + c['shorts'])[:10]))
    log(f"  XSMOM trade day {sig['trade_date']} rebalance={x['is_rebalance']} "
        f"(next {x['rebalance_date']}) longs={[k['symbol'] for k in x['longs']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
