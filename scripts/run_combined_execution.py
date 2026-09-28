#!/usr/bin/env python3
"""run_combined_execution.py — VPS cron wrapper (runs hourly).

1. git pull the data repo so the latest Hermes signal is present.
2. run the executor (python -m combined_exec.run).

Idempotency lives in the executor's state file: entries for a given signal
happen once; every other hourly run only reconciles positions and trails /
re-asserts stops. So it is safe (and intended) to run this every hour — the
first run after the Hermes push trades, the rest manage.

Env:
  HANDOFF_REPO          data repo checkout (default /opt/bybit-execution-data)
  COMBINED_SIGNALS_DIR  default $HANDOFF_REPO/combined
  DMA_ENGINE_DIR        bybit_exec location (default /opt/bybit-execution-engine)
"""
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDOFF_REPO = os.environ.get("HANDOFF_REPO", "/opt/bybit-execution-data")
SIGNALS_DIR = os.environ.get("COMBINED_SIGNALS_DIR", os.path.join(HANDOFF_REPO, "combined"))
DMA_ENGINE_DIR = os.environ.get("DMA_ENGINE_DIR", "/opt/bybit-execution-engine")


def pull_signals():
    if not os.path.isdir(os.path.join(HANDOFF_REPO, ".git")):
        print(f"[combined] no data repo at {HANDOFF_REPO}; using local signals")
        return
    for attempt in range(3):
        r = subprocess.run(["git", "-C", HANDOFF_REPO, "pull", "--ff-only", "-q"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return
        print(f"[combined] pull attempt {attempt + 1} failed: {r.stderr.strip()}")
        time.sleep(10)
    print("[combined] WARNING: signal pull failed; using what's local")


def main():
    pull_signals()
    env = dict(os.environ, COMBINED_SIGNALS_DIR=SIGNALS_DIR)
    extra = os.pathsep.join(d for d in (REPO, DMA_ENGINE_DIR) if os.path.isdir(d))
    env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-m", "combined_exec.run", *sys.argv[1:]],
                       cwd=REPO, env=env)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
