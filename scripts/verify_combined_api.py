#!/usr/bin/env python3
"""verify_combined_api.py — verify the COMBINED_* API key works on the VPS.

Runs three checks, all read-only (NO orders are placed):
  1. Key permission check  — GET /v5/user/query-api-key (shows whether the key
     has ContractTrade order permission, its IP whitelist, and expiry).
  2. Wallet balance        — signed GET, confirms auth + read on the sub-account.
  3. Open positions        — confirms position read works (expect [] on a new sub).

Exit code 0 = all checks pass, 1 = any check failed.

Usage (on the VPS):
  cd /opt/20dxsmomportfolio && \
  python3 scripts/verify_combined_api.py

Reads the same COMBINED_* env from ~/.hermes/.env as the executor.
"""
import json
import os
import sys
import urllib.request

# repo root + DMA engine (bybit_exec client) on PYTHONPATH
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [
    REPO_ROOT,
    os.environ.get("DMA_ENGINE_DIR", ""),
    "/opt/bybit-execution-engine",          # VPS layout
    "/home/jose/workspace/bybit-execution-engine",  # Hermes box layout
]
for d in _CANDIDATES:
    if d and d not in sys.path and os.path.isdir(d):
        sys.path.insert(0, d)

from combined_exec.config import combined_config  # noqa: E402
from bybit_exec.bybit_client import BybitClient  # noqa: E402


def main():
    cfg = combined_config()
    print("mode        :", "MAINNET" if cfg["is_mainnet"] else
          ("TESTNET" if cfg["testnet"] else "mainnet-url-without-testnet"))
    print("base_url    :", cfg["base_url"])
    print("auto_trade  :", cfg["auto_trade"])
    print("api_key     :", (cfg["api_key"][:6] + "…" + cfg["api_key"][-4:]) if cfg["api_key"] else "(NOT SET)")
    print("api_secret  :", "SET" if cfg["api_secret"] else "(NOT SET)")
    if cfg["is_mainnet"] and not (cfg["auto_trade"] and cfg["api_key"] and cfg["api_secret"]):
        print("FAIL: mainnet requires COMBINED_AUTO_TRADE=true AND keys — config is intentionally "
              "fail-closed, executor would refuse to run.")
        return 1
    if not cfg["api_key"] or not cfg["api_secret"]:
        print("FAIL: COMBINED_API_KEY / COMBINED_API_SECRET not set in ~/.hermes/.env")
        return 1

    client = BybitClient(cfg)
    ok = True

    # --- 1. key permissions (read-only signed endpoint) ---
    try:
        res = client._signed_get("/v5/user/query-api-key", {})
        data = res.get("result", {})
        perms = data.get("permissions", {})
        contract = perms.get("ContractTrade", [])
        print("\n[1] API key permissions:")
        for k, v in sorted(perms.items()):
            print(f"      {k:15s}: {','.join(v) if v else '-'}")
        print(f"      key type  : {data.get('type', '?')}")
        print(f"      ip        : {data.get('ip', '(unset = any IP)')}")
        print(f"      expired   : {data.get('expired', '?')}")
        print(f"      created   : {data.get('createdAt', '?')}")
        print(f"      lastUsed  : {data.get('lastUsedAt', '?')}")
        if "Order" in contract:
            print("      => CONTRACT-TRADE ORDER PERMISSION: YES ✓")
        else:
            print("      => CONTRACT-TRADE ORDER PERMISSION: NO ✗ (key cannot place contract orders)")
            ok = False
        if perms.get("Withdraw"):
            print("      WARNING: key has Withdraw permission — consider removing it")
    except Exception as e:
        print(f"[1] FAIL: query-api-key error: {e}")
        ok = False

    # --- 2. wallet balance ---
    try:
        # wallet_balance() already returns the unwrapped {totalEquity, ...} dict
        bal = client.wallet_balance(account_type="UNIFIED")
        total = bal.get("totalEquity", "?")
        print(f"\n[2] wallet balance (UNIFIED): {total} USDT")
        if total in (None, "", "?"):
            ok = False
    except Exception as e:
        print(f"[2] FAIL: wallet_balance error: {e}")
        ok = False

    # --- 3. open positions ---
    try:
        # positions() already returns the parsed list
        lst = client.positions()
        print(f"[3] open positions: {len(lst)}")
        for p in lst:
            print(f"      {p.get('symbol')} {p.get('side')} size={p.get('size')}")
    except Exception as e:
        print(f"[3] FAIL: positions error: {e}")
        ok = False

    print("\nRESULT:", "ALL CHECKS PASSED ✓ — API authenticated, read works, "
          "contract-trade permission confirmed." if ok else "CHECKS FAILED — see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
