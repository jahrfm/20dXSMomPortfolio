"""combined_exec/strategy.py — the strategy rules, in ONE place.

Everything that decides *what* to trade lives here as pure functions over
completed daily bars. The backtest (backtest/combined_backtest.py), the
Hermes signal generator (combined_signal.py) and the VPS executor
(combined_exec/run.py) all call these same functions, so what is backtested
is what is traded. No network, no exchange code, stdlib only.

Rules
-----
Universe (point-in-time): symbols with >= MIN_HISTORY completed daily bars,
  excluding stablecoin pairs, ranked by mean daily turnover over the last
  30 completed bars; top 50.

CHAND4 40/15 (from jahrfm/20d; its 2026-09-28 audit found no out-of-sample
evidence that this tuned exit beats the plain channel breakout — see
backtest/README.md for the side-by-side on this repo's universe):
  signal   new 40-day high (long) / low (short) on the completed bar
           (high > max of the prior 40 highs), ranked by
           |close - SMA40(prior 40 closes)| / ATR14.
  entry    next bar's open.
  stop     initial = lowest low of the 15 bars ending at the signal bar
           (highest high for shorts); then daily chandelier
           highest-high-since-entry - 4 x ATR14, ratcheted (never loosened).
           ATR uses completed bars only (no current-bar lookahead).
  book     at most 5 concurrent positions per side.

XSMOM (from jahrfm/MOMSXperp):
  schedule every 14 days on Mondays, anchored at 2026-08-17.
  signal   30d raw return / (ATR% x sqrt(30)) ("VNR") on completed bars;
           longs must close above MA100 with a positive 30d return; top 5.
  weights  linear signal-proportional (MOMSXperp _size_longs), applied as a
           multiplier on the per-trade risk budget (mean multiplier = 1).
  exit     15% hard stop from entry; names that drop out of the top 5 are
           closed at the rebalance; names that stay are held (no churn).
  long-only (the short side is dormant in the research and never traded).

Both legs share one account. A symbol can be held by only one leg at a time
(Bybit one-way mode nets positions per symbol).
"""
import math
from datetime import date, datetime, timedelta, timezone

DEFAULTS = {
    # universe
    "universe_size": 50,
    "universe_turnover_window": 30,
    "min_history": 110,
    "atr_period": 14,
    # CHAND4
    "chand4_lookback": 40,
    "chand4_stop_window": 15,
    "chandelier_mult": 4.0,
    "chand4_exit": "chandelier",   # "chandelier" | "channel" (N-day low/high trail, 20d spec)
    "chand4_top_n": 5,          # max concurrent positions PER SIDE
    "chand4_shorts": True,
    "chand4_candidates_kept": 15,  # ranked candidates per side written to the signal
    # XSMOM — OFF by default: the joint point-in-time backtest (backtest/)
    # shows no edge after costs (Sharpe ~0.2, PF ~1.0) and it drags the
    # combined book down. The signal is still computed and logged.
    "xsmom_enabled": False,
    "xsmom_top_n": 5,
    "xsmom_window": 30,
    "xsmom_ma": 100,
    "xsmom_stop_pct": 0.15,
    "xsmom_rebalance_days": 14,
    "xsmom_anchor": "2026-08-17",
    "xsmom_weighting": "signal",  # "signal" (research) | "equal"
    # sizing / risk
    "risk_pct": 0.005,           # fraction of equity risked per trade (to the stop)
    "chand4_risk_pct": None,     # None -> risk_pct
    "xsmom_risk_pct": None,      # None -> risk_pct
    "max_positions": 15,
    "cap_daily": 0.90,           # max total initial margin / equity
    "max_position_notional": 0.35,  # max notional of one position / equity
    "global_max_lev": 5,
    "mmr": 0.005,
    "liq_safety": 1.5,
    "stale_pct": 5.0,            # skip entries whose price drifted > x% from signal close
}

