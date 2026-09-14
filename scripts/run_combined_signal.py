#!/usr/bin/env python3
"""run_combined_signal.py — Hermes-side daily combined signal generation + push.

1. Generate combined_signals_<last-completed-bar>.json via combined_signal.py
   (CHAND4 40/15 daily breakout + XSMOM biweekly momentum).
2. Push it to the git-bus data repo (jahrfm/bybit-execution-data/combined/)
   via handoff_sync.py --push-combined so the VPS executor can pull it.

Designed for cron (no session). Quiet on success, prints on failure.

Usage:
    python3 run_combined_signal.py [--date YYYY-MM-DD] [--out DIR]
"""
import argparse
import os
import subprocess
import sys

WORKSPACE = "/home/jose/workspace"
ENGINE_REPO = os.environ.get("HANDOFF_ENGINE_REPO",
                            "/home/jose/workspace/bybit-execution-engine")
DATA_REPO = os.environ.get("HANDOFF_DATA_REPO",
                           "/home/jose/workspace/bybit-execution-data")
DATA_DIR = os.environ.get("TRADING_DATA_DIR",
                          "/home/jose/workspace/hypertracker-data")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=DATA_DIR)
    ap.add_argument("--no-push", action="store_true",
                    help="generate only; do not push to the data repo")
    args = ap.parse_args()

    # 1) generate the signal
    sig_py = os.path.join(ENGINE_REPO, "combined_signal.py")
    if not os.path.exists(sig_py):
        print(f"[combined-signal] FATAL: {sig_py} not found", file=sys.stderr)
        return 1
    cmd = [sys.executable, sig_py, "--out", args.out]
    if args.date:
        cmd += ["--date", args.date]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"[combined-signal] generation FAILED:\n{r.stdout}{r.stderr}",
              file=sys.stderr)
        return 1
    print(r.stdout.strip())

    if args.no_push:
        return 0

    # 2) push via handoff_sync (hermes role)
    hs = os.path.join(ENGINE_REPO, "scripts", "handoff_sync.py")
    if not os.path.isdir(os.path.join(DATA_REPO, ".git")):
        print(f"[combined-signal] data repo missing at {DATA_REPO}; "
              f"skipping push (signal saved locally)", file=sys.stderr)
        return 0
    push_cmd = [sys.executable, hs, "--repo", DATA_REPO, "--role", "hermes",
                "--data-dir", args.out, "--push-combined"]
    if args.date:
        push_cmd += ["--date", args.date]
    r2 = subprocess.run(push_cmd, capture_output=True, text=True, timeout=180)
    if r2.returncode != 0:
        print(f"[combined-signal] push FAILED:\n{r2.stdout}{r2.stderr}",
              file=sys.stderr)
        return 2
    print(r2.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
