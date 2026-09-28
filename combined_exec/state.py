"""combined_exec/state.py — which leg owns which position.

Bybit's position list carries no order tags, so leg attribution, entry date,
initial stop and the trailing-stop ratchet live here. One JSON file per mode
(live / paper) in COMBINED_STATE_DIR, written atomically after every action
so a crash mid-run never loses track of an opened position.
"""
import json
import os
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path, data=None):
        self.path = path
        self.data = data or {"version": 2, "positions": {}, "history": [],
                             "last_signal_asof": None, "log": []}

    @classmethod
    def load(cls, path):
        if os.path.exists(path):
            with open(path) as f:
                return cls(path, json.load(f))
        return cls(path)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # positions: {symbol: {leg, side, qty, entry_price, entry_date, stop,
    #                      initial_stop, margin, leverage, risk, order_link_id}}
    @property
    def positions(self):
        return self.data["positions"]

    def add(self, symbol, pos):
        pos = dict(pos, opened_at=_now())
        self.positions[symbol] = pos
        self.save()

    def remove(self, symbol, exit_price, reason, exit_date, pnl=None):
        pos = self.positions.pop(symbol)
        sign = 1 if pos["side"] == "LONG" else -1
        if pnl is None and exit_price:
            pnl = pos["qty"] * (exit_price - pos["entry_price"]) * sign
        self.data["history"].append(dict(pos, symbol=symbol, exit_price=exit_price,
                                          exit_date=exit_date, reason=reason, pnl=pnl,
                                          closed_at=_now()))
        self.save()
        return pos

    def set_stop(self, symbol, stop):
        self.positions[symbol]["stop"] = stop
        self.save()

    def note(self, msg):
        self.data["log"] = (self.data.get("log", []) + [f"{_now()} {msg}"])[-500:]