STABLE_BASES = {
    "USDC", "USDE", "FDUSD", "DAI", "TUSD", "BUSD", "USD1", "USDD", "PYUSD",
    "USDP", "UST", "USTC", "EURC", "EUR", "RLUSD", "USDQ", "USDR", "SUSD",
}
# Tokenised gold trades like a commodity, not a crypto trend.
EXCLUDED_BASES = STABLE_BASES | {"XAUT", "PAXG"}
# Bybit instrument symbolType: "" and "innovation" are crypto; "stock",
# "ETF", "commodity", "forex" are TradFi perps (weekend gaps, different
# behaviour) and were never in the backtest universe. Same rule as MOMSXperp.
CRYPTO_SYMBOL_TYPES = {"", "innovation"}


def is_stable(symbol):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base in EXCLUDED_BASES


def is_crypto_instrument(symbol_type, base_coin):
    """True for crypto perps; False for stock/ETF/commodity/forex perps,
    stablecoins and tokenised gold."""
    return (symbol_type or "") in CRYPTO_SYMBOL_TYPES and base_coin not in EXCLUDED_BASES


def day_str(d):
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


def parse_day(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def utc_today():
    return datetime.now(timezone.utc).date()


# ── price series ────────────────────────────────────────────────────────────
def wilder_atr(high, low, close, n=14):
    """Wilder ATR, element i uses bars <= i only. None until warmed up."""
    size = len(close)
    out = [None] * size
    if size < n:
        return out
    trs = [high[0] - low[0]]
    for i in range(1, size):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    out[n - 1] = sum(trs[:n]) / n
    for i in range(n, size):
        out[i] = (out[i - 1] * (n - 1) + trs[i]) / n
    return out


class Series:
    """Completed daily bars for one symbol, oldest first."""

    def __init__(self, symbol, rows, atr_period=14):
        rows = sorted(rows, key=lambda r: r[0])
        self.symbol = symbol
        self.dates = [r[0] for r in rows]
        self.open = [float(r[1]) for r in rows]
        self.high = [float(r[2]) for r in rows]
        self.low = [float(r[3]) for r in rows]
        self.close = [float(r[4]) for r in rows]
        self.turnover = [float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
                         for r in rows]
        self.idx = {d: i for i, d in enumerate(self.dates)}
        self.atr = wilder_atr(self.high, self.low, self.close, atr_period)
        pt = [0.0]
        for t in self.turnover:
            pt.append(pt[-1] + t)
        self._pt = pt

    def __len__(self):
        return len(self.dates)

    def mean_turnover(self, i, window):
        lo = max(0, i + 1 - window)
        return (self._pt[i + 1] - self._pt[lo]) / (i + 1 - lo)

    @classmethod
    def from_bybit(cls, symbol, raw_rows, before=None, atr_period=14):
        """Build from Bybit kline rows [startMs, o, h, l, c, volume, turnover]
        (any order). Bars whose day is >= `before` (default: today UTC) are
        dropped, so the still-forming candle is NEVER used."""
        before = day_str(before or utc_today())
        rows = []
        for r in raw_rows:
            d = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc).date().isoformat()
            if d >= before:
                continue
            o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            if min(o, h, l, c) <= 0 or any(math.isnan(x) for x in (o, h, l, c)):
                continue
            rows.append((d, o, h, l, c, float(r[6]) if len(r) > 6 else 0.0))
        return cls(symbol, rows, atr_period)


# ── universe ────────────────────────────────────────────────────────────────
def universe(series_map, asof, p=DEFAULTS):
    """Top-N symbols by trailing mean turnover, using bars <= asof only.
    A symbol must have a bar ON asof (i.e. be trading) to qualify."""
    asof = day_str(asof)
    ranked = []
    for sym, s in series_map.items():
        if is_stable(sym):
            continue
        i = s.idx.get(asof)
        if i is None or i + 1 < p["min_history"]:
            continue
        ranked.append((s.mean_turnover(i, p["universe_turnover_window"]), sym))
    ranked.sort(reverse=True)
    return [sym for _, sym in ranked[:p["universe_size"]]]


