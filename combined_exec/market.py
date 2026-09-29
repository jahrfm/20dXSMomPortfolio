"""combined_exec/market.py — public Bybit V5 market data (no keys, stdlib).

Used by the signal generator (Hermes), the trailing-stop pass and the paper
broker. Always point it at MAINNET for prices; testnet data is not real.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .strategy import Series, is_crypto_instrument


class MarketError(Exception):
    pass


class Market:
    def __init__(self, base_url="https://api.bybit.com", timeout=20, delay=0.05):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self._instruments = None

    def _get(self, path, params):
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "combined-exec/2"})
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode())
                if d.get("retCode") != 0:
                    raise MarketError(f"{path} [{d.get('retCode')}] {d.get('retMsg')}")
                time.sleep(self.delay)
                return d.get("result", {})
            except (urllib.error.URLError, OSError, ValueError, MarketError) as e:
                last = e
                time.sleep(1 + 2 * attempt)
        raise MarketError(f"GET {path} failed: {last}")

    def tickers(self):
        """{symbol: {"mark", "last", "turnover24h", "funding"}} for USDT linear perps."""
        out = {}
        for t in self._get("/v5/market/tickers", {"category": "linear"}).get("list", []):
            s = t.get("symbol", "")
            if not s.endswith("USDT"):
                continue
            try:
                out[s] = {"mark": float(t.get("markPrice") or 0),
                          "last": float(t.get("lastPrice") or 0),
                          "turnover24h": float(t.get("turnover24h") or 0),
                          "funding": float(t.get("fundingRate") or 0)}
            except ValueError:
                continue
        return out

    def marks(self, symbols=None):
        tk = self.tickers()
        want = set(symbols) if symbols is not None else set(tk)
        return {s: v["mark"] for s, v in tk.items() if s in want and v["mark"] > 0}

    def instruments(self):
        """{symbol: {status, qtyStep, minQty, maxQty, tickSize, maxLeverage}}"""
        if self._instruments is None:
            out, cursor = {}, None
            while True:
                params = {"category": "linear", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                res = self._get("/v5/market/instruments-info", params)
                for it in res.get("list", []):
                    lot, prc, lev = (it.get("lotSizeFilter", {}), it.get("priceFilter", {}),
                                     it.get("leverageFilter", {}))
                    out[it["symbol"]] = {
                        "status": it.get("status"),
                        "contractType": it.get("contractType"),
                        "symbolType": it.get("symbolType", ""),
                        "baseCoin": it.get("baseCoin", ""),
                        "qtyStep": float(lot.get("qtyStep") or 1),
                        "minQty": float(lot.get("minOrderQty") or 0),
                        "maxQty": float(lot.get("maxMktOrderQty") or lot.get("maxOrderQty") or 0),
                        "minNotional": float(lot.get("minNotionalValue") or 5),
                        "tickSize": float(prc.get("tickSize") or 0.0001),
                        "maxLeverage": float(lev.get("maxLeverage") or 0),
                    }
                cursor = res.get("nextPageCursor")
                if not cursor:
                    break
            self._instruments = out
        return self._instruments

    def tradable_usdt_perps(self):
        """Trading USDT linear perps that are crypto (no stock/ETF/commodity/
        forex perps, stablecoins or tokenised gold) — the backtest universe."""
        return [s for s, i in self.instruments().items()
                if s.endswith("USDT") and i["status"] == "Trading"
                and i.get("contractType") in (None, "LinearPerpetual")
                and is_crypto_instrument(i.get("symbolType"), i.get("baseCoin"))]

    def klines(self, symbol, limit=300):
        rows = self._get("/v5/market/kline", {"category": "linear", "symbol": symbol,
                                              "interval": "D", "limit": min(limit, 1000)})
        return rows.get("list", [])

    def series(self, symbol, limit=300, before=None):
        """Completed daily bars only (the forming candle is dropped)."""
        return Series.from_bybit(symbol, self.klines(symbol, limit), before=before)
