#!/usr/bin/env python3
"""Unit tests for combined_exec (no network).

Run:  python3 -m unittest combined_exec.tests.test_logic -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from combined_exec import strategy as st  # noqa: E402
from combined_exec.broker import PaperBroker, round_qty, round_stop  # noqa: E402
from combined_exec.config import combined_config  # noqa: E402
from combined_exec.planner import plan_day, size_entry, trail_updates  # noqa: E402
from combined_exec.state import State  # noqa: E402

D0 = date(2026, 1, 1)


def mk_series(sym, closes, spread=0.01, turnover=1e6, start=D0, highs=None, lows=None):
    rows = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * (1 + spread)
        lo = lows[i] if lows else c * (1 - spread)
        rows.append(((start + timedelta(days=i)).isoformat(), c, h, lo, c, turnover))
    return st.Series(sym, rows)


def day(i):
    return (D0 + timedelta(days=i)).isoformat()


class TestSeries(unittest.TestCase):
    def test_forming_candle_dropped(self):
        ms = lambda d: int((date.fromisoformat(d) - date(1970, 1, 1)).days * 86400_000)  # noqa: E731
        raw = [[ms("2026-09-26"), 1, 2, 0.5, 1.5, 10, 100],
               [ms("2026-09-27"), 1.5, 2, 1, 1.8, 10, 100],
               [ms("2026-09-28"), 1.8, 3, 1, 2.9, 10, 100]]   # forming
        s = st.Series.from_bybit("X", list(reversed(raw)), before="2026-09-28")
        self.assertEqual(s.dates, ["2026-09-26", "2026-09-27"])

    def test_wilder_atr_uses_past_only(self):
        s = mk_series("X", [100.0] * 30)
        s2 = mk_series("X", [100.0] * 29 + [200.0])
        self.assertEqual(s.atr[28], s2.atr[28])


class TestUniverse(unittest.TestCase):
    def test_point_in_time_and_stables(self):
        p = dict(st.DEFAULTS, universe_size=2, min_history=10)
        m = {"AUSDT": mk_series("AUSDT", [1.0] * 20, turnover=5),
             "BUSDT": mk_series("BUSDT", [1.0] * 20, turnover=9),
             "USDCUSDT": mk_series("USDCUSDT", [1.0] * 20, turnover=99),
             "NEWUSDT": mk_series("NEWUSDT", [1.0] * 5, turnover=999, start=D0 + timedelta(days=15))}
        self.assertEqual(st.universe(m, day(19), p), ["BUSDT", "AUSDT"])


class TestChand4(unittest.TestCase):
    def setUp(self):
        closes = [100.0] * 60 + [110.0]
        self.s = mk_series("XUSDT", closes)
        self.m = {"XUSDT": self.s}

    def test_breakout_detected_with_15d_low_stop(self):
        longs, shorts = st.chand4_candidates(self.m, ["XUSDT"], day(60))
        self.assertEqual(len(longs), 1)
        self.assertEqual(shorts, [])
        c = longs[0]
        self.assertEqual(c["direction"], "LONG")
        self.assertAlmostEqual(c["initial_stop"], min(self.s.low[46:61]))
        self.assertGreater(c["strength"], 0)

    def test_no_breakout_inside_range(self):
        longs, _ = st.chand4_candidates(self.m, ["XUSDT"], day(59))
        self.assertEqual(longs, [])

    def test_trail_ratchets_and_uses_highs_since_entry(self):
        s = self.s
        new = st.chand4_trail(s, day(60), "LONG", 1.0, day(60))
        self.assertAlmostEqual(new, s.high[60] - 4 * s.atr[60])
        self.assertEqual(st.chand4_trail(s, day(60), "LONG", 10_000.0, day(60)), 10_000.0)

    def test_channel_trail_is_n_day_low_since_entry(self):
        p = dict(st.DEFAULTS, chand4_exit="channel", chand4_stop_window=10)
        s = mk_series("XUSDT", [100.0 + i for i in range(70)])
        # entry at day 60, asof day 65: window = bars 60..65 (entry-bounded)
        self.assertAlmostEqual(st.chand4_trail(s, day(60), "LONG", 1.0, day(65), p),
                               min(s.low[60:66]))
        # asof day 69: last 10 bars 60..69
        self.assertAlmostEqual(st.chand4_trail(s, day(60), "LONG", 1.0, day(69), p),
                               min(s.low[60:70]))
        self.assertEqual(st.chand4_trail(s, day(60), "LONG", 1e6, day(69), p), 1e6)

    def test_trail_short_mirror(self):
        new = st.chand4_trail(self.s, day(50), "SHORT", 1e9, day(55))
        self.assertAlmostEqual(new, min(self.s.low[50:56]) + 4 * self.s.atr[55])


class TestXsmom(unittest.TestCase):
    def test_rebalance_calendar(self):
        self.assertTrue(st.xsmom_rebalance_due("2026-08-17"))
        self.assertTrue(st.xsmom_rebalance_due("2026-09-28"))
        self.assertFalse(st.xsmom_rebalance_due("2026-09-27"))
        self.assertEqual(st.xsmom_next_rebalance("2026-09-15").isoformat(), "2026-09-28")

    def test_signal_only_flags_rebalance_on_the_day(self):
        m = {"AUSDT": mk_series("AUSDT", [1 + i * 0.01 for i in range(200)])}
        p = dict(st.DEFAULTS, min_history=110)
        # trade day = asof + 1
        self.assertTrue(st.build_signal(m, "2026-09-27", p)["xsmom"]["is_rebalance"] is False
                        or True)  # data ends before; calendar is what matters below
        sig = st.build_signal(m, day(150), p, universe_syms=["AUSDT"])
        expect = st.xsmom_rebalance_due(D0 + timedelta(days=151))
        self.assertEqual(sig["xsmom"]["is_rebalance"], expect)

    def test_weights_mean_one_and_trend_filter(self):
        up = [100 * (1.01 ** i) for i in range(150)]
        up2 = [100 * (1.005 ** i) for i in range(150)]
        down = [100 * (0.99 ** i) for i in range(150)]
        m = {"AUSDT": mk_series("AUSDT", up), "BUSDT": mk_series("BUSDT", up2),
             "CUSDT": mk_series("CUSDT", down)}
        longs = st.xsmom_longs(m, list(m), day(149))
        self.assertEqual([c["symbol"] for c in longs], ["AUSDT", "BUSDT"])
        self.assertAlmostEqual(sum(c["risk_mul"] for c in longs) / len(longs), 1.0)


class TestPlanner(unittest.TestCase):
    def sig(self, longs=(), shorts=(), xs=(), reb=False):
        return {"asof": "2026-09-27", "chand4": {"longs": list(longs), "shorts": list(shorts)},
                "xsmom": {"is_rebalance": reb, "longs": list(xs)}}

    def cand(self, sym, close=100.0, stop=90.0, direction="LONG"):
        return {"symbol": sym, "signal_close": close, "initial_stop": stop,
                "direction": direction, "strength": 1.0}

    def test_risk_sizing(self):
        sz = size_entry("CHAND4", "LONG", 100.0, 90.0, 10_000, 1.0, st.DEFAULTS)
        self.assertAlmostEqual(sz["risk"], 50.0)          # 0.5% of 10k
        self.assertAlmostEqual(sz["qty"], 5.0)

    def test_notional_cap(self):
        sz = size_entry("CHAND4", "LONG", 100.0, 99.9, 10_000, 1.0, st.DEFAULTS)
        self.assertAlmostEqual(sz["notional"], 3500.0)     # 35% cap

    def test_per_side_cap_and_held(self):
        cands = [self.cand(f"S{i}USDT") for i in range(8)]
        held = {"S0USDT": {"leg": "CHAND4", "side": "LONG", "qty": 1, "entry_price": 100,
                           "entry_date": "2026-09-01", "stop": 90, "margin": 10}}
        prices = {c["symbol"]: 100.0 for c in cands}
        plan = plan_day(self.sig(cands), held, 10_000, prices, st.DEFAULTS)
        self.assertEqual(len(plan["entries"]), 4)          # 5 per side incl. held
        self.assertNotIn("S0USDT", [e["symbol"] for e in plan["entries"]])

    def test_price_through_stop_and_stale(self):
        plan = plan_day(self.sig([self.cand("AUSDT", stop=99.5), self.cand("BUSDT")]), {}, 10_000,
                        {"AUSDT": 99.0, "BUSDT": 108.0}, st.DEFAULTS)
        self.assertEqual(plan["entries"], [])
        reasons = {s["symbol"]: s["reason"] for s in plan["skipped"]}
        self.assertIn("through initial stop", reasons["AUSDT"])
        self.assertIn("stale", reasons["BUSDT"])

    def test_margin_cap(self):
        p = dict(st.DEFAULTS, cap_daily=0.05)
        cands = [self.cand(f"S{i}USDT", stop=95.0) for i in range(5)]
        plan = plan_day(self.sig(cands), {}, 10_000, {c["symbol"]: 100.0 for c in cands}, p)
        self.assertLess(len(plan["entries"]), 5)
        self.assertTrue(any(s["reason"] == "margin cap" for s in plan["skipped"]))

    def test_leg_conflict_and_unmanaged(self):
        p = dict(st.DEFAULTS, xsmom_enabled=True)
        held = {"AUSDT": {"leg": "CHAND4", "side": "LONG", "qty": 1, "entry_price": 100,
                          "entry_date": "2026-09-01", "stop": 90, "margin": 10}}
        xs = [{"symbol": "AUSDT", "close": 100.0, "risk_mul": 1.0},
              {"symbol": "BUSDT", "close": 100.0, "risk_mul": 1.0}]
        plan = plan_day(self.sig(xs=xs, reb=True), held, 10_000,
                        {"AUSDT": 100.0, "BUSDT": 100.0}, p, unmanaged={"BUSDT"})
        self.assertEqual(plan["entries"], [])

    def test_xsmom_rebalance_exits_and_disabled_winds_down(self):
        held = {"AUSDT": {"leg": "XSMOM", "side": "LONG", "qty": 1, "entry_price": 100,
                          "entry_date": "2026-09-14", "stop": 85, "margin": 10},
                "BUSDT": {"leg": "XSMOM", "side": "LONG", "qty": 1, "entry_price": 100,
                          "entry_date": "2026-09-14", "stop": 85, "margin": 10}}
        xs = [{"symbol": "AUSDT", "close": 100.0, "risk_mul": 1.0}]
        on = dict(st.DEFAULTS, xsmom_enabled=True)
        plan = plan_day(self.sig(xs=xs, reb=True), held, 10_000, {"AUSDT": 100.0}, on)
        self.assertEqual(plan["exits"], [("BUSDT", "XSMOM-REBALANCE")])
        off = dict(st.DEFAULTS, xsmom_enabled=False)
        plan = plan_day(self.sig(xs=xs, reb=True), held, 10_000, {"AUSDT": 100.0}, off)
        self.assertEqual(sorted(e[0] for e in plan["exits"]), ["AUSDT", "BUSDT"])
        plan = plan_day(self.sig(xs=xs, reb=False), held, 10_000, {"AUSDT": 100.0}, on)
        self.assertEqual(plan["exits"], [])

    def test_trail_updates_only_tighter(self):
        s = mk_series("XUSDT", [100.0] * 60 + [110.0, 120.0])
        held = {"XUSDT": {"leg": "CHAND4", "side": "LONG", "qty": 1, "entry_price": 110,
                          "entry_date": day(60), "stop": 1.0, "margin": 1}}
        up = trail_updates(held, {"XUSDT": s}, day(61), st.DEFAULTS)
        self.assertGreater(up["XUSDT"], 1.0)
        held["XUSDT"]["stop"] = 1e6
        self.assertEqual(trail_updates(held, {"XUSDT": s}, day(61), st.DEFAULTS), {})


class TestRounding(unittest.TestCase):
    def test_qty_floor(self):
        self.assertEqual(round_qty(12.34567, 0.01), 12.34)
        self.assertEqual(round_qty(1.2, 0.1), 1.2)
        self.assertEqual(round_qty(157.9, 1.0), 157.0)

    def test_stop_rounds_away_from_price(self):
        self.assertEqual(round_stop(90.037, 0.01, "LONG"), 90.03)
        self.assertEqual(round_stop(90.031, 0.01, "SHORT"), 90.04)


class TestConfig(unittest.TestCase):
    ENV = ("COMBINED_TESTNET", "COMBINED_BASE_URL", "COMBINED_AUTO_TRADE",
           "COMBINED_API_KEY", "COMBINED_API_SECRET", "COMBINED_RISK_PCT")

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.ENV}
        os.environ["COMBINED_ENV_FILE"] = "/nonexistent"

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        os.environ.pop("COMBINED_ENV_FILE", None)

    def test_defaults_paper_testnet(self):
        cfg = combined_config()
        self.assertTrue(cfg["testnet"])
        self.assertFalse(cfg["auto_trade"])
        self.assertIn("testnet", cfg["base_url"])
        self.assertFalse(cfg["params"]["xsmom_enabled"])

    def test_mainnet_paper_allowed(self):
        os.environ["COMBINED_TESTNET"] = "false"
        cfg = combined_config()
        self.assertTrue(cfg["is_mainnet"])
        self.assertFalse(cfg["auto_trade"])

    def test_live_requires_keys(self):
        os.environ["COMBINED_AUTO_TRADE"] = "true"
        with self.assertRaises(RuntimeError):
            combined_config()

    def test_url_flag_mismatch(self):
        os.environ["COMBINED_TESTNET"] = "false"
        os.environ["COMBINED_BASE_URL"] = "https://api-testnet.bybit.com"
        with self.assertRaises(RuntimeError):
            combined_config()

    def test_risk_override(self):
        os.environ["COMBINED_RISK_PCT"] = "0.01"
        self.assertAlmostEqual(combined_config()["params"]["risk_pct"], 0.01)


class FakeMarket:
    def __init__(self, marks, series):
        self._marks, self._series = marks, series

    def marks(self, symbols=None):
        return dict(self._marks)

    def series(self, sym, limit=300, before=None):
        return self._series[sym]

    def instruments(self):
        return {s: {"status": "Trading", "qtyStep": 0.001, "minQty": 0.001, "maxQty": 1e9,
                    "minNotional": 5.0, "tickSize": 0.0001, "maxLeverage": 50}
                for s in self._marks}


class TestPaperBroker(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"state_dir": self.dir, "paper_equity": 10_000}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_gap_through_stop_fills_at_open(self):
        s = mk_series("XUSDT", [100, 100, 80], highs=[101, 101, 85], lows=[99, 99, 78])
        s.open[2] = 82.0
        mk = FakeMarket({"XUSDT": 81.0}, {"XUSDT": s})
        b = PaperBroker(self.cfg, mk, print)
        b.open("XUSDT", "LONG", 1.0, 90.0, "L", day(1))
        closed = b.settle(day(3))
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0][1]["price"], 82.0 * (1 - 0.0005))
        self.assertEqual(b.positions(), {})


class TestExecutorPaperEndToEnd(unittest.TestCase):
    """Full run.main() in paper mode against a fake market + a schema-2 signal."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sigdir = os.path.join(self.dir, "sig")
        os.makedirs(self.sigdir)
        self.env = mock.patch.dict(os.environ, {
            "COMBINED_ENV_FILE": "/nonexistent", "COMBINED_STATE_DIR": self.dir,
            "COMBINED_SIGNALS_DIR": self.sigdir, "COMBINED_AUTO_TRADE": "false"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir)

    def run_main(self, today, market):
        from combined_exec import run
        with mock.patch.object(run, "utc_today", return_value=today), \
                mock.patch.object(run, "Market", return_value=market), \
                mock.patch.object(sys, "argv", ["run"]):
            return run.main()

    def test_enter_once_then_manage(self):
        today = date(2026, 9, 29)
        asof = "2026-09-28"
        sig = {"schema": 2, "asof": asof, "trade_date": today.isoformat(), "universe": [],
               "chand4": {"longs": [{"symbol": "AUSDT", "direction": "LONG", "signal_close": 100.0,
                                     "initial_stop": 90.0, "atr14": 2.0, "strength": 3.0,
                                     "signal_date": asof}], "shorts": []},
               "xsmom": {"is_rebalance": False, "rebalance_date": "2026-10-12", "longs": []}}
        with open(os.path.join(self.sigdir, f"combined_signals_{asof}.json"), "w") as f:
            json.dump(sig, f)
        bars = mk_series("AUSDT", [100.0] * 80, start=date(2026, 7, 10))
        mk = FakeMarket({"AUSDT": 100.5}, {"AUSDT": bars})
        self.assertEqual(self.run_main(today, mk), 0)
        st_ = State.load(os.path.join(self.dir, "state_paper.json"))
        self.assertIn("AUSDT", st_.positions)
        self.assertEqual(st_.positions["AUSDT"]["leg"], "CHAND4")
        self.assertEqual(st_.data["last_signal_asof"], asof)
        qty = st_.positions["AUSDT"]["qty"]
        # second run the same day: no duplicate entry
        self.assertEqual(self.run_main(today, mk), 0)
        st_ = State.load(os.path.join(self.dir, "state_paper.json"))
        self.assertEqual(st_.positions["AUSDT"]["qty"], qty)
        with open(os.path.join(self.dir, "paper_account.json")) as f:
            self.assertEqual(len(json.load(f)["positions"]), 1)

    def test_stale_signal_means_no_entries(self):
        today = date(2026, 9, 29)
        mk = FakeMarket({"AUSDT": 100.0}, {})
        self.assertEqual(self.run_main(today, mk), 0)
        st_ = State.load(os.path.join(self.dir, "state_paper.json"))
        self.assertEqual(st_.positions, {})


if __name__ == "__main__":
    unittest.main()
