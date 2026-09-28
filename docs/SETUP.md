# Setup & Runbook — 20dXSMomPortfolio on the Bybit VPS

This is the first-run + mainnet-cutover runbook. It follows the same deploy
patterns proven by the DMA engine (`jahrfm/bybit-execution-engine`) — deploy
keys, `ssh.github.com:443` aliases, git-bus data handoff, fail-closed mainnet.

**Boxes**
- **Hermes control box** (this one): generates signals, pushes to the data repo.
- **Bybit execution VPS** (`38.54.14.248`, hostname `kzryhcdl`): places orders.
  Hermes cannot SSH to it — deploy = push to GitHub, VPS pulls via cron.

---

## 1. One-time: create the Bybit sub-account + API keys

1. Bybit → Sub-accounts → create **a new sub-account** for this strategy
   (e.g. `combined`). Transfer the amount you want to deploy.
2. In that sub-account, create **API keys** with:
   - Permission: **Contract trade** (position creation) + **Read** (positions).
   - No withdrawal permission. No transfer permission (or as you prefer).
   - IP whitelist: the **VPS public IP** only (38.54.14.248).
3. Keep the key + secret; you will paste them into the VPS `.env`.

---

## 2. One-time: VPS deploy keys + SSH aliases

On the VPS, create a deploy key for this repo and (if not already there) for
the data repo. Port 22 to GitHub is blocked on the VPS, so use the
`ssh.github.com:443` trick with per-repo aliases.

```bash
# on the VPS, as the user that runs cron (root):
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_combined -N "" -C "combined-vps"
# add public key to GitHub → jahrfm/20dXSMomPortfolio → Deploy keys (read-only)

# ~/.ssh/config additions (per-repo aliases — a single shared alias offers no key)
Host github.com-combined
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile ~/.ssh/id_ed25519_combined

# data repo key (if not already set up by the DMA engine)
Host github.com-data
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile ~/.ssh/id_ed25519_data

# test
ssh -T github.com-combined   # → "Hi jahrfm/20dXSMomPortfolio!"
ssh -T github.com-data       # → "Hi jahrfm/bybit-execution-data!"
```

If the DMA engine already set up `github.com-data`, reuse that alias — do not
create a second one with the same name.

---

## 3. One-time: VPS bootstrap

```bash
sudo mkdir -p /opt/20dxsmomportfolio
sudo chown $(whoami) /opt/20dxsmomportfolio
git clone git@github.com-combined:jahrfm/20dXSMomPortfolio.git /opt/20dxsmomportfolio

# data repo (may already exist from the DMA engine at /opt/bybit-execution-data)
sudo mkdir -p /opt/bybit-execution-data && sudo chown $(whoami) /opt/bybit-execution-data
git clone git@github.com-data:jahrfm/bybit-execution-data.git /opt/bybit-execution-data

# first update (clones/pulls, installs crontab, tries to run)
/opt/20dxsmomportfolio/deploy/update_vps.sh
```

Verify:
```bash
crontab -l   # or: cat /etc/cron.d/20dxsmomportfolio
tail -20 /opt/20dxsmomportfolio/update.log
```

---

## 4. First run: paper mode (default; no keys needed)

Paper mode keeps a simulated account (`~/.combined_exec/paper_account.json`)
on **real mainnet prices**: entries fill at the mark ± slippage with taker
fees, stops are settled from daily bars and the live mark. This is the
forward test. The hourly cron runs it automatically once installed.

```bash
cd /opt/20dxsmomportfolio
ls /opt/bybit-execution-data/combined/            # signals from Hermes
python3 -m combined_exec.run --dry-plan           # what it would do now; touches nothing
python3 scripts/run_combined_execution.py         # a real paper run (what cron does)
python3 -m combined_exec.report                   # paper book + per-leg P&L
tail -50 /opt/20dxsmomportfolio/logs/combined_execution.log
```

`COMBINED_TESTNET=false` is allowed in paper mode (it only selects the order
endpoint, which paper never calls). Let it run for a few weeks and compare
the paper trades with the backtest's behaviour before going live.

