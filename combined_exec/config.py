#!/usr/bin/env python3
"""combined_exec/config.py — config for the combined-strategy sub-account.

Loads from ~/.hermes/.env (setdefault, never clobber). Keys are prefixed
COMBINED_* so they never collide with the DMA engine's BYBIT_* keys, and the
new sub-account is explicitly separated.
"""
import os


def load_env():
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _f(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _b(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def combined_config():
    """Full config dict for the combined executor. Fail-closed on mainnet."""
    load_env()
    testnet = _b("COMBINED_TESTNET", True)
    base_url = os.environ.get(
        "COMBINED_BASE_URL", "https://api-testnet.bybit.com").rstrip("/")
    auto_trade = _b("COMBINED_AUTO_TRADE", False)
    is_mainnet = (not testnet) and ("testnet" not in (base_url or "").lower())

    # Fail-closed: mainnet requires AUTO_TRADE + a mainnet URL + explicit key.
    api_key = os.environ.get("COMBINED_API_KEY", "")
    api_secret = os.environ.get("COMBINED_API_SECRET", "")
    if is_mainnet and (not auto_trade or not api_key or not api_secret):
        raise RuntimeError(
            "COMBINED_TESTNET=false (mainnet) requires COMBINED_AUTO_TRADE=true "
            "AND COMBINED_API_KEY/COMBINED_API_SECRET set. Refusing to trade "
            "mainnet in paper mode or without the new sub-account keys."
        )

    # Signal JSONs come through the git-bus data repo on the VPS.
    default_signals = "/opt/bybit-execution-data"
    signals_dir = os.environ.get("COMBINED_SIGNALS_DIR", default_signals)

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "base_url": base_url,
        "testnet": testnet,
        "auto_trade": auto_trade,
        "is_mainnet": is_mainnet,
        "risk_pct": _f("COMBINED_RISK_PCT", 0.005),
        "cap_daily": _f("COMBINED_CAP_DAILY", 0.90),
        "max_positions": _i("COMBINED_MAX_POSITIONS", 12),
        "max_long_positions": _i("COMBINED_MAX_LONG_POSITIONS", 10),
        "global_max_lev": _i("COMBINED_GLOBAL_MAX_LEV", 5),
        "mmr": _f("COMBINED_MMR", 0.005),
        "liq_safety": _f("COMBINED_LIQ_SAFETY", 1.5),
        "stale_pct": _f("COMBINED_STALE_PCT", 5.0),
        "recv_window": _i("COMBINED_RECV_WINDOW", 5000),
        "signals_dir": signals_dir,
        "chand4_top_n": _i("COMBINED_CHAND4_TOP_N", 5),
        "xsmom_top_n": _i("COMBINED_XSMOM_TOP_N", 5),
        "chandelier_mult": _f("COMBINED_CHANDELIER_MULT", 4.0),
        "xsmom_stop_pct": _f("COMBINED_XSMOM_STOP_PCT", 0.15),
        # Min ATR-multiple the chandelier stop can be above entry (long) —
        # prevents the trail from collapsing below the entry on low-vol bars.
        "chandelier_min_atr": _f("COMBINED_CHANDELIER_MIN_ATR", 2.0),
    }
