"""combined_exec/broker.py — one interface, two implementations.

  BybitBroker  real orders on the combined sub-account (COMBINED_AUTO_TRADE=true)
  PaperBroker  simulated account on real MAINNET prices (default). Fills at
               the mark +/- slippage with taker fees; stops are settled from
               completed daily bars (gap-through fills at the open, like the
               backtest) and from the live mark on every run.

The executor (run.py) only calls the methods below, so paper and live run the
exact same decision code.
"""
import json
import math
import os
import time


from .strategy import day_str, utc_today

FEE = 0.00055
SLIP = 0.0005
LEGACY_LINK_PREFIXES = ("CH4-", "XSM-")   # PostOnly orders placed by the v1 executor


def round_qty(qty, step):
    if step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


def round_stop(price, tick, side):
    """Long stops round down, short stops round up (never tighter than planned)."""
    if tick <= 0:
        return price
    n = price / tick
    n = math.floor(n + 1e-9) if side == "LONG" else math.ceil(n - 1e-9)
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(n * tick, decimals + 2)


def fmt(x):
    return format(x, "f").rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


class BybitBroker:
    mode = "live"

    def __init__(self, cfg, log):
        from bybit_exec.bybit_client import BybitAPIError, BybitClient  # DMA engine repo
        self.client = BybitClient(cfg)
        self.APIError = BybitAPIError
        self.log = log
        self._inst = {}

    def equity(self, marks=None):
        return float(self.client.wallet_balance()["totalEquity"])

    def positions(self):
        return {p["symbol"]: {"side": "LONG" if p["side"] == "Buy" else "SHORT",
                              "size": float(p["size"]), "avg_price": float(p["avgPrice"])}
                for p in self.client.positions()}

    def instrument(self, symbol):
        if symbol not in self._inst:
            self._inst[symbol] = self.client.instruments(symbol)
        return self._inst[symbol]

    def marks(self, symbols):
        return {s: v for s, v in self.client.ticker_marks().items() if s in set(symbols)}

    def settle(self, today):
        return []          # the exchange enforces stops itself

    def set_leverage(self, symbol, lev):
        try:
            self.client.set_leverage(symbol, lev)
        except self.APIError as e:
            if e.ret_code != 110043:          # leverage not modified
                raise

    def _position(self, symbol):
        for s, p in self.positions().items():
            if s == symbol:
                return p
        return None

    def open(self, symbol, side, qty, stop, link, today):
        body = {"category": "linear", "symbol": symbol,
                "side": "Buy" if side == "LONG" else "Sell",
                "orderType": "Market", "qty": fmt(qty), "positionIdx": 0,
                "stopLoss": fmt(stop), "slTriggerBy": "LastPrice", "tpslMode": "Full",
                "orderLinkId": link}
        try:
            self.client._signed_post("/v5/order/create", body)
        except self.APIError as e:
            if e.ret_code not in (110072, 10014):   # duplicate orderLinkId -> already sent
                raise
            self.log(f"[OPEN] {symbol}: orderLinkId {link} already used; reading position")
        for _ in range(10):
            p = self._position(symbol)
            if p and p["size"] > 0:
                return {"qty": p["size"], "avg_price": p["avg_price"]}
            time.sleep(0.5)
        return None

    def close(self, symbol, side, qty, link, today):
        body = {"category": "linear", "symbol": symbol,
                "side": "Sell" if side == "LONG" else "Buy", "orderType": "Market",
                "qty": fmt(qty), "reduceOnly": True, "positionIdx": 0, "orderLinkId": link}
        try:
            self.client._signed_post("/v5/order/create", body)
        except self.APIError as e:
            if e.ret_code not in (110072, 10014):
                raise
        info = self.exit_info(symbol)
        return info or {"price": None, "date": day_str(today), "pnl": None}

    def set_stop(self, symbol, side, stop):
        body = {"category": "linear", "symbol": symbol, "stopLoss": fmt(stop),
                "slTriggerBy": "LastPrice", "tpslMode": "Full", "positionIdx": 0}
        try:
            self.client._signed_post("/v5/position/trading-stop", body)
        except self.APIError as e:
            if e.ret_code != 34040:            # not modified
                raise

    def exit_info(self, symbol):
        """Most recent closed-PnL record for the symbol (best effort)."""
        try:
            rows = self.client.closed_pnl(symbol=symbol, days=30)
        except Exception:
            return None
        if not rows:
            return None
        r = max(rows, key=lambda x: int(x.get("updatedTime") or x.get("createdTime") or 0))
        from datetime import datetime, timezone
        ts = int(r.get("updatedTime") or r.get("createdTime") or 0)
        return {"price": float(r.get("avgExitPrice") or 0) or None,
                "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat(),
                "pnl": float(r.get("closedPnl") or 0)}

    def cancel_legacy_orders(self):
        """Cancel resting entry orders left by the v1 executor (tagged CH4-/XSM-)."""
        n = 0
        for o in self.client.order_realtime():
            link = o.get("orderLinkId") or ""
            if link.startswith(LEGACY_LINK_PREFIXES) and not o.get("reduceOnly"):
                self.client.cancel_order(o["symbol"], order_id=o.get("orderId"))
                self.log(f"[CANCEL] legacy order {link} {o['symbol']}")
                n += 1
        return n


