#!/usr/bin/env python3
"""Tests for combined_exec (no network — logic only).

Run:  cd bybit-execution-engine && python3 -m combined_exec.tests.test_logic
"""
import os
import sys
import json
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from combined_exec.config import combined_config
from combined_exec.run import (
    compute_chand4_stop,
    chandelier_stop_from_highs,
    CombinedExecutor,
)


class TestChand4Stop(unittest.TestCase):
    def test_long_stop_below_entry(self):
        stop = compute_chand4_stop("BTCUSDT", 100.0, "2026-09-09", atr14=2.0,
                                   chandelier_mult=4.0, min_atr_mult=2.0, side="LONG")
        self.assertAlmostEqual(stop, 92.0)  # 100 - 4*2

    def test_short_stop_above_entry(self):
        stop = compute_chand4_stop("BTCUSDT", 100.0, "2026-09-09", atr14=2.0,
                                   chandelier_mult=4.0, min_atr_mult=2.0, side="SHORT")
        self.assertAlmostEqual(stop, 108.0)  # 100 + 4*2

    def test_chandelier_from_highs(self):
        stop = chandelier_stop_from_highs(110.0, 2.0, 4.0, "LONG")
        self.assertAlmostEqual(stop, 102.0)  # 110 - 4*2
        stop = chandelier_stop_from_highs(90.0, 2.0, 4.0, "SHORT")
        self.assertAlmostEqual(stop, 98.0)  # 90 + 4*2


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        os.environ.pop("COMBINED_API_KEY", None)
        os.environ.pop("COMBINED_TESTNET", None)
        cfg = combined_config()
        self.assertTrue(cfg["testnet"])
        self.assertFalse(cfg["auto_trade"])
        self.assertAlmostEqual(cfg["risk_pct"], 0.005)
        self.assertAlmostEqual(cfg["chandelier_mult"], 4.0)
        self.assertAlmostEqual(cfg["xsmom_stop_pct"], 0.15)

    def test_mainnet_fail_closed(self):
        os.environ["COMBINED_TESTNET"] = "false"
        os.environ["COMBINED_BASE_URL"] = "https://api.bybit.com"
        os.environ.pop("COMBINED_API_KEY", None)
        os.environ.pop("COMBINED_AUTO_TRADE", None)
        with self.assertRaises(RuntimeError):
            combined_config()
        os.environ.pop("COMBINED_TESTNET", None)
        os.environ.pop("COMBINED_BASE_URL", None)


class TestSignalParsing(unittest.TestCase):
    def test_parse_signal_file(self):
        # minimal signal file shape used by run.py
        sig = {
            "date": "2026-09-09",
            "chand4": {
                "candidates": [
                    {"symbol": "BTCUSDT", "direction": "LONG", "strength": 2.5,
                     "entry_guess": 100.0, "atr14": 2.0},
                ],
                "longs": [{"symbol": "BTCUSDT"}],
                "shorts": [],
                "asof": "2026-09-08",
            },
            "xsmom": {
                "longs": [{"symbol": "ETHUSDT", "close": 3000.0}],
                "shorts": [],
                "rebalance_date": "2026-09-14",
                "is_rebalance": True,
            },
        }
        ch = sig["chand4"]
        self.assertEqual(len(ch["candidates"]), 1)
        self.assertEqual(ch["candidates"][0]["direction"], "LONG")
        xs = sig["xsmom"]
        self.assertTrue(xs["is_rebalance"])


if __name__ == "__main__":
    unittest.main()
