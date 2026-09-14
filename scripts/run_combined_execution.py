#!/usr/bin/env python3
"""run_combined_execution.py — VPS daily wrapper for the combined strategy.

Pulls the latest combined signals from the git-bus data repo (if HANDOFF_REPO
set), then runs the executor. Mirrors run_daily_execution.py's handoff-aware
behavior: waits for/pulls signals, runs the executor, idempotency markers.

Usage (VPS):
  python3 scripts/run_combined_execution.py [--dry-run] [--max-attempts 3]

Env:
  COMBINED_AUTO_TRADE=true   -> real orders; false -> paper/dry-run
  HANDOFF_REPO               -> git-bus data repo path (pull signals first)
  COMBINED_SIGNALS_DIR       -> where signals land after pull
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import date

WORKSPACE = "/home/jose/workspace"
# This repo's root (20dXSMomPortfolio) — holds combined_exec.
ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The DMA execution engine repo provides bybit_exec (client + sizing) that
# combined_exec imports. On the VPS it lives at /opt/bybit-execution-engine.
DMA_ENGINE_DIR = os.environ.get("DMA_ENGINE_DIR", "/opt/bybit-execution-engine")
HANDOFF_REPO = os.environ.get("HANDOFF_REPO", "/opt/bybit-execution-data")
SIGNALS_DIR = os.environ.get(
    "COMBINED_SIGNALS_DIR",
    HANDOFF_REPO if os.path.isdir(HANDOFF_REPO) else os.path.join(WORKSPACE, "hypertracker-data"),
)
DONE_DIR = os.environ.get("COMBINED_DONE_DIR", "/opt/bybit-execution/logs")
PYTHONPATH_EXTRA = os.pathsep.join(
    d for d in (ENGINE_DIR, DMA_ENGINE_DIR) if os.path.isdir(d))
MAX_ATTEMPTS = 3


def pull_signals():
    """If HANDOFF_REPO exists, pull to fetch Hermes-pushed combined signals."""
    if not os.path.isdir(HANDOFF_REPO):
        print(f"[combined] no HANDOFF_REPO at {HANDOFF_REPO}, skipping pull")
        return
    for attempt in range(3):
        r = subprocess.run(["git", "-C", HANDOFF_REPO, "pull", "--ff-only", "-q"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"[combined] pulled signals from {HANDOFF_REPO}")
            return
        print(f"[combined] pull attempt {attempt + 1} failed: {r.stderr.strip()}")
        time.sleep(10)
    print("[combined] WARNING: signal pull failed after 3 attempts; using what's local")


def latest_signal_date():
    import glob
    files = sorted(glob.glob(os.path.join(SIGNALS_DIR, "combined_signals_*.json")))
    if not files:
        return None
    name = os.path.basename(files[-1])
    return name.replace("combined_signals_", "").replace(".json", "")


def run_executor(dry_run, date_str):
    cmd = [sys.executable, "-m", "combined_exec.run", "--date", date_str]
    if dry_run:
        cmd.append("--dry-run")
    env = dict(os.environ)
    env["PYTHONPATH"] = PYTHONPATH_EXTRA + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(cmd, cwd=ENGINE_DIR, env=env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = ap.parse_args()

    pull_signals()
    date_str = latest_signal_date()
    if not date_str:
        print("[combined] FATAL: no combined signals found")
        return 1
    print(f"[combined] latest signal date: {date_str}")

    marker = os.path.join(DONE_DIR, f"combined_done_{date_str}.ok")
    if os.path.exists(marker) and not args.dry_run:
        print(f"[combined] already done for {date_str} (marker exists); skipping")
        return 0

    for attempt in range(1, args.max_attempts + 1):
        print(f"[combined] attempt {attempt}/{args.max_attempts}")
        r = run_executor(args.dry_run, date_str)
        if r.returncode == 0:
            if not args.dry_run:
                os.makedirs(DONE_DIR, exist_ok=True)
                with open(marker, "w") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            print("[combined] SUCCESS")
            return 0
        if attempt < args.max_attempts:
            time.sleep(15 * attempt)
    print("[combined] CRITICAL: executor failed after all attempts")
    return 1


if __name__ == "__main__":
    sys.exit(main())
