#!/usr/bin/env python3
"""combined_exec/run.py — manage the combined book (paper or live).

Safe to run as often as you like (the VPS cron runs it hourly):

  1. lock           one run at a time (flock on the state dir)
  2. reconcile      positions that vanished on the exchange (stop hit,
                    liquidation, manual close) are recorded and dropped from
                    state; exchange positions we did not open are UNMANAGED
                    (never touched, but they count against caps/conflicts)
  3. trail          CHAND4 chandelier stops ratchet from completed daily bars
                    and every managed stop is re-asserted on the exchange
  4. trade          ONCE per signal: load combined_signals_<yesterday>.json,
                    plan with planner.plan_day() (the same code the backtest
                    runs), close rebalance exits, open entries at market with
                    the stop attached and leverage set from the stop
  5. record         state is saved after every action

Entries only ever use the signal for yesterday's completed UTC bar; a stale
or missing signal means manage-only (no new risk).

Usage:
  python3 -m combined_exec.run [--signals-dir DIR] [--dry-plan]
    --dry-plan   print the plan and stop (no orders, no paper fills)
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

from combined_exec.broker import BybitBroker, PaperBroker, round_qty, round_stop  # noqa: E402
from combined_exec.config import combined_config  # noqa: E402
from combined_exec.market import Market  # noqa: E402
from combined_exec.planner import plan_day, trail_updates  # noqa: E402
from combined_exec.state import State  # noqa: E402
from combined_exec.strategy import day_str, utc_today  # noqa: E402

LOG = os.environ.get("COMBINED_LOG", "")


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    if LOG:
        try:
            with open(LOG, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def acquire_lock(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    fh = open(os.path.join(state_dir, ".lock"), "w")
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass                       # Windows dev box: no flock
    except OSError:
        return None
    return fh


def link_id(leg, side, symbol, asof, action="O"):
    tag = ("C4" if leg == "CHAND4" else "XS") + side[0] + action
    return f"{tag}-{symbol}-{asof.replace('-', '')[2:]}"[:36]


def load_signal(signals_dir, asof):
    path = os.path.join(signals_dir, f"combined_signals_{asof}.json")
    if not os.path.exists(path):
        return None, path
    with open(path) as f:
        sig = json.load(f)
    if sig.get("schema") != 2 or sig.get("asof") != asof:
        raise RuntimeError(f"{path}: not a schema-2 signal for {asof} "
                           f"(schema={sig.get('schema')}, asof={sig.get('asof')})")
    return sig, path


def reconcile(state, broker, today):
    """Drop state positions that no longer exist on the exchange; return the
    set of unmanaged exchange symbols."""
    for sym, info in broker.settle(today):
        log(f"[STOP] {sym} exit {info['price']} pnl {info['pnl']:+.2f}")
    live = broker.positions()
    for sym in list(state.positions):
        pos = state.positions[sym]
        ex = live.get(sym)
        if ex and ex["side"] == pos["side"]:
            if abs(ex["size"] - pos["qty"]) > 1e-12:
                log(f"[RECON] {sym} size {pos['qty']} -> {ex['size']} (exchange)")
                pos["qty"] = ex["size"]
            continue
        info = broker.exit_info(sym) or {"price": pos["stop"], "date": day_str(today), "pnl": None}
        state.remove(sym, info["price"], "CLOSED-ON-EXCHANGE", info["date"], info["pnl"])
        log(f"[RECON] {pos['leg']} {pos['side']} {sym} closed on exchange at "
            f"{info['price']} (pnl {info['pnl']})")
    unmanaged = {s for s in live if s not in state.positions}
    for s in sorted(unmanaged):
        log(f"[UNMANAGED] {s} {live[s]['side']} size {live[s]['size']} - not ours, left alone")
    state.save()
    return unmanaged


def trail_and_assert(state, broker, market, asof, p, dry):
    held = state.positions
    if not held:
        return
    series = {}
    for sym, pos in held.items():
        if pos["leg"] == "CHAND4":
            try:
                series[sym] = market.series(sym, limit=300)
            except Exception as e:
                log(f"[TRAIL] {sym}: klines failed ({e}); keeping stop {pos['stop']}")
    updates = trail_updates(held, series, asof, p)
    for sym, pos in held.items():
        new = updates.get(sym, pos["stop"])
        new = round_stop(new, broker.instrument(sym)["tickSize"], pos["side"])
        tighter = new > pos["stop"] if pos["side"] == "LONG" else new < pos["stop"]
        if dry:
            if tighter:
                log(f"[PLAN] trail {sym} {pos['stop']} -> {new}")
            continue
        try:
            broker.set_stop(sym, pos["side"], new if tighter else pos["stop"])
            if tighter:
                log(f"[TRAIL] {sym} {pos['side']} stop {pos['stop']} -> {new}")
                state.set_stop(sym, new)
        except Exception as e:
            log(f"[TRAIL-FAIL] {sym}: {e}")


def execute_plan(plan, state, broker, signal, p, dry, today):
    asof = signal["asof"]
    for sym, reason in plan["exits"]:
        pos = state.positions[sym]
        if dry:
            log(f"[PLAN] close {pos['leg']} {sym} ({reason})")
            continue
        info = broker.close(sym, pos["side"], pos["qty"],
                            link_id(pos["leg"], pos["side"], sym, asof, "X"), today)
        state.remove(sym, info["price"], reason, info["date"], info["pnl"])
        log(f"[CLOSE] {pos['leg']} {sym} {reason} at {info['price']}")

    for e in plan["entries"]:
        sym, side = e["symbol"], e["side"]
        inst = broker.instrument(sym)
        if inst.get("status") not in (None, "Trading"):
            log(f"[SKIP] {sym}: instrument status {inst.get('status')}")
            continue
        qty = round_qty(e["qty"], inst["qtyStep"])
        if inst.get("maxQty") and qty > inst["maxQty"]:
            qty = round_qty(inst["maxQty"], inst["qtyStep"])
        if qty < inst["minQty"] or qty * e["price"] < inst.get("minNotional", 5.0):
            log(f"[SKIP] {sym}: qty {qty} below exchange minimum")
            continue
        stop = round_stop(e["stop"], inst["tickSize"], side)
        lev = e["leverage"]
        desc = (f"{e['leg']} {side} {sym} qty={qty} ~{e['price']:.6g} stop={stop} "
                f"lev={lev} risk=${e['risk']:.2f} margin=${e['margin']:.2f}")
        if dry:
            log(f"[PLAN] open {desc}")
            continue
        try:
            broker.set_leverage(sym, lev)
            fill = broker.open(sym, side, qty, stop, link_id(e["leg"], side, sym, asof), today)
        except Exception as ex:
            log(f"[OPEN-FAIL] {desc}: {ex}")
            continue
        if not fill:
            log(f"[OPEN-FAIL] {desc}: no fill confirmed")
            continue
        if e["leg"] == "XSMOM":         # hard stop is relative to the actual fill
            stop = round_stop(fill["avg_price"] * (1 - p["xsmom_stop_pct"]), inst["tickSize"], side)
            broker.set_stop(sym, side, stop)
        state.add(sym, {"leg": e["leg"], "side": side, "qty": fill["qty"],
                        "entry_price": fill["avg_price"], "entry_date": day_str(today),
                        "stop": stop, "initial_stop": stop, "leverage": lev,
                        "margin": fill["qty"] * fill["avg_price"] / lev,
                        "risk": abs(fill["avg_price"] - stop) * fill["qty"],
                        "signal_asof": asof})
        log(f"[OPEN] {desc} filled {fill['qty']} @ {fill['avg_price']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-dir", default=None)
    ap.add_argument("--dry-plan", action="store_true",
                    help="print the plan only: no orders, no paper fills, no state changes")
    args = ap.parse_args()

    cfg = combined_config()
    p = cfg["params"]
    signals_dir = args.signals_dir or cfg["signals_dir"]
    lock = acquire_lock(cfg["state_dir"])
    if lock is None:
        log("another run holds the lock; exiting")
        return 0

    market = Market(cfg["data_url"])
    if cfg["auto_trade"]:
        broker = BybitBroker(cfg, log)
        mode = "LIVE " + ("MAINNET" if cfg["is_mainnet"] else "TESTNET")
    else:
        broker = PaperBroker(cfg, market, log)
        mode = "PAPER (mainnet prices, no orders)"
    state = State.load(os.path.join(cfg["state_dir"], f"state_{broker.mode}.json"))
    today = utc_today()
    asof = (today - timedelta(days=1)).isoformat()
    log(f"=== combined_exec {mode} | today {today} | signal asof {asof} | "
        f"xsmom {'ON' if p['xsmom_enabled'] else 'OFF'} | risk {p['risk_pct']:.2%}")

    if not args.dry_plan:
        n = broker.cancel_legacy_orders()
        if n:
            log(f"cancelled {n} legacy v1 resting orders")
    unmanaged = reconcile(state, broker, today) if not args.dry_plan else set()
    trail_and_assert(state, broker, market, asof, p, args.dry_plan)

    signal, path = load_signal(signals_dir, asof)
    if signal is None:
        log(f"no signal at {path} - manage-only (no new entries)")
    elif state.data.get("last_signal_asof") == asof and not args.dry_plan:
        log(f"signal {asof} already executed - manage-only")
    else:
        held = state.positions
        want = {c["symbol"] for c in signal["chand4"]["longs"] + signal["chand4"]["shorts"]
                + signal["xsmom"]["longs"]}
        marks = broker.marks(want | set(held))
        equity = broker.equity()
        max_lev = {}
        for s in want:
            try:
                max_lev[s] = broker.instrument(s).get("maxLeverage")
            except Exception:
                pass
        plan = plan_day(signal, held, equity, marks, p, unmanaged, max_lev)
        log(f"equity {equity:.2f} | {len(held)} held | plan: {len(plan['exits'])} exits, "
            f"{len(plan['entries'])} entries, {len(plan['skipped'])} skipped | "
            f"xsmom rebalance {signal['xsmom']['is_rebalance']}")
        for s in plan["skipped"]:
            log(f"[SKIP] {s['leg']} {s['side']} {s['symbol']}: {s['reason']}")
        execute_plan(plan, state, broker, signal, p, args.dry_plan, today)
        if not args.dry_plan:
            state.data["last_signal_asof"] = asof
            state.note(f"executed signal {asof}: {len(plan['entries'])} entries, "
                       f"{len(plan['exits'])} exits")
    if not args.dry_plan:
        state.data["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state.save()
    log(f"done | equity {broker.equity():.2f} | positions "
        + (", ".join(f"{s}:{v['leg']}/{v['side']}" for s, v in state.positions.items()) or "none"))
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