# ── CHAND4 ──────────────────────────────────────────────────────────────────
def chand4_candidates(series_map, symbols, asof, p=DEFAULTS):
    """Breakouts on the completed `asof` bar. Returns (longs, shorts), each
    ranked strongest first and truncated to chand4_candidates_kept."""
    asof = day_str(asof)
    lb, sw = p["chand4_lookback"], p["chand4_stop_window"]
    longs, shorts = [], []
    for sym in symbols:
        s = series_map[sym]
        i = s.idx.get(asof)
        if i is None or i < max(lb, sw):
            continue
        atr = s.atr[i]
        if not atr or atr <= 0:
            continue
        prior_hi = max(s.high[i - lb:i])
        prior_lo = min(s.low[i - lb:i])
        sma = sum(s.close[i - lb:i]) / lb
        strength = (s.close[i] - sma) / atr
        base = {"symbol": sym, "signal_date": asof, "signal_close": s.close[i],
                "atr14": atr, "strength": round(abs(strength), 4)}
        if s.high[i] > prior_hi:
            longs.append(dict(base, direction="LONG",
                              initial_stop=min(s.low[i - sw + 1:i + 1])))
        if s.low[i] < prior_lo and p["chand4_shorts"]:
            shorts.append(dict(base, direction="SHORT",
                               initial_stop=max(s.high[i - sw + 1:i + 1])))
    keep = p["chand4_candidates_kept"]
    longs.sort(key=lambda c: -c["strength"])
    shorts.sort(key=lambda c: -c["strength"])
    return longs[:keep], shorts[:keep]


def chand4_trail(series, entry_date, side, prev_stop, asof, p=DEFAULTS):
    """Chandelier stop for the day after `asof`, from bars entry_date..asof.
    Ratchets: never looser than prev_stop. Returns prev_stop if not computable."""
    i = series.idx.get(day_str(asof))
    e = series.idx.get(day_str(entry_date))
    if i is None or e is None or i < e:
        return prev_stop
    if p.get("chand4_exit") == "channel":
        # 20d spec: lowest low (highest high) of the last N completed bars
        # since entry; a sliding-window min can only fall on a stop-out, so
        # this never loosens.
        lo = max(e, i + 1 - p["chand4_stop_window"])
        if side == "LONG":
            new = min(series.low[lo:i + 1])
            return max(prev_stop, new) if prev_stop else new
        new = max(series.high[lo:i + 1])
        return min(prev_stop, new) if prev_stop else new
    atr = series.atr[i]
    if not atr or atr <= 0:
        return prev_stop
    k = p["chandelier_mult"]
    if side == "LONG":
        new = max(series.high[e:i + 1]) - k * atr
        return max(prev_stop, new) if prev_stop else new
    new = min(series.low[e:i + 1]) + k * atr
    return min(prev_stop, new) if prev_stop else new


# ── XSMOM ───────────────────────────────────────────────────────────────────
def xsmom_rebalance_due(day, p=DEFAULTS):
    """True if `day` (the trading day, i.e. signal asof + 1) is a rebalance day."""
    delta = (parse_day(day) - parse_day(p["xsmom_anchor"])).days
    return delta % p["xsmom_rebalance_days"] == 0


def xsmom_last_rebalance(day, p=DEFAULTS):
    d = parse_day(day)
    delta = (d - parse_day(p["xsmom_anchor"])).days
    return d - timedelta(days=delta % p["xsmom_rebalance_days"])


def xsmom_next_rebalance(day, p=DEFAULTS):
    last = xsmom_last_rebalance(day, p)
    return last if last == parse_day(day) else last + timedelta(days=p["xsmom_rebalance_days"])


