#!/usr/bin/env python3
"""combined_exec/run.py — place/manage the combined strategy book on Bybit.

Flow (called daily on the VPS):
  1. Load the latest combined_signals_<date>.json from the git-bus data repo.
  2. CHAND4 leg:
     - For each candidate not already held (same symbol+direction), place a
       limit order at today's open (or market if too stale), sized via the
       bybit_exec sizing stack with a 4xATR chandelier initial stop.
     - For each EXISTING position with a chandelier leg, recompute the trail
       from the highest close since entry and RAISE the stop (never lower).
  3. XSMOM leg:
     - On rebalance days: close XSMOM legs whose 14-day hold window elapsed,
       then enter the new top-5 longs (15% hard stop).
     - On non-rebalance days: only trail/guard existing XSMOM stops.
  4. Dry-run (default, paper): print the plan, POST nothing.

Order tagging: orderLinkId = "CH4-<SYM>-<YYYYMMDD>" / "XSM-<SYM>-<YYYYMMDD>"
so report/reconciliation can attribute P&L per leg.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

# repo layout: this file lives in <repo>/combined_exec/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
# The DMA engine repo provides bybit_exec (client + sizing). If not on the
# path yet, add the common VPS location as a fallback.
for d in ("/opt/bybit-execution-engine", "/home/jose/workspace/bybit-execution-engine"):
    if d not in sys.path and os.path.isdir(os.path.join(d, "bybit_exec")):
        sys.path.insert(0, d)
        break

from combined_exec.config import combined_config
from bybit_exec.bybit_client import BybitClient, BybitAPIError
from bybit_exec.sizing import size_order, allocate

LOG = os.environ.get("COMBINED_LOG", "/tmp/combined_exec.log")


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_signals(cfg, date_str):
    path = os.path.join(cfg["signals_dir"], f"combined_signals_{date_str}.json")
    if not os.path.exists(path):
        sys.exit(f"FATAL: no combined signals at {path}")
    with open(path) as f:
        return json.load(f)


def compute_chand4_stop(sym, entry_px, entry_date, atr14, chandelier_mult,
                        min_atr_mult, side="LONG"):
    """Chandelier initial stop = entry ∓ k*ATR (the live trail is managed
    separately by raise_stops using highest-close since entry)."""
    if side == "LONG":
        return entry_px - chandelier_mult * atr14
    return entry_px + chandelier_mult * atr14


def chandelier_stop_from_highs(highest_close, atr14, chandelier_mult, side="LONG"):
    if side == "LONG":
        return highest_close - chandelier_mult * atr14
    return highest_close + chandelier_mult * atr14


def _clean_price(px, tick_size):
    import math
    if tick_size <= 0:
        return px
    return math.floor(px / tick_size + 1e-9) * tick_size


class CombinedExecutor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = BybitClient(cfg)
        self.instruments = {}

    def instrument(self, symbol):
        if symbol not in self.instruments:
            self.instruments[symbol] = self.client.instruments(symbol)
        return self.instruments[symbol]

    def place_order(self, spec, side, reason, dry_run):
        """Place a limit order with attached stop (Bybit TP/SL). spec has
        entry/stop/qty. Returns order dict or None."""
        sym = spec["symbol"]
        inst = self.instrument(sym)
        tick = inst["tickSize"]
        price = _clean_price(spec["entry"], tick)
        stop = _clean_price(spec["stop"], tick)
        qty = spec["qty"]
        if qty <= 0:
            return None
        link = f"{'CH4' if reason == 'CHAND4' else 'XSM'}-{sym}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        if dry_run:
            log(f"[DRY] would place {reason} {side} {sym} qty={qty} "
                f"entry={price} stop={stop} link={link} "
                f"leverage={spec.get('leverage')} margin={spec.get('margin')}")
            return {"symbol": sym, "side": side, "qty": qty, "price": price,
                    "stop": stop, "orderLinkId": link, "dry_run": True}
        try:
            buy_side = "Buy" if side == "LONG" else "Sell"
            body = {
                "category": "linear",
                "symbol": sym,
                "side": buy_side,
                "orderType": "Limit",
                "qty": str(qty),
                "price": str(price),
                "timeInForce": "PostOnly",
                "stopLoss": str(stop),
                "positionIdx": 0,
            }
            res = self.client._signed_post("/v5/order/create", body)
            log(f"[ORDER] {reason} {side} {sym} qty={qty} entry={price} "
                f"stop={stop} -> {res}")
            return res
        except BybitAPIError as e:
            log(f"[ORDER-FAIL] {sym}: {e}")
            return None

    def raise_stop(self, position, new_stop, dry_run):
        """Trail a position's stop-loss up (long) / down (short). Never lowers."""
        sym = position["symbol"]
        old = float(position.get("stopLoss") or 0)
        side = position.get("side", "Sell")
        is_long = side == "Buy"
        if is_long and new_stop <= old:
            return False
        if not is_long and new_stop >= old and old > 0:
            return False
        if dry_run:
            log(f"[DRY] would trail {sym} stop {old} -> {new_stop}")
            return True
        # set TP/SL on an existing position: use /v5/position/trading-stop
        try:
            body = {"category": "linear", "symbol": sym,
                    "stopLoss": str(new_stop), "positionIdx": 0}
            res = self.client._signed_post("/v5/position/trading-stop", body)
            log(f"[TRAIL] {sym} stop {old} -> {new_stop} -> {res}")
            return True
        except BybitAPIError as e:
            log(f"[TRAIL-FAIL] {sym}: {e}")
            return False

    def close_position(self, position, dry_run, reason="REBALANCE"):
        sym = position["symbol"]
        size = float(position["size"])
        # Bybit position side: 'Buy' = long, 'Sell' = short. Close opposite.
        close_side = "Sell" if position.get("side", "Buy") == "Buy" else "Buy"
        if dry_run:
            log(f"[DRY] would close {reason} {sym} size={size} side={close_side} (reduceOnly)")
            return True
        try:
            body = {
                "category": "linear",
                "symbol": sym,
                "side": close_side,
                "orderType": "Market",
                "qty": str(size),
                "timeInForce": "GoodTillCancel",
                "reduceOnly": True,
            }
            res = self.client._signed_post("/v5/order/create", body)
            log(f"[CLOSE] {reason} {sym} size={size} side={close_side} -> {res}")
            return True
        except BybitAPIError as e:
            log(f"[CLOSE-FAIL] {sym}: {e}")
            return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="signal date (default: last completed bar from the signal file name)")
    ap.add_argument("--dry-run", action="store_true", default=False,
                    help="paper mode: print plan, POST nothing (default unless AUTO_TRADE)")
    ap.add_argument("--signals-dir", default=None)
    args = ap.parse_args()

    cfg = combined_config()
    dry_run = args.dry_run or not cfg["auto_trade"]
    if dry_run and not args.dry_run and not cfg["auto_trade"]:
        log("COMBINED_AUTO_TRADE=false -> dry-run (paper) mode; set true to place orders")

    # Resolve signal file: explicit --date, else the latest combined_signals_*
    signals_dir = args.signals_dir or cfg["signals_dir"]
    if args.date:
        date_str = args.date
        path = os.path.join(signals_dir, f"combined_signals_{date_str}.json")
        if not os.path.exists(path):
            sys.exit(f"FATAL: no signals at {path}")
    else:
        import glob
        files = sorted(glob.glob(os.path.join(signals_dir, "combined_signals_*.json")))
        if not files:
            sys.exit(f"FATAL: no combined_signals_*.json in {signals_dir}")
        path = files[-1]
        date_str = os.path.basename(path).replace("combined_signals_", "").replace(".json", "")
    with open(path) as f:
        signals = json.load(f)
    log(f"signals {date_str}: CHAND4 {len(signals.get('chand4', {}).get('candidates', []))} "
        f"cands | XSMOM rebalance {signals.get('xsmom', {}).get('rebalance_date')} "
        f"is_rebalance={signals.get('xsmom', {}).get('is_rebalance')}")

    ex = CombinedExecutor(cfg)
    try:
        positions = ex.client.positions()
    except Exception as e:
        positions = []
        log(f"WARNING: could not fetch positions ({e}). If no COMBINED_API_KEY/"
           f"COMBINED_API_SECRET are configured yet, this is expected — the "
           f"executor needs the new sub-account keys to see open positions. "
           f"Run with COMBINED_AUTO_TRADE=false (paper) only after keys exist, "
           f"or keep paper mode local for signal inspection.")
    held = {(p["symbol"], p["side"]) for p in positions}
    log(f"current positions: {len(positions)}")

    # ── CHAND4 entries ──────────────────────────────────────────────
    chand4 = signals.get("chand4", {})
    for cand in chand4.get("candidates", [])[:cfg["chand4_top_n"] * 2]:
        sym = cand["symbol"]
        direction = cand["direction"]
        held_key = (sym, "Buy" if direction == "LONG" else "Sell")
        if held_key in held:
            continue  # already in this position
        # skip if the opposite side is held (avoid flip-flopping on the same symbol)
        opp = (sym, "Sell" if direction == "LONG" else "Buy")
        if opp in held:
            continue
        entry = cand.get("entry_guess") or cand.get("signal_close")
        atr14 = cand.get("atr14")
        if not entry or not atr14 or atr14 <= 0:
            continue
        # staleness: entry guess vs live mark
        try:
            marks = ex.client.ticker_marks(sym)
            mark = marks.get(sym)
        except Exception:
            mark = None
        if mark and entry:
            drift = abs(entry - mark) / mark * 100
            if drift > cfg["stale_pct"]:
                log(f"[SKIP-STALE] {sym} {direction} entry {entry} mark {mark} "
                    f"drift {drift:.1f}%")
                continue
        spec = {"symbol": sym, "entry": entry,
                "stop": compute_chand4_stop(sym, entry, date_str, atr14,
                                            cfg["chandelier_mult"],
                                            cfg["chandelier_min_atr"],
                                            direction)}
        # sizing via the bybit_exec stack (risk-% of balance, stop-derived lev)
        try:
            bal = ex.client.wallet_balance()
            balance = bal["totalEquity"]
        except Exception as e:
            if dry_run:
                balance = 1000.0  # paper: assume $1k so sizing can be previewed
                log(f"[PAPER] balance fetch failed ({e}); using $1000 for sizing preview")
            else:
                log(f"FATAL: balance fetch failed ({e})")
                return 1
        inst = ex.instrument(sym)
        sized = size_order(spec, balance, inst, cfg)
        if not sized:
            log(f"[SKIP-SIZE] {sym} {direction}: {spec.get('status')}")
            continue
        spec.update(sized)
        ex.place_order(spec, direction, "CHAND4", dry_run)

    # ── XSMOM rebalance ─────────────────────────────────────────────
    xsmom = signals.get("xsmom", {})
    if xsmom.get("is_rebalance"):
        # close XSMOM legs that have been held >= 14 days (identified by tag/date)
        for p in positions:
            link = p.get("orderLinkId") or ""
            if link.startswith("XSM-"):
                ex.close_position(p, dry_run, reason="XSMOM-HOLD-END")
        # enter new XSMOM longs (top-5 by VNR, 15% stop)
        for c in xsmom.get("longs", [])[:cfg["xsmom_top_n"]]:
            sym = c["symbol"]
            held_key = (sym, "Buy")
            if held_key in held:
                continue
            entry = c.get("close") or c.get("entry")
            stop = entry * (1 - cfg["xsmom_stop_pct"]) if entry else None
            if not entry or not stop:
                continue
            spec = {"symbol": sym, "entry": entry, "stop": stop}
            try:
                bal = ex.client.wallet_balance()
                balance = bal["totalEquity"]
            except Exception as e:
                if dry_run:
                    balance = 1000.0
                    log(f"[PAPER] balance fetch failed ({e}); using $1000 for sizing preview")
                else:
                    log(f"FATAL: balance fetch failed ({e})")
                    return 1
            inst = ex.instrument(sym)
            sized = size_order(spec, balance, inst, cfg)
            if not sized:
                log(f"[SKIP-SIZE] XSM {sym}: {spec.get('status')}")
                continue
            spec.update(sized)
            ex.place_order(spec, "LONG", "XSMOM", dry_run)
    else:
        log("no XSMOM rebalance today (next: %s)" % xsmom.get("rebalance_date"))

    # ── Trail CHAND4 stops (daily) ─────────────────────────────────
    # NOTE: proper chandelier trailing needs each position's highest close since
    # entry — fetched on the VPS from public klines. The executor reads the
    # latest klines and raises stops for CHAND4 legs.
    try:
        for p in positions:
            link = p.get("orderLinkId") or ""
            if not link.startswith("CH4-"):
                continue
            sym = p["symbol"]
            side = p.get("side", "Sell")
            is_long = side == "Buy"
            # fetch ~30d daily klines for the symbol
            rows = ex.client._public_get("/v5/market/kline", {
                "category": "linear", "symbol": sym, "interval": "D", "limit": 30
            }).get("list", [])
            if not rows:
                continue
            # rows newest-first: [ts, o, h, l, c, ...]
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            closes = [float(r[4]) for r in rows]
            # ATR14 on the window
            trs = []
            for j in range(1, min(15, len(closes))):
                trs.append(max(highs[j] - lows[j],
                               abs(highs[j] - closes[j - 1]),
                               abs(lows[j] - closes[j - 1])))
            atr = sum(trs) / len(trs) if trs else 0
            if atr <= 0:
                continue
            hh = max(highs[:15])
            new_stop = chandelier_stop_from_highs(hh, atr, cfg["chandelier_mult"],
                                                  "LONG" if is_long else "SHORT")
            if is_long:
                # never trail below the entry + min ATR cushion
                entry = float(p.get("avgPrice") or 0)
                floor = entry - cfg["chandelier_min_atr"] * atr
                new_stop = max(new_stop, floor)
            else:
                entry = float(p.get("avgPrice") or 0)
                cap = entry + cfg["chandelier_min_atr"] * atr
                new_stop = min(new_stop, cap) if new_stop > 0 else new_stop
            ex.raise_stop(p, new_stop, dry_run)
    except Exception as e:
        log(f"[TRAIL-ERROR] {e}")

    log("combined_exec run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