class PaperBroker:
    mode = "paper"

    def __init__(self, cfg, market, log):
        self.market = market
        self.log = log
        self.path = os.path.join(cfg["state_dir"], "paper_account.json")
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.acct = json.load(f)
        else:
            self.acct = {"cash": cfg["paper_equity"], "start_equity": cfg["paper_equity"],
                         "positions": {}, "closed": []}
        self._marks = None

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.acct, f, indent=2)
        os.replace(tmp, self.path)

    def _all_marks(self):
        if self._marks is None:
            self._marks = self.market.marks()
        return self._marks

    def marks(self, symbols):
        m = self._all_marks()
        return {s: m[s] for s in symbols if s in m}

    def equity(self, marks=None):
        m = marks or self._all_marks()
        eq = self.acct["cash"]
        for s, p in self.acct["positions"].items():
            px = m.get(s, p["avg_price"])
            eq += p["size"] * (px - p["avg_price"]) * (1 if p["side"] == "LONG" else -1)
        return eq

    def positions(self):
        return {s: {"side": p["side"], "size": p["size"], "avg_price": p["avg_price"]}
                for s, p in self.acct["positions"].items()}

    def instrument(self, symbol):
        return self.market.instruments()[symbol]

    def set_leverage(self, symbol, lev):
        pass

    def _fill(self, symbol, side, qty, raw_px, date, reason):
        p = self.acct["positions"].pop(symbol)
        px = raw_px * (1 - SLIP if side == "LONG" else 1 + SLIP)
        fee = qty * px * FEE
        pnl = qty * (px - p["avg_price"]) * (1 if side == "LONG" else -1) - fee
        self.acct["cash"] += pnl
        self.acct["closed"].append({"symbol": symbol, "side": side, "qty": qty,
                                    "entry": p["avg_price"], "exit": px, "date": date,
                                    "reason": reason, "pnl": pnl})
        self._save()
        return {"price": px, "date": date, "pnl": pnl}

    def open(self, symbol, side, qty, stop, link, today):
        mark = self._all_marks().get(symbol)
        if not mark:
            return None
        px = mark * (1 + SLIP if side == "LONG" else 1 - SLIP)
        self.acct["cash"] -= qty * px * FEE
        self.acct["positions"][symbol] = {"side": side, "size": qty, "avg_price": px,
                                          "stop": stop, "check_from": day_str(today),
                                          "link": link}
        self._save()
        return {"qty": qty, "avg_price": px}

    def close(self, symbol, side, qty, link, today):
        mark = self._all_marks().get(symbol) or self.acct["positions"][symbol]["avg_price"]
        return self._fill(symbol, side, qty, mark, day_str(today), "CLOSE")

    def set_stop(self, symbol, side, stop):
        self.acct["positions"][symbol]["stop"] = stop
        self._save()

    def settle(self, today):
        """Apply stops: completed daily bars since entry/last check, then the
        live mark. Returns [(symbol, exit_info)]."""
        out = []
        today = day_str(today)
        for sym in list(self.acct["positions"]):
            p = self.acct["positions"][sym]
            long_ = p["side"] == "LONG"
            hit = None
            if p["check_from"] < today:
                s = self.market.series(sym, limit=60)
                for i, d in enumerate(s.dates):
                    if d < p["check_from"]:
                        continue
                    if long_ and s.low[i] <= p["stop"]:
                        hit = (min(p["stop"], s.open[i]), d)
                        break
                    if not long_ and s.high[i] >= p["stop"]:
                        hit = (max(p["stop"], s.open[i]), d)
                        break
                if not hit:
                    p["check_from"] = today
            if not hit:
                mark = self._all_marks().get(sym)
                if mark and ((long_ and mark <= p["stop"]) or (not long_ and mark >= p["stop"])):
                    hit = (mark, today)
            if hit:
                out.append((sym, self._fill(sym, p["side"], p["size"], hit[0], hit[1], "STOP")))
        self._save()
        return out

    def exit_info(self, symbol):
        for c in reversed(self.acct["closed"]):
            if c["symbol"] == symbol:
                return {"price": c["exit"], "date": c["date"], "pnl": c["pnl"]}
        return None

    def cancel_legacy_orders(self):
        return 0
