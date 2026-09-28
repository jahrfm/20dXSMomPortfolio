#!/usr/bin/env python3
"""combined_exec/config.py — config for the combined-strategy sub-account.

Loads KEY=VALUE lines from $COMBINED_ENV_FILE (default ~/.hermes/.env) with
setdefault (never clobbers the real environment). Keys are prefixed
COMBINED_* so they never collide with the DMA engine's BYBIT_* keys.

Modes
-----
  COMBINED_AUTO_TRADE=false (default)  PAPER: reads public market data, keeps
                                       a simulated account in the state dir,
                                       posts NOTHING. Works against mainnet
                                       prices — this is the forward test.
  COMBINED_AUTO_TRADE=true             LIVE: real orders on the sub-account.
                                       Requires COMBINED_API_KEY/SECRET.

COMBINED_TESTNET (default true) picks the order endpoint; COMBINED_BASE_URL
may override it but must agree with COMBINED_TESTNET (fail-closed).

Only risk/sizing knobs can be overridden from the environment. The signal
rules (lookbacks, stop windows, multipliers) are fixed in strategy.DEFAULTS
so the Hermes signal box and the VPS can never silently disagree.
"""
import os

from .strategy import DEFAULTS

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"

# env name -> (param key, parser)
_OVERRIDABLE = {
    "COMBINED_RISK_PCT": ("risk_pct", float),
    "COMBINED_CHAND4_RISK_PCT": ("chand4_risk_pct", float),
    "COMBINED_XSMOM_RISK_PCT": ("xsmom_risk_pct", float),
    "COMBINED_MAX_POSITIONS": ("max_positions", int),
    "COMBINED_CAP_DAILY": ("cap_daily", float),
    "COMBINED_MAX_POSITION_NOTIONAL": ("max_position_notional", float),
    "COMBINED_GLOBAL_MAX_LEV": ("global_max_lev", int),
    "COMBINED_MMR": ("mmr", float),
    "COMBINED_LIQ_SAFETY": ("liq_safety", float),
    "COMBINED_STALE_PCT": ("stale_pct", float),
    "COMBINED_CHAND4_TOP_N": ("chand4_top_n", int),
    "COMBINED_CHAND4_SHORTS": ("chand4_shorts", "bool"),
    "COMBINED_XSMOM_ENABLED": ("xsmom_enabled", "bool"),
    "COMBINED_XSMOM_TOP_N": ("xsmom_top_n", int),
}


def load_env():
    env_path = os.path.expanduser(os.environ.get("COMBINED_ENV_FILE", "~/.hermes/.env"))
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _b(name, default):
    v = os.environ.get(name)
    return default if v is None or v == "" else _bool(v)


def params_from_env():
    p = dict(DEFAULTS)
    for env, (key, parse) in _OVERRIDABLE.items():
        v = os.environ.get(env)
        if v is None or v == "":
            continue
        try:
            p[key] = _bool(v) if parse == "bool" else parse(float(v)) if parse is int else parse(v)
        except (TypeError, ValueError):
            raise RuntimeError(f"invalid value for {env}: {v!r}")
    return p


def combined_config():
    """Full config dict for the combined executor. Fail-closed."""
    load_env()
    testnet = _b("COMBINED_TESTNET", True)
    base_url = (os.environ.get("COMBINED_BASE_URL") or (TESTNET_URL if testnet else MAINNET_URL)).rstrip("/")
    url_is_testnet = "testnet" in base_url.lower()
    if url_is_testnet != testnet:
        raise RuntimeError(
            f"COMBINED_TESTNET={testnet} disagrees with COMBINED_BASE_URL={base_url}. "
            "Refusing to guess which exchange to trade on.")
    auto_trade = _b("COMBINED_AUTO_TRADE", False)
    api_key = os.environ.get("COMBINED_API_KEY", "")
    api_secret = os.environ.get("COMBINED_API_SECRET", "")
    if auto_trade and (not api_key or not api_secret):
        raise RuntimeError(
            "COMBINED_AUTO_TRADE=true requires COMBINED_API_KEY and COMBINED_API_SECRET "
            "(the dedicated sub-account keys).")

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "base_url": base_url,
        "testnet": testnet,
        "auto_trade": auto_trade,
        "is_mainnet": not testnet,
        "recv_window": int(float(os.environ.get("COMBINED_RECV_WINDOW", 5000))),
        # market data always comes from MAINNET (testnet prices are not real)
        "data_url": os.environ.get("COMBINED_DATA_URL", MAINNET_URL).rstrip("/"),
        "signals_dir": os.environ.get("COMBINED_SIGNALS_DIR", "/opt/bybit-execution-data/combined"),
        "state_dir": os.path.expanduser(os.environ.get("COMBINED_STATE_DIR", "~/.combined_exec")),
        "paper_equity": float(os.environ.get("COMBINED_PAPER_EQUITY", 10000)),
        "params": params_from_env(),
    }
