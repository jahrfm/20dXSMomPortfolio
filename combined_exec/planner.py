"""combined_exec/planner.py — turn a signal book + current holdings into a
day's actions. Shared by the backtest and the live/paper executor.

holdings: {symbol: {"leg": "CHAND4"|"XSMOM", "side": "LONG"|"SHORT",
                    "qty", "entry_price", "entry_date", "stop", "margin"}}
"""
from .strategy import DEFAULTS, chand4_trail, leg_risk_pct, stop_derived_leverage


def trail_updates(holdings, series_map, asof, p=DEFAULTS):
    """New chandelier stops for CHAND4 holdings, from bars <= asof.
    Only tightened stops are returned."""
    out = {}
    for sym, h in holdings.items():
        if h["leg"] != "CHAND4" or sym not in series_map:
            continue
        new = chand4_trail(series_map[sym], h["entry_date"], h["side"], h["stop"], asof, p)
        tighter = new > h["stop"] if h["side"] == "LONG" else new < h["stop"]
        if tighter:
            out[sym] = new
    return out


def size_entry(leg, side, price, stop, equity, risk_mul, p=DEFAULTS, symbol_max_lev=None):
    """Risk-% sizing: qty such that a stop-out loses equity*risk_pct*risk_mul,
    capped at max_position_notional of equity. Returns dict or None."""
    dist = abs(price - stop)
    if dist <= 0 or price <= 0 or risk_mul <= 0:
        return None
    qty = equity * leg_risk_pct(leg, p) * risk_mul / dist
    max_notional = equity * p["max_position_notional"]
    if qty * price > max_notional:
        qty = max_notional / price
    stop_pct = dist / price
    lev = stop_derived_leverage(stop_pct, p, symbol_max_lev)
    notional = qty * price
    return {"qty": qty, "notional": notional, "leverage": lev,
            "margin": notional / lev, "stop_pct": stop_pct, "risk": qty * dist}


def plan_day(signal, holdings, equity, prices, p=DEFAULTS, unmanaged=(),
             symbol_max_lev=None):
    """Return {"exits": [(sym, reason)], "entries": [spec], "skipped": [...]}.

    prices: {symbol: price an entry would fill at now} (next open in the
    backtest, live mark on the VPS). symbol_max_lev: {symbol: maxLeverage}.
    """
    symbol_max_lev = symbol_max_lev or {}
    exits, entries, skipped = [], [], []
    xs = signal.get("xsmom", {})
    xs_on = p.get("xsmom_enabled", True)

    # 1. XSMOM rebalance exits: names no longer in the top-N (all of them
    #    when the leg is disabled, so a switched-off leg winds down cleanly)
    if xs.get("is_rebalance"):
        target = {c["symbol"] for c in xs.get("longs", [])} if xs_on else set()
        for sym, h in holdings.items():
            if h["leg"] == "XSMOM" and sym not in target:
                exits.append((sym, "XSMOM-REBALANCE"))
    remaining = {s: h for s, h in holdings.items() if s not in {e[0] for e in exits}}

    taken = set(remaining) | set(unmanaged)
    n_open = len(taken)
    margin_used = sum(h.get("margin", 0.0) for h in remaining.values())
    budget = p["cap_daily"] * equity
    count = {("CHAND4", "LONG"): 0, ("CHAND4", "SHORT"): 0, ("XSMOM", "LONG"): 0}
    for h in remaining.values():
        count[(h["leg"], h["side"])] = count.get((h["leg"], h["side"]), 0) + 1
    limit = {("CHAND4", "LONG"): p["chand4_top_n"], ("CHAND4", "SHORT"): p["chand4_top_n"],
             ("XSMOM", "LONG"): p["xsmom_top_n"]}

    queue = []
    if xs.get("is_rebalance") and xs_on:
        queue += [("XSMOM", "LONG", c) for c in xs.get("longs", [])]
    queue += [("CHAND4", "LONG", c) for c in signal.get("chand4", {}).get("longs", [])]
    if p["chand4_shorts"]:
        queue += [("CHAND4", "SHORT", c) for c in signal.get("chand4", {}).get("shorts", [])]

    for leg, side, c in queue:
        sym = c["symbol"]
        key = (leg, side)

        def skip(reason):
            skipped.append({"symbol": sym, "leg": leg, "side": side, "reason": reason})

        if sym in taken:
            if not (sym in remaining and remaining[sym]["leg"] == leg):
                skip("symbol held by another leg/unmanaged")
            continue
        if count[key] >= limit[key]:
            continue            # book full for this leg/side; not an error
        if n_open >= p["max_positions"]:
            skip("max_positions")
            continue
        price = prices.get(sym)
        if not price or price <= 0:
            skip("no price")
            continue
        ref = c.get("signal_close") or c.get("close")
        if ref and abs(price / ref - 1) * 100 > p["stale_pct"]:
            skip(f"stale: price {price:.6g} vs signal close {ref:.6g}")
            continue
        if leg == "CHAND4":
            stop = c["initial_stop"]
            if (side == "LONG" and price <= stop) or (side == "SHORT" and price >= stop):
                skip("price already through initial stop")
                continue
            mul = 1.0
        else:
            stop = price * (1 - p["xsmom_stop_pct"])
            mul = c.get("risk_mul", 1.0)
            if mul <= 0:
                skip("zero signal weight")
                continue
        sz = size_entry(leg, side, price, stop, equity, mul, p, symbol_max_lev.get(sym))
        if not sz:
            skip("unsizeable")
            continue
        if margin_used + sz["margin"] > budget + 1e-9:
            skip("margin cap")
            continue
        entries.append(dict(sz, symbol=sym, leg=leg, side=side, price=price, stop=stop,
                            risk_mul=mul, signal_date=c.get("signal_date") or signal.get("asof")))
        taken.add(sym)
        n_open += 1
        count[key] += 1
        margin_used += sz["margin"]
    return {"exits": exits, "entries": entries, "skipped": skipped}
