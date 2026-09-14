#!/usr/bin/env bash
# update_vps.sh — self-updating deploy for the COMBINED strategy (20dXSMomPortfolio).
#
# Canonical source: jahrfm/20dXSMomPortfolio (deploy/update_vps.sh).
# Runs on the Bybit execution VPS. The Hermes box generates combined signals
# and pushes them to jahrfm/bybit-execution-data/combined/ (git-bus handoff);
# this script pulls THAT data repo so the executor has fresh signals, then
# self-updates the code repo.
#
# Cron (VPS): */15 * * * * /opt/20dxsmomportfolio/update_vps.sh >> /opt/20dxsmomportfolio/update.log 2>&1
#
# Repos:
#   CODE repo  jahrfm/20dXSMomPortfolio  -> /opt/20dxsmomportfolio  (clean consumer: reset to origin)
#   DATA repo  jahrfm/bybit-execution-data -> /opt/bybit-execution-data (pull --ff-only; used for signals + the DMA bus)
#
# SSH aliases on the VPS (port-22 blocked): github.com-combined (read, code repo),
# github.com-data (write, data repo), both via ssh.github.com:443.
set -u

CODE_DIR=${CODE_DIR:-/opt/20dxsmomportfolio}
DATA_REPO=${DATA_REPO:-/opt/bybit-execution-data}
STABLE_SCRIPT=${STABLE_SCRIPT:-/opt/20dxsmomportfolio/update_vps.sh}
LOG_DIR=/opt/20dxsmomportfolio
LOG="$LOG_DIR/update.log"
LOGS_DIR="$LOG_DIR/logs"
LOCK="$LOG_DIR/.update.lock"
BRANCH=${VPS_BRANCH:-main}

mkdir -p "$LOG_DIR" "$LOGS_DIR"

if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

log()   { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
report(){ echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }

# --- 1. code repo (clean consumer) ---
if [ ! -d "$CODE_DIR/.git" ]; then
    log "code repo missing — cloning"
    git clone git@github.com-combined:jahrfm/20dXSMomPortfolio.git "$CODE_DIR" >>"$LOG" 2>&1 \
        || { report "CODE CLONE FAILED"; exit 1; }
fi
if ! (cd "$CODE_DIR" && git fetch -q origin && git checkout -q "$BRANCH" 2>/dev/null \
      && git reset -q --hard "origin/$BRANCH"); then
    report "CODE UPDATE FAILED (branch=$BRANCH)"
    exit 1
fi
if [ ! -f "$CODE_DIR/deploy/update_vps.sh" ]; then
    report "FATAL: deploy/update_vps.sh missing after checkout on branch=$BRANCH — aborting (self-wipe guard)"
    exit 1
fi

# --- 2. data repo (signals; VPS may push rankings, never reset) ---
if [ ! -d "$DATA_REPO/.git" ]; then
    log "data repo missing — cloning"
    git clone git@github.com-data:jahrfm/bybit-execution-data.git "$DATA_REPO" >>"$LOG" 2>&1 \
        || { report "DATA CLONE FAILED"; exit 1; }
fi
if ! (cd "$DATA_REPO" && git pull -q --ff-only origin HEAD); then
    report "DATA PULL FAILED"
fi

# --- 3. re-copy self to stable location ---
if ! cmp -s "$CODE_DIR/deploy/update_vps.sh" "$STABLE_SCRIPT"; then
    cp "$CODE_DIR/deploy/update_vps.sh" "$STABLE_SCRIPT"
    chmod +x "$STABLE_SCRIPT"
    log "re-copied update_vps.sh to stable location"
fi

# --- 4. install combined crontab if changed (repo = source of truth) ---
CRON_SRC="$CODE_DIR/deploy/cron/combined.crontab"
CRON_DST=/etc/cron.d/20dxsmomportfolio
if [ -f "$CRON_SRC" ]; then
    if ! cmp -s "$CRON_SRC" "$CRON_DST"; then
        cp "$CRON_SRC" "$CRON_DST"
        chmod 644 "$CRON_DST"
        log "installed combined crontab -> $CRON_DST"
    fi
fi

# --- 5. run the executor if today's signal is present (daily pass) ---
TODAY=$(date -u +%F)
SIG="$DATA_REPO/combined/combined_signals_${TODAY}.json"
if [ -f "$SIG" ]; then
    cd "$CODE_DIR" && \
        COMBINED_SIGNALS_DIR="$DATA_REPO/combined" \
        python3 -m combined_exec.run --date "$TODAY" >> "$LOGS_DIR/combined_execution.log" 2>&1
else
    log "no signal yet for $TODAY — skipping executor (update-only)"
fi

exit 0
