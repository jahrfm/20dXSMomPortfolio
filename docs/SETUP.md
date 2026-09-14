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

## 4. First run: paper / dry-run (no keys needed to inspect signals)

```bash
# list what signals the Hermes box pushed
ls /opt/bybit-execution-data/combined/

# dry-run against a specific signal — prints intended orders, posts nothing
cd /opt/20dxsmomportfolio
python3 -m combined_exec.run --dry-run --date 2026-09-13
```

Expected output: `[DRY] would place ...` lines for each candidate, `combined_exec
run complete`. If `COMBINED_AUTO_TRADE` is false (default), the cron runs stay
paper even without `--dry-run`.

---

## 5. Add the keys + go live

1. On the VPS, the combined executor reads **`~/.hermes/.env`** (as root:
   `/root/.hermes/.env` — **the same file the DMA engine reads**, not a
   per-repo `.env`). Add a block for the new sub-account:

```ini
# NEW sub-account for the combined strategy
COMBINED_API_KEY=...
COMBINED_API_SECRET=...
COMBINED_TESTNET=false
COMBINED_BASE_URL=https://api.bybit.com
COMBINED_AUTO_TRADE=true
# optional sizing knobs (defaults are sensible; see README)
# COMBINED_RISK_PCT=0.005
```

2. **Paper-review a few days first** (`COMBINED_AUTO_TRADE=false`, cron runs
   paper, `report.py` shows the intended book). Only when the book looks right,
   flip `COMBINED_AUTO_TRADE=true`.

3. Restart/re-run:
```bash
cd /opt/20dxsmomportfolio && python3 -m combined_exec.report --days 7
# and let the 15-min update + daily cron take over
```

---

## 6. Verification

- **Signal side (Hermes):** `hypertracker-data/combined_signals_<date>.json`
  exists and was pushed (check `git log --oneline -1` in bybit-execution-data).
- **Data side (VPS):** `/opt/bybit-execution-data/combined/` has the dated file.
- **Execution side (VPS):** `/opt/20dxsmomportfolio/logs/combined_execution.log`
  shows `[ORDER]` (live) or `[DRY]` (paper) lines; `report.py` shows positions.
- **Bybit app:** positions appear on the `combined` sub-account with orderLinkId
  prefixes `CH4-` (CHAND4) and `XSM-` (XSMOM).

---

## 7. Safety rails (enforced in code)

1. **Paper by default** — `COMBINED_AUTO_TRADE` unset/false → no orders posted.
2. **Mainnet fail-closed** — mainnet (testnet=false) requires AUTO_TRADE=true +
   keys; otherwise raises before any API call.
3. **Risk-% sizing** — every trade risks `COMBINED_RISK_PCT` of balance; stop
   derived leverage keeps liquidation at/beyond the stop.
4. **Staleness guard** — entries >5% past live mark are skipped.
5. **Never lower a chandelier stop** — trailing only ratchets up (longs) / down
   (shorts).
6. **Dedicated sub-account** — the combined book never touches the DMA TOP /
   SCREENED wallets; no cross-contamination of risk or keys.
7. **Idempotency marker** — `combined_done_<date>.ok` prevents duplicate daily
   runs.

---

## 8. Rollback / pause

- Pause trading: set `COMBINED_AUTO_TRADE=false` (paper) or remove the
  crontab drop-in `rm /etc/cron.d/20dxsmomportfolio` (update_vps.sh will
  reinstall it on the next tick — also remove from the repo if permanent).
- Emergency: cancel open orders / close positions via the Bybit app or
  `report.py`-driven actions on the sub-account.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FATAL: no combined signals at ...` | Hermes hasn't pushed a signal for that date; check the Hermes cron + data repo. |
| `BybitAPIError: [None] None` on positions | No/incorrect keys in `.env` — add `COMBINED_API_KEY/SECRET` (or it's a transient blip, retry). |
| `[10003] API key is invalid` | Testnet key against mainnet URL (or vice versa) — check `COMBINED_TESTNET` + `COMBINED_BASE_URL` match. |
| No `CH4-`/`XSM-` positions but `[DRY]` lines | You're in paper mode — set `COMBINED_AUTO_TRADE=true`. |
| `git@github.com-combined: ... Permission denied` | Deploy key not authorized on that repo, or alias not in `~/.ssh/config`. |
