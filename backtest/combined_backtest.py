#!/usr/bin/env python3
"""backtest/combined_backtest.py — joint, shared-capital backtest of the
COMBINED book (CHAND4 40/15 + XSMOM) exactly as the executor trades it.

Unlike 20d/run_combined_backtest.py (which added two separately-sized P&L
series on XSMOM's gappy period grid), this is ONE account simulated day by
day with the SAME code the live system runs:

  signal     strategy.build_signal()   (also used by combined_signal.py)
  decisions  planner.plan_day() / planner.trail_updates()  (also used by run.py)

Daily loop for trading day D (signal asof = D-1, completed bars only):
  1. positions whose symbol stopped trading (delisted) exit at last close
  2. CHAND4 chandelier stops ratchet from bars <= D-1
  3. build the signal on D-1; plan exits/entries against equity at D's open
  4. exits + entries fill at D's open +/- slippage, taker fee both sides
  5. stops are checked against D's bar INCLUDING the entry day
     (gap-through fills at the open, not at the stop)
  6. funding charged on positions held at the close (actual Bybit 8h rates,
     summed per day; a default rate is assumed where history is missing)
  7. mark to market at D's close

Data: MOMSXperp's data_cache (klines incl. turnover for every Bybit USDT perp
ever listed — delisted ones included — plus funding history). The universe
is point-in-time (trailing turnover), so there is no survivorship bias.

Usage:
  python3 backtest/combined_backtest.py [--data DIR] [--start YYYY-MM-DD]
                                         [--end YYYY-MM-DD] [--capital 10000]
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from combined_exec.strategy import DEFAULTS, Series, build_signal, universe  # noqa: E402
from combined_exec.planner import plan_day, trail_updates  # noqa: E402

FEE = 0.00055          # Bybit taker, per side
SLIP = 0.0005          # slippage per side (5 bps)
DEFAULT_FUNDING_8H = 0.0001   # assumed where no funding history exists
DEFAULT_DATA = os.path.join(os.path.dirname(REPO), "MOMSXperp", "data_cache")


# ── data ────────────────────────────────────────────────────────────────────
def load_data(data_dir, cutoff=None):
    """Series for every symbol + {symbol: {date: daily funding rate}}."""
    manifest = os.path.join(data_dir, "manifest.json")
    if cutoff is None and os.path.exists(manifest):
        with open(manifest) as f:
            fetched = json.load(f).get("klines_fetched_at")
        if fetched:
            cutoff = fetched[:10]          # bars on the fetch day may be forming
    series, funding = {}, {}
    kdir = os.path.join(data_dir, "klines")
    for name in sorted(os.listdir(kdir)):
        if not name.endswith("USDT.json"):
            continue
        sym = name[:-5]
        with open(os.path.join(kdir, name)) as f:
            rows = json.load(f)
        s = Series.from_bybit(sym, rows, before=cutoff)
        if len(s) >= 30:
            series[sym] = s
    fdir = os.path.join(data_dir, "funding")
    if os.path.isdir(fdir):
        for name in os.listdir(fdir):
            sym = name[:-5]
            if sym not in series:
                continue
            with open(os.path.join(fdir, name)) as f:
                rows = json.load(f)
            daily = {}
            for ts, rate in rows:
                d = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date().isoformat()
                daily[d] = daily.get(d, 0.0) + float(rate)
            funding[sym] = daily
    return series, funding


def precompute_universes(series, start, end, p):
    out = {}
    d = start - timedelta(days=1)
    while d < end:
        out[d.isoformat()] = universe(series, d.isoformat(), p)
        d += timedelta(days=1)
    return out


def precompute_signals(series, universes, p, fixed_universe=None):
    """Signal books for every asof; independent of holdings. fixed_universe
    (diagnostic only) replaces the point-in-time universe with one static
    list — i.e. the survivorship/lookahead the MOMSXperp research had."""
    out = {}
    for a, syms in universes.items():
        if fixed_universe is not None:
            syms = [s for s in fixed_universe if a in series[s].idx]
        out[a] = build_signal(series, a, p, universe_syms=syms)
    return out


# ── simulation ──────────────────────────────────────────────────────────────
def simulate(series, funding, signals, start, end, p, capital=10000.0,
             legs=("CHAND4", "XSMOM"), with_funding=True):
    cash = capital
    holdings = {}           # sym -> dict(leg, side, qty, entry_price, entry_date, stop, margin, fees, funding)
    trades, curve = [], []
    costs = {"fees": 0.0, "slippage": 0.0, "funding": 0.0}

    def sign(side):
        return 1.0 if side == "LONG" else -1.0

    def close_pos(sym, px_raw, day, reason):
        nonlocal cash
        h = holdings.pop(sym)
        slip = SLIP if h["side"] == "LONG" else -SLIP
        px = px_raw * (1 - slip)              # sell lower / buy back higher
        fee = h["qty"] * px * FEE
        gross = h["qty"] * (px - h["entry_price"]) * sign(h["side"])
        cash += gross - fee
        costs["fees"] += fee
        costs["slippage"] += abs(px - px_raw) * h["qty"]
        pnl = gross - fee - h["fees"] - h["funding"]
        trades.append({
            "symbol": sym, "leg": h["leg"], "side": h["side"],
            "entry_date": h["entry_date"], "exit_date": day, "reason": reason,
            "entry": h["entry_price"], "exit": px, "qty": h["qty"],
            "pnl": pnl, "risk": h["risk"], "r": pnl / h["risk"] if h["risk"] else 0.0,
            "hold_days": (date.fromisoformat(day) - date.fromisoformat(h["entry_date"])).days,
        })

    def equity_at(day, field):
        eq = cash
        for sym, h in holdings.items():
            s = series[sym]
            i = s.idx.get(day)
            px = getattr(s, field)[i] if i is not None else s.close[s.idx[h["last_seen"]]]
            eq += h["qty"] * (px - h["entry_price"]) * sign(h["side"])
        return eq

    d = start
    while d < end:
        day = d.isoformat()
        asof = (d - timedelta(days=1)).isoformat()

        # 1. delisted / missing bar today
        for sym in list(holdings):
            s = series[sym]
            if day not in s.idx and day > s.dates[-1]:
                close_pos(sym, s.close[-1], day, "DELISTED")

        # 2. trail CHAND4 stops from completed bars
        for sym, stop in trail_updates(holdings, series, asof, p).items():
            holdings[sym]["stop"] = stop

        # 3. plan against equity at today's open
        sig = signals[asof]
        if "CHAND4" not in legs:
            sig = dict(sig, chand4={"longs": [], "shorts": []})
        if "XSMOM" not in legs:
            sig = dict(sig, xsmom={"is_rebalance": False, "longs": []})
        eq_open = equity_at(day, "open")
        if eq_open <= 0:
            break
        want = {c["symbol"] for c in sig["chand4"]["longs"] + sig["chand4"]["shorts"]
                + sig["xsmom"]["longs"]}
        prices = {s: series[s].open[series[s].idx[day]] for s in want if day in series[s].idx}
        plan = plan_day(sig, holdings, eq_open, prices, p)

        # 4. fills at the open
        for sym, reason in plan["exits"]:
            s = series[sym]
            if day in s.idx:
                close_pos(sym, s.open[s.idx[day]], day, reason)
        for e in plan["entries"]:
            slip = SLIP if e["side"] == "LONG" else -SLIP
            fill = e["price"] * (1 + slip)
            fee = e["qty"] * fill * FEE
            cash -= fee
            costs["fees"] += fee
            costs["slippage"] += abs(fill - e["price"]) * e["qty"]
            holdings[e["symbol"]] = {
                "leg": e["leg"], "side": e["side"], "qty": e["qty"],
                "entry_price": fill, "entry_date": day, "stop": e["stop"],
                "margin": e["margin"], "risk": e["risk"], "fees": fee,
                "funding": 0.0, "last_seen": day,
            }

        # 5. stops on today's bar (entry day included)
        for sym in list(holdings):
            h = holdings[sym]
            s = series[sym]
            i = s.idx.get(day)
            if i is None:
                continue
            h["last_seen"] = day
            if h["side"] == "LONG" and s.low[i] <= h["stop"]:
                close_pos(sym, min(h["stop"], s.open[i]), day, "STOP")
            elif h["side"] == "SHORT" and s.high[i] >= h["stop"]:
                close_pos(sym, max(h["stop"], s.open[i]), day, "STOP")

        # 6. funding on positions held through the day
        for sym, h in (holdings.items() if with_funding else ()):
            s = series[sym]
            i = s.idx.get(day)
            if i is None:
                continue
            rate = funding.get(sym, {}).get(day)
            if rate is None:
                rate = 3 * DEFAULT_FUNDING_8H
            f = h["qty"] * s.close[i] * rate * sign(h["side"])
            cash -= f
            h["funding"] += f
            costs["funding"] += f

        # 7. mark to market
        eq = equity_at(day, "close")
        gross = sum(h["qty"] * series[s].close[series[s].idx[day]]
                    for s, h in holdings.items() if day in series[s].idx)
        curve.append({"date": day, "equity": eq, "gross": gross, "n": len(holdings)})
        d += timedelta(days=1)

    # close anything still open at the last mark (for trade stats only)
    last = curve[-1]["date"] if curve else start.isoformat()
    for sym in list(holdings):
        s = series[sym]
        i = s.idx.get(last, len(s) - 1)
        close_pos(sym, s.close[i], last, "END")
    return {"curve": curve, "trades": trades, "costs": costs, "capital": capital}


# ── statistics ──────────────────────────────────────────────────────────────
def curve_stats(curve, capital):
    eq = [c["equity"] for c in curve]
    if len(eq) < 2:
        return {}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    years = len(eq) / 365.0
    final = eq[-1]
    cagr = (final / capital) ** (1 / years) - 1 if final > 0 else -1.0
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    dn = [min(r, 0) for r in rets]
    dsd = math.sqrt(sum(x * x for x in dn) / len(dn))
    peak, mdd, dd_start, worst = capital, 0.0, None, None
    for c in curve:
        if c["equity"] > peak:
            peak = c["equity"]
        dd = 1 - c["equity"] / peak
        if dd > mdd:
            mdd, worst = dd, c["date"]
    by_year = {}
    prev = capital
    for c in curve:
        y = c["date"][:4]
        by_year.setdefault(y, [prev, None])
        by_year[y][1] = c["equity"]
        prev = c["equity"]
    return {
        "start": curve[0]["date"], "end": curve[-1]["date"], "years": round(years, 2),
        "final_equity": round(final, 2), "total_return_pct": round((final / capital - 1) * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "sharpe": round(mean / sd * math.sqrt(365), 2) if sd else 0.0,
        "sortino": round(mean / dsd * math.sqrt(365), 2) if dsd else 0.0,
        "max_dd_pct": round(mdd * 100, 1), "max_dd_trough": worst,
        "calmar": round(cagr / mdd, 2) if mdd else 0.0,
        "avg_gross_exposure": round(sum(c["gross"] / c["equity"] for c in curve
                                        if c["equity"] > 0) / len(curve), 3),
        "avg_positions": round(sum(c["n"] for c in curve) / len(curve), 2),
        "by_year_pct": {y: round((b / a - 1) * 100, 1) for y, (a, b) in by_year.items()},
    }


def trade_stats(trades):
    out = {}
    groups = {}
    for t in trades:
        groups.setdefault(f"{t['leg']} {t['side']}", []).append(t)
        groups.setdefault(t["leg"], []).append(t)
    for k, ts in sorted(groups.items()):
        wins = [t["pnl"] for t in ts if t["pnl"] > 0]
        losses = [-t["pnl"] for t in ts if t["pnl"] <= 0]
        out[k] = {
            "trades": len(ts),
            "pnl": round(sum(t["pnl"] for t in ts), 2),
            "win_rate_pct": round(len(wins) / len(ts) * 100, 1),
            "profit_factor": round(sum(wins) / sum(losses), 2) if sum(losses) else None,
            "avg_r": round(sum(t["r"] for t in ts) / len(ts), 3),
            "avg_hold_days": round(sum(t["hold_days"] for t in ts) / len(ts), 1),
            "best_r": round(max(t["r"] for t in ts), 2),
            "stops_pct": round(sum(t["reason"] == "STOP" for t in ts) / len(ts) * 100, 1),
        }
    return out


def daily_returns(curve):
    return {curve[i]["date"]: curve[i]["equity"] / curve[i - 1]["equity"] - 1
            for i in range(1, len(curve))}


def correlation(a, b):
    keys = sorted(set(a) & set(b))
    x = [a[k] for k in keys]
    y = [b[k] for k in keys]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    cov = sum((i - mx) * (j - my) for i, j in zip(x, y))
    vx = math.sqrt(sum((i - mx) ** 2 for i in x))
    vy = math.sqrt(sum((j - my) ** 2 for j in y))
    return cov / (vx * vy) if vx and vy else 0.0


# ── main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("COMBINED_BT_DATA", DEFAULT_DATA))
    ap.add_argument("--start", default=None, help="default: first day the universe is full")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--out", default=os.path.join(REPO, "backtest", "results"))
    args = ap.parse_args()

    t0 = time.time()
    series, funding = load_data(args.data)
    last_day = max(s.dates[-1] for s in series.values())
    print(f"loaded {len(series)} symbols ({len(funding)} with funding), last bar {last_day}")
    p = dict(DEFAULTS, xsmom_enabled=True)   # the backtest selects legs explicitly

    first = min(s.dates[0] for s in series.values())
    probe_start = date.fromisoformat(first) + timedelta(days=p["min_history"])
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(last_day) + timedelta(days=1)
    universes = precompute_universes(series, probe_start, end, p)
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        full = [a for a, syms in sorted(universes.items()) if len(syms) >= p["universe_size"]]
        start = date.fromisoformat(full[0]) + timedelta(days=1)
    print(f"universes precomputed in {time.time() - t0:.0f}s; simulating {start} -> {end}")

    # the research's universe: today's top-50 applied to all history (diagnostic)
    today_top = universe(series, last_day, p)
    sig_cache = {}

    def signals_for(key, pv, fixed=None):
        if key not in sig_cache:
            sig_cache[key] = precompute_signals(series, universes, pv, fixed)
        return sig_cache[key]

    B2010 = {"chand4_lookback": 20, "chand4_stop_window": 10, "chand4_exit": "channel"}
    B4015 = {"chand4_exit": "channel"}
    # name, legs, param overrides, signal key, fixed universe, funding
    variants = [
        ("combined", ("CHAND4", "XSMOM"), {}, "base", None, True),
        ("chand4_only", ("CHAND4",), {}, "base", None, True),
        ("xsmom_only", ("XSMOM",), {}, "base", None, True),
        ("combined_long_only", ("CHAND4", "XSMOM"), {"chand4_shorts": False}, "base", None, True),
        ("combined_risk_1pct", ("CHAND4", "XSMOM"), {"risk_pct": 0.01}, "base", None, True),
        ("chand4_only_risk_1pct", ("CHAND4",), {"risk_pct": 0.01}, "base", None, True),
        ("chand4_only_no_funding", ("CHAND4",), {}, "base", None, False),
        ("xsmom_only_no_funding", ("XSMOM",), {}, "base", None, False),
        ("xsmom_only_equal_weight", ("XSMOM",), {"xsmom_weighting": "equal"}, "equal", None, True),
        ("xsmom_only_research_universe", ("XSMOM",), {}, "fixed", today_top, True),
        # plain channel breakouts (the 20d audit found these match or beat the chandelier)
        ("breakout_20_10_channel", ("CHAND4",), B2010, "b2010", None, True),
        ("breakout_20_10_channel_risk_1pct", ("CHAND4",), dict(B2010, risk_pct=0.01), "b2010", None, True),
        ("breakout_20_10_channel_long_only", ("CHAND4",), dict(B2010, chand4_shorts=False), "b2010", None, True),
        ("breakout_40_15_channel", ("CHAND4",), B4015, "base", None, True),
        ("chand4_only_long_only", ("CHAND4",), {"chand4_shorts": False}, "base", None, True),
    ]
    runs, meta = {}, {}
    for name, legs, over, key, fixed, fund in variants:
        pv = dict(p, **over)
        sigs = signals_for(key, pv, fixed)
        res = simulate(series, funding, sigs, start, end, pv, args.capital, legs, fund)
        runs[name] = res
        meta[name] = {"legs": list(legs), "overrides": over, "funding": fund,
                      "universe": "today's top-50 fixed (lookahead)" if fixed else "point-in-time"}
        s = curve_stats(res["curve"], args.capital)
        print(f"{name:<30} CAGR {s['cagr_pct']:>6.1f}%  Sharpe {s['sharpe']:>5.2f}  "
              f"maxDD {s['max_dd_pct']:>5.1f}%  trades {len(res['trades'])}")

    rho = correlation(daily_returns(runs["chand4_only"]["curve"]),
                      daily_returns(runs["xsmom_only"]["curve"]))
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {"dir": args.data, "symbols": len(series), "last_bar": last_day},
        "assumptions": {"fee_per_side": FEE, "slippage_per_side": SLIP,
                        "default_funding_8h": DEFAULT_FUNDING_8H,
                        "capital": args.capital, "params": p},
        "daily_return_correlation_chand4_vs_xsmom": round(rho, 3),
        "variants": {},
    }
    for name, res in runs.items():
        summary["variants"][name] = dict(meta[name], **{
            "stats": curve_stats(res["curve"], args.capital),
            "trades": trade_stats(res["trades"]),
            "costs": {k: round(v, 2) for k, v in res["costs"].items()},
        })
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "combined_backtest_results.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(args.out, "combined_equity_curve.json"), "w") as f:
        json.dump({k: [[c["date"], round(c["equity"], 2)] for c in v["curve"]]
                   for k, v in runs.items()}, f)
    with open(os.path.join(args.out, "combined_trades.json"), "w") as f:
        json.dump(runs["combined"]["trades"], f, indent=1, default=str)
    print(f"correlation of daily returns CHAND4 vs XSMOM: {rho:+.3f}")
    print(f"wrote {args.out}/ ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