def xsmom_candidate(series, asof, p=DEFAULTS):
    i = series.idx.get(day_str(asof))
    w = p["xsmom_window"]
    if i is None or i < w or i + 1 < p["xsmom_ma"]:
        return None
    start, end = series.close[i - w], series.close[i]
    if start <= 0 or end <= 0:
        return None
    raw = (end - start) / start
    trs = [max(series.high[j] - series.low[j],
               abs(series.high[j] - series.close[j - 1]),
               abs(series.low[j] - series.close[j - 1])) for j in range(i - w + 1, i + 1)]
    atr_pct = (sum(trs) / len(trs)) / end
    if atr_pct <= 0:
        return None
    vnr = raw / (atr_pct * math.sqrt(w))
    ma = sum(series.close[i + 1 - p["xsmom_ma"]:i + 1]) / p["xsmom_ma"]
    return {"symbol": series.symbol, "close": end, "raw_ret": raw, "vnr": vnr,
            "above_ma": end > ma, "positive_raw": raw > 0}


def signal_weights(values):
    """MOMSXperp linear signal-proportional weights (min-max, sum to 1)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = hi - lo
    raw = [(v - lo) / span if span else 1.0 for v in values]
    if not sum(raw):
        raw = [1.0] * len(values)
    total = sum(raw)
    return [r / total for r in raw]


def xsmom_longs(series_map, symbols, asof, p=DEFAULTS):
    cands = [c for c in (xsmom_candidate(series_map[s], asof, p) for s in symbols) if c]
    cands.sort(key=lambda c: -c["vnr"])
    longs = [c for c in cands if c["above_ma"] and c["positive_raw"]][:p["xsmom_top_n"]]
    if p["xsmom_weighting"] == "signal":
        weights = signal_weights([c["vnr"] for c in longs])
    else:
        weights = [1.0 / len(longs)] * len(longs) if longs else []
    for c, wt in zip(longs, weights):
        c["weight"] = round(wt, 6)
        c["risk_mul"] = round(wt * len(longs), 6)   # mean 1.0
    return longs


# ── the signal book (identical for backtest and live) ────────────────────────
def build_signal(series_map, asof, p=DEFAULTS, universe_syms=None):
    """Everything the executor needs to trade on day asof+1."""
    asof = day_str(asof)
    trade_day = parse_day(asof) + timedelta(days=1)
    syms = universe_syms if universe_syms is not None else universe(series_map, asof, p)
    longs, shorts = chand4_candidates(series_map, syms, asof, p)
    due = xsmom_rebalance_due(trade_day, p)
    return {
        "asof": asof,
        "trade_date": trade_day.isoformat(),
        "universe": syms,
        "chand4": {"longs": longs, "shorts": shorts},
        "xsmom": {
            "is_rebalance": due,
            "rebalance_date": xsmom_next_rebalance(trade_day, p).isoformat(),
            "longs": xsmom_longs(series_map, syms, asof, p) if due else [],
        },
    }


# ── sizing ──────────────────────────────────────────────────────────────────
def stop_derived_leverage(stop_pct, p=DEFAULTS, symbol_max_lev=None):
    """Leverage that keeps (isolated) liquidation beyond the stop:
    1/L - MMR >= stop_pct * LIQ_SAFETY. Same formula as bybit_exec.sizing."""
    denom = stop_pct * p["liq_safety"] + p["mmr"]
    lev = int(1.0 / denom) if denom > 0 else p["global_max_lev"]
    cap = p["global_max_lev"]
    if symbol_max_lev and symbol_max_lev > 0:
        cap = min(cap, symbol_max_lev)
    return max(1, min(cap, lev))


def leg_risk_pct(leg, p=DEFAULTS):
    v = p.get("chand4_risk_pct") if leg == "CHAND4" else p.get("xsmom_risk_pct")
    return p["risk_pct"] if v is None else v
