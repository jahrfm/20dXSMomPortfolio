#!/usr/bin/env python3
"""combined_exec — VPS-side executor for the COMBINED strategy order book.

Consumes combined_signals_<date>.json (produced on the Hermes box by
combined_signal.py and delivered via the git-bus data repo) and manages
orders/positions on the new Bybit sub-account:

  - CHAND4 leg: place new 40-day breakout entries (top-5 per side by
    strength), set a 4xATR chandelier stop as a Bybit stop-loss, and each
    subsequent run, TRAIL the stop up as the position's highest close rises.
  - XSMOM leg: on biweekly rebalance days, enter the top-5 longs (MA100 +
    raw-positive), 15% hard stop, hold until next rebalance (rebalance-day
    run closes XSMOM legs whose hold window elapsed).

Sizing reuses the bybit_exec stack (risk-% of balance, stop-derived leverage,
min-value / qty-step guards). Config via ~/.hermes/.env:
  COMBINED_API_KEY / COMBINED_API_SECRET   (new sub-account)
  COMBINED_BASE_URL   (mainnet api.bybit.com by default)
  COMBINED_TESTNET    (true/false)
  COMBINED_AUTO_TRADE (false = dry-run / paper; true = place orders)
  COMBINED_RISK_PCT   (default 0.005 = 0.5% of balance per trade — HALF the
                       DMA engine's 1% because this sub-account runs BOTH
                       strategies; 50/50 risk split is achieved by sizing
                       each leg with the same risk budget and letting the
                       cap limit overlap)
  COMBINED_CAP_DAILY / COMBINED_MAX_POSITIONS
  COMBINED_SIGNALS_DIR (where combined_signals_*.json land; default the
                       git-bus data repo dir)

Usage (on the VPS):
  python3 -m combined_exec.run --date YYYY-MM-DD [--dry-run]
  python3 -m combined_exec.report

Safety: COMBINED_AUTO_TRADE=false → computes + prints orders, posts NOTHING.
Mainnet requires COMBINED_AUTO_TRADE=true AND a mainnet base URL (fail-closed).
"""
__version__ = "0.1.0"
