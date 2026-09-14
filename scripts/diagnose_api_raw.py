#!/usr/bin/env python3
"""diagnose_api_raw.py — raw HTTP diagnostic for api.bybit.com from the VPS.

Shows exactly what comes back from Bybit WITHOUT the client's parsing so we
can see whether a proxy / firewall / wrong URL shape is mangling responses.
Prints:
  - proxy env vars (urllib honors HTTP(S)_PROXY — a common silent-breakage)
  - public endpoint (no auth) raw status + body
  - signed endpoint raw status + body (query-api-key)
No orders, read-only. Usage:
  cd /opt/20dxsmomportfolio && python3 scripts/diagnose_api_raw.py
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [REPO_ROOT, "/opt/bybit-execution-engine", "/home/jose/workspace/bybit-execution-engine"]
for d in _CANDIDATES:
    if d and d not in sys.path and os.path.isdir(d):
        sys.path.insert(0, d)

from combined_exec.config import combined_config  # noqa: E402


def raw_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return None, f"EXC: {type(e).__name__}: {e}"


def main():
    cfg = combined_config()

    print("=== 0. proxy / network env (urllib honors these!) ===")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        v = os.environ.get(k)
        if v:
            print(f"  {k}={v}")
    if not any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")):
        print("  (no proxy env vars set)")

    print("\n=== 1. public endpoint (no auth): /v5/market/time ===")
    status, body = raw_get("https://api.bybit.com/v5/market/time",
                           {"User-Agent": "hermes-agent/bybit-exec"})
    print(f"  status: {status}")
    print(f"  body  : {body[:400]}")
    try:
        d = json.loads(body)
        print(f"  parsed retCode: {d.get('retCode')} retMsg: {d.get('retMsg')}")
    except Exception as e:
        print(f"  NOT VALID JSON: {e}")

    print("\n=== 2. signed endpoint: /v5/user/query-api-key ===")
    if not (cfg.get("api_key") and cfg.get("api_secret")):
        print("  (no COMBINED_API_KEY/SECRET set — nothing to sign with)")
        return 1
    # Replicate BybitClient._signed_get EXACTLY (same sign string + headers),
    # but print the raw body before any parsing so we see what the API sent.
    from bybit_exec.bybit_client import sign, BybitClient  # noqa: E402
    ts = int(time.time() * 1000)
    qs = ""  # query-api-key takes no params; _signed_get joins {} -> ""
    sig = sign(ts, cfg["api_key"], cfg.get("recv_window", 5000), qs, cfg["api_secret"])
    url = f"{cfg['base_url']}/v5/user/query-api-key" + (f"?{qs}" if qs else "")
    h = {
        "X-BAPI-API-KEY": cfg["api_key"],
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-SIGN": sig,
        "X-BAPI-RECV-WINDOW": str(cfg.get("recv_window", 5000)),
        "User-Agent": "hermes-agent/bybit-exec",
    }
    status, body = raw_get(url, h)
    print(f"  url    : {cfg['base_url']}/v5/user/query-api-key")
    print(f"  status : {status}")
    print(f"  body   : {body[:600]}")
    try:
        d = json.loads(body)
        print(f"  parsed retCode: {d.get('retCode')} retMsg: {d.get('retMsg')}")
        if isinstance(d, dict) and d.get("result"):
            perms = d["result"].get("permissions", {})
            print(f"  ContractTrade: {perms.get('ContractTrade')}")
            print(f"  Withdraw     : {perms.get('Withdraw')}")
            print(f"  ip           : {d['result'].get('ip')}")
    except Exception as e:
        print(f"  NOT VALID JSON (or array): {e}")

    print("\n=== 3. connection check without proxy (--noproxy equivalent) ===")
    import urllib.request as ur
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    status, body = raw_get("https://api.bybit.com/v5/market/time",
                           {"User-Agent": "hermes-agent/bybit-exec"})
    print(f"  status: {status}")
    print(f"  body  : {body[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