**Hermes side:** the signal cron must run `scripts/run_combined_signal.py`
from **this repo** (it now calls this repo's `combined_signal.py`, not a copy
in bybit-execution-engine), after 00:00 UTC. Signal files are schema 2; the
executor refuses older files.

---

## 5. Go live

1. Add the sub-account block to `/root/.hermes/.env`:

```ini
COMBINED_API_KEY=...
COMBINED_API_SECRET=...
COMBINED_TESTNET=true          # smoke-test on testnet first
COMBINED_AUTO_TRADE=true
# COMBINED_RISK_PCT=0.005      # 0.5% of equity per trade (backtest default)
# COMBINED_XSMOM_ENABLED=false # backtest: no edge; leave off
```

2. `python3 scripts/verify_combined_api.py` → all checks pass.
3. Testnet: let one signal execute; confirm positions, attached stops and
   leverage in the Bybit testnet UI and `python3 -m combined_exec.report`.
4. Mainnet: `COMBINED_TESTNET=false`, keep `COMBINED_AUTO_TRADE=true`.
   The live state file is `state_live.json` — separate from paper.

---

## 6. Verification

- **Hermes:** `combined_signals_<yesterday UTC>.json` with `"schema": 2`
  pushed to bybit-execution-data/combined/.
- **VPS log:** one `=== combined_exec ...` block per hour; entries (`[OPEN]`)
  only in the first run after a new signal; later runs say `manage-only`.
- **Bybit:** each position has a stop-loss attached; CHAND4 stops move up
  (longs) / down (shorts) as the chandelier trails.
- **Report:** `python3 -m combined_exec.report` attributes P&L by leg from
  the state file.

---

## 7. Safety rails (enforced in code)

1. **Paper by default**. Live needs `COMBINED_AUTO_TRADE=true` + keys.
2. **Endpoint sanity**. `COMBINED_TESTNET` and `COMBINED_BASE_URL` must agree.
3. **Fresh signals only**. Entries only from yesterday's completed UTC bar.
4. **Once per signal**. State `last_signal_asof`, deterministic
   orderLinkIds, and a run lock.
5. **Risk-% sizing** with per-position notional cap, total margin cap,
   per-leg/side caps, max positions, stop-derived leverage (set on Bybit
   before every entry).
6. **Stops live on the exchange** from the moment of entry (attached to the
   market order), re-asserted every hour, trailed only in the safe direction.
7. **Unmanaged positions are never touched**, but count against caps.
8. **Legacy cleanup**: resting `CH4-`/`XSM-` orders from the v1 executor
   are cancelled on the first live run.

---

## 8. Rollback / pause

- Stop new entries, keep managing: `COMBINED_AUTO_TRADE=false` is **not** a
  pause for a live book (it switches to the paper account). To pause live
  entries while keeping stops managed, set `COMBINED_MAX_POSITIONS=0`.
- Full stop: remove the executor line from `deploy/cron/combined.crontab`
  in the repo (update_vps.sh reinstalls whatever the repo has). Exchange
  stops stay in place on open positions.
- Emergency: close positions in the Bybit app; the next run records them as
  `CLOSED-ON-EXCHANGE`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no signal at .../combined_signals_<date>.json - manage-only` | Hermes hasn't pushed yesterday's signal yet (or failed). Check the Hermes cron; the next hourly run will pick it up. |
| `not a schema-2 signal` | Hermes is running an old `combined_signal.py`. Point its cron at this repo's `scripts/run_combined_signal.py`. |
| `COMBINED_TESTNET=... disagrees with COMBINED_BASE_URL` | Fix the pair in `.env` (or remove `COMBINED_BASE_URL`). |
| `[UNMANAGED] SYM ...` | A position on the sub-account the executor didn't open. It's left alone; close it manually if unintended. |
| `[SKIP] ... stale` | Price moved >5% from the signal close before the run; by design. |
| `[10003] API key is invalid` | Testnet key against mainnet (or vice versa). |
| `git@github.com-combined: ... Permission denied` | Deploy key not authorized, or alias missing from `~/.ssh/config`. |
