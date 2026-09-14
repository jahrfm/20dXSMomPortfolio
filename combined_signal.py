#!/usr/bin/env python3
"""combined_signal.py — generate the daily COMBINED strategy order book.

Combines two signal engines into one dated JSON that the VPS executor consumes:
  - CHAND4 40/15 (from the 20d breakout research): daily scan for new 40-day
    highs/lows, top-5 per side by vol-normalised strength, entry next open,
    exit on 4xATR chandelier trail.
  - XSMOM (from the MOMSXperp repo): biweekly cross-sectional momentum,
    top-5 longs (MA100 + raw-positive filter), 15% hard stop, 14-day hold.

Output:  combined_signals_<date>.json  (in THESES_DIR, default the git-bus
data repo dir) with sections:
  {
    "date": ..., "generated_at": ...,
    "chand4":  {"candidates": [ {symbol, direction, strength, entry_guess,
                                 signal_date} ], "asof": ...},
    "xsmom":   {"longs": [...], "shorts": [...], "rebalance_date": ...,
                 "is_rebalance": bool},
    "meta": {"universe": N, "note": "signal-only; sizing/execution on VPS"}
  }

Run on the Hermes box (has the strategy engines + data). The VPS NEVER runs
this — it only consumes the JSON.

Usage:
  python3 combined_signal.py [--date YYYY-MM-DD] [--out DIR] [--xsmom-json PATH]
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

# --- locate strategy engines -------------------------------------------------
WORKSPACE = "/home/jose/workspace"
sys.path.insert(0, WORKSPACE)

# THESES_DIR: where signal JSONs land (git-bus data repo on Hermes push path).
THESES_DIR = os.environ.get(
    "THESES_DIR", os.path.join(WORKSPACE, "hypertracker-data"))

CHAND4_LOOKBACK = 40
CHAND4_TRAIL = 15
CHAND4_CHANDELIER = 4.0
CHAND4_TOP_N = 5


def log(msg):
    print(f"[combined_signal] {msg}", flush=True)


def load_bybit_daily(top_n=43):
    """Load Bybit daily OHLCV for the strategy universe (cached where possible).

    Uses the MOMSXperp fetch (public Bybit V5) so it works on any box with
    network; falls back to the 20d repo's cached /tmp/bybit_5y.json if present
    (deterministic backtest consistency).
    """
    cache5y = "/tmp/bybit_5y.json"
    if os.path.exists(cache5y):
        with open(cache5y) as f:
            data = json.load(f)
        # keep newest 250 bars per symbol (enough for 40d lookback + ATR warmup)
        out = {}
        for sym, bars in data.items():
            if isinstance(bars, list) and len(bars) >= 100:
                tail = bars[-260:]
                out[sym] = [b if isinstance(b, list) else
                            [b["date"], b["open"], b["high"], b["low"], b["close"]]
                            for b in tail]
        if out:
            log(f"loaded {len(out)} syms from /tmp/bybit_5y.json cache")
            return out
    # live fetch fallback
    try:
        from momsxperp.fetch import get_volume_ranked_universe, fetch_daily_klines
        universe = get_volume_ranked_universe(top_n)
        out = {}
        for item in universe:
            try:
                rows = fetch_daily_klines(item["symbol"], days=300)
            except Exception:
                continue
            # rows newest-first; keep oldest-first for indicator math
            rows_asc = sorted(rows, key=lambda r: r[0])
            if len(rows_asc) >= 100:
                out[item["symbol"]] = [
                    [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                     r[1], r[2], r[3], r[4]] for r in rows_asc
                ]
        log(f"live-fetched {len(out)} syms")
        return out
    except Exception as e:
        log(f"WARNING: no data source available: {e}")
        return {}


def chand4_signal(data_map, asof_date=None):
    """CHAND4 40/15 breakout candidates as of the last COMPLETED daily bar.

    When asof_date is None, uses the last completed daily candle (the bar
    whose date < today UTC) — the forming candle is NEVER used (no lookahead).
    Returns list of candidate dicts (signal on the completed bar, entry would
    be the NEXT bar's open — the executor decides actual entry).
    """
    if asof_date is None:
        # last completed daily bar across all symbols
        today = date.today().isoformat()
        asof_date = max(
            (b[0] for bars in data_map.values() for b in bars if b[0] < today),
            default=today,
        )
    candidates = []
    for sym, bars in data_map.items():
        # bars oldest-first with [date, open, high, low, close]
        if len(bars) < CHAND4_LOOKBACK + 16:
            continue
        dates = [b[0] for b in bars]
        if dates[-1] > asof_date:
            continue  # asof not reached yet for this symbol
        # find the last completed bar at/prior to asof
        idx = None
        for i, d in enumerate(dates):
            if d <= asof_date:
                idx = i
            else:
                break
        if idx is None or idx < CHAND4_LOOKBACK:
            continue
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        closes = [b[4] for b in bars]
        opens = [b[1] for b in bars]
        # 40d breakout on the completed bar
        hi = highs[idx]
        lo = lows[idx]
        new_high = hi > max(highs[idx - CHAND4_LOOKBACK:idx])
        new_low = lo < min(lows[idx - CHAND4_LOOKBACK:idx])
        # strength: (close - SMA40) / ATR14
        sma = sum(closes[idx - CHAND4_LOOKBACK:idx]) / CHAND4_LOOKBACK
        # ATR14
        trs = []
        for j in range(idx - 13, idx + 1):
            if j == 0:
                continue
            trs.append(max(highs[j] - lows[j],
                           abs(highs[j] - closes[j - 1]),
                           abs(lows[j] - closes[j - 1])))
        atr = sum(trs) / 14 if len(trs) == 14 else None
        if not atr or atr <= 0:
            continue
        strength = (closes[idx] - sma) / atr if sma > 0 else 0.0
        if new_high:
            candidates.append({
                "symbol": sym, "direction": "LONG",
                "strength": round(strength, 3),
                "signal_date": dates[idx],
                "signal_close": round(closes[idx], 6),
                "entry_guess": round(opens[idx + 1], 6) if idx + 1 < len(opens) else None,
                "atr14": round(atr, 6),
            })
        if new_low:
            candidates.append({
                "symbol": sym, "direction": "SHORT",
                "strength": round(abs(strength), 3),
                "signal_date": dates[idx],
                "signal_close": round(closes[idx], 6),
                "entry_guess": round(opens[idx + 1], 6) if idx + 1 < len(opens) else None,
                "atr14": round(atr, 6),
            })
    # rank by strength, top-N per side
    longs = sorted([c for c in candidates if c["direction"] == "LONG"],
                   key=lambda c: -c["strength"])[:CHAND4_TOP_N]
    shorts = sorted([c for c in candidates if c["direction"] == "SHORT"],
                    key=lambda c: -c["strength"])[:CHAND4_TOP_N]
    return {"candidates": longs + shorts, "longs": longs, "shorts": shorts,
            "asof": asof_date}


def xsmom_signal(xsmom_json=None):
    """XSMOM biweekly signal. Uses the checked-in signal JSON if given (Hermes
    has the MOMSXperp repo; also calls its live signal module directly).
    """
    # Prefer the live signal module (MOMSXperp) for current rebalance.
    try:
        from momsxperp import signal as xs
        sig = xs.generate_signal()
        rebalance = xs.next_rebalance_date()
        return {
            "longs": sig.get("longs", []),
            "shorts": sig.get("shorts", []),
            "asof": sig.get("asof_date"),
            "rebalance_date": sig.get("rebalance_date"),
            "is_rebalance": (sig.get("rebalance_date") == rebalance.isoformat()),
            "universe_size": sig.get("universe_size"),
        }
    except Exception as e:
        log(f"WARNING: XSMOM live signal failed ({e}); using JSON fallback")
        if xsmom_json and os.path.exists(xsmom_json):
            with open(xsmom_json) as f:
                sig = json.load(f)
            return {
                "longs": sig.get("longs", []),
                "shorts": sig.get("shorts", []),
                "asof": sig.get("asof_date"),
                "rebalance_date": sig.get("rebalance_date"),
                "is_rebalance": False,
                "universe_size": sig.get("universe_size"),
            }
        return {"longs": [], "shorts": [], "asof": None,
                "rebalance_date": None, "is_rebalance": False,
                "universe_size": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="asof date YYYY-MM-DD (default: last completed daily bar)")
    ap.add_argument("--out", default=None, help="output dir (default THESES_DIR)")
    ap.add_argument("--xsmom-json", default=None,
                    help="path to a checked-in xsmom_signal_<date>.json fallback")
    args = ap.parse_args()

    asof_date = args.date
    out_dir = args.out or THESES_DIR
    os.makedirs(out_dir, exist_ok=True)

    data = load_bybit_daily()
    chand4 = chand4_signal(data, asof_date)
    xsmom = xsmom_signal(args.xsmom_json)

    book = {
        "date": asof_date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chand4": chand4,
        "xsmom": xsmom,
        "meta": {
            "universe": len(data),
            "note": "signal-only; sizing/execution on VPS via combined_exec",
            "chand4_config": {
                "lookback": CHAND4_LOOKBACK, "trail": CHAND4_TRAIL,
                "chandelier_mult": CHAND4_CHANDELIER, "top_n": CHAND4_TOP_N,
            },
        },
    }

    out_path = os.path.join(out_dir, f"combined_signals_{chand4['asof']}.json")
    with open(out_path, "w") as f:
        json.dump(book, f, indent=2)
    log(f"wrote {out_path}")
    log(f"  chand4: {len(chand4['candidates'])} candidates "
        f"({len(chand4['longs'])} L / {len(chand4['shorts'])} S)")
    log(f"  xsmom: rebalance={xsmom['rebalance_date']} "
        f"is_rebalance={xsmom['is_rebalance']} longs={len(xsmom['longs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
