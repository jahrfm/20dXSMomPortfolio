# 20dXSMomPortfolio — Combined CHAND4 + XSMOM Automated Execution

Automated execution of the **combined portfolio** on Bybit: the 20d breakout
strategy (CHAND4 40/15) running alongside the XSMOM cross-sectional momentum
strategy, on a **dedicated Bybit sub-account**, executed from the Bybit VPS.

**Signal-only research → automated execution.** This repo contains the
execution engine + deployment; the strategy research/backtests live in
[`jahrfm/20d`](https://github.com/jahrfm/20d) (CHAND4 40/15) and
[`jahrfm/MOMSXperp`](https://github.com/jahrfm/MOMSXperp) (XSMOM). The
combined backtest that justifies running both together (period-P&L correlation
≈ −0.07, combined Sharpe 1.48 > either alone) is in the 20d repo:
`run_combined_backtest.py` → `combined_results.json`.

---

## How it works

```
┌─ Hermes box ───────────────────────────────────────────────┐
│  combined_signal.py (daily, via cron)                      │
│    • CHAND4 40/15: scan top-50 Bybit perps for new 40-day  │
│      highs/lows, rank by (close−SMA40)/ATR14, top-5/side   │
│    • XSMOM: biweekly top-5 longs (30d VNR + MA100 filter), │
│      15% hard stop, 14-day hold                            │
│    → combined_signals_<date>.json                          │
│  run_combined_signal.py → handoff_sync --push-combined     │
│    → jahrfm/bybit-execution-data/combined/                 │
└────────────────────────────────────────────────────────────┘
                          │ git-bus (GitHub)
                          ▼
┌─ Bybit execution VPS (38.54.14.248) ───────────────────────┐
│  update_vps.sh (every 15 min):                             │
│    • pull code repo (this repo) → /opt/20dxsmomportfolio   │
│    • pull data repo → /opt/bybit-execution-data            │
│    • install /etc/cron.d/20dxsmomportfolio                  │
│    • run executor if today's signal is present             │
│  run_combined_execution.py (daily 08:00 +07 + 16:00 +07):  │
│    combined_exec.run --date <signal-date>                  │
│    • CHAND4: place new breakouts (limit at next open,      │
│      4×ATR chandelier stop), trail stops up daily          │
│    • XSMOM: on rebalance days, close held legs, enter      │
│      new top-5 (15% stop)                                  │
│    • sizing: risk-% of balance via bybit_exec sizing stack │
└────────────────────────────────────────────────────────────┘
```

Both legs share one sub-account. Risk is split 50/50 by sizing each leg with
the same risk budget (`COMBINED_RISK_PCT`, default 0.5% of balance per trade —
half the DMA engine's 1% so the combined book risks ~1% per period when both
legs are active).

---

## Repos & roles

| Repo | Role |
|---|---|
| **jahrfm/20dXSMomPortfolio** (this) | Combined executor code + deployment + docs |
| jahrfm/20d | CHAND4 40/15 research + backtest engine (signal math source) |
| jahrfm/MOMSXperp | XSMOM research + live signal module (`signal.py`) |
| jahrfm/bybit-execution-data | git-bus data repo: `combined/` (Hermes → VPS signals) |
| jahrfm/bybit-execution-engine | DMA engine (provides `bybit_exec` client + sizing, already on the VPS) |

---

## Layout

```
combined_signal.py              Hermes-side: generate the daily order book
combined_exec/
  __init__.py
  config.py                     COMBINED_* env knobs (fail-closed on mainnet)
  run.py                        VPS executor: entries, trails, XSMOM rebalance
  report.py                     positions + per-leg PnL report
  tests/test_logic.py           unit tests (no network)
scripts/
  run_combined_signal.py        Hermes cron wrapper (generate + push via git-bus)
  run_combined_execution.py     VPS cron wrapper (pull signals + run executor)
deploy/
  update_vps.sh                 VPS self-update (code+data+cron+run)
  cron/combined.crontab         /etc/cron.d drop-in (self-update, daily, afternoon)
docs/
  SETUP.md                      VPS + keys + first-run runbook
```

---

## Configuration (`~/.hermes/.env` on Hermes, `/opt/bybit-execution/.env` on VPS)

The combined executor reads `COMBINED_*` keys (never collides with the DMA
engine's `BYBIT_*` keys):

| Key | Default | Meaning |
|---|---|---|
| `COMBINED_API_KEY` / `COMBINED_API_SECRET` | — | **New sub-account keys** (you provide) |
| `COMBINED_TESTNET` | `true` | true = testnet; false = mainnet |
| `COMBINED_BASE_URL` | testnet | `https://api.bybit.com` for mainnet |
| `COMBINED_AUTO_TRADE` | `false` | false = paper/dry-run; true = place orders |
| `COMBINED_RISK_PCT` | `0.005` | risk-% of balance per trade (0.5%) |
| `COMBINED_CAP_DAILY` | `0.90` | max deployed margin fraction |
| `COMBINED_MAX_POSITIONS` | `12` | book cap (CHAND4 5 + XSMOM 5 + slack) |
| `COMBINED_MAX_LONG_POSITIONS` | `10` | long book cap |
| `COMBINED_GLOBAL_MAX_LEV` | `5` | leverage ceiling (conservative for a 2-strategy book) |
| `COMBINED_MMR` / `COMBINED_LIQ_SAFETY` | `0.005` / `1.5` | liq model |
| `COMBINED_STALE_PCT` | `5.0` | skip entries >5% past live mark |
| `COMBINED_CHAND4_TOP_N` | `5` | CHAND4 book size |
| `COMBINED_XSMOM_TOP_N` | `5` | XSMOM book size |
| `COMBINED_CHANDELIER_MULT` | `4.0` | chandelier stop = highest close − 4×ATR |
| `COMBINED_CHANDELIER_MIN_ATR` | `2.0` | trail never closer to entry than 2×ATR |
| `COMBINED_XSMOM_STOP_PCT` | `0.15` | XSMOM hard stop |
| `COMBINED_SIGNALS_DIR` | `/opt/bybit-execution-data` | where pulled signals land |

**Fail-closed:** `COMBINED_TESTNET=false` (mainnet) requires `COMBINED_AUTO_TRADE=true`
AND keys set. Paper mode never posts orders.

---

## Quick start

### Hermes box (signal side)
```bash
# generate + push today's signal (daily cron does this)
python3 scripts/run_combined_signal.py            # generates + pushes via git-bus
python3 scripts/run_combined_signal.py --no-push  # generate only, inspect locally

# unit tests
python3 -m combined_exec.tests.test_logic
```

### VPS (execution side)
```bash
# bootstrap (first time)
sudo git clone git@github.com-combined:jahrfm/20dXSMomPortfolio.git /opt/20dxsmomportfolio
sudo /opt/20dxsmomportfolio/deploy/update_vps.sh   # pulls data repo, installs cron, runs

# paper first (COMBINED_AUTO_TRADE=false — default)
cd /opt/20dxsmomportfolio && python3 -m combined_exec.run --dry-run --date <date>

# report (needs keys)
python3 -m combined_exec.report --days 7
```

See [`docs/SETUP.md`](docs/SETUP.md) for the full first-run + mainnet-cutover runbook
(deploy keys, SSH aliases, .env placement, verification steps).

---

## Status

- [x] Combined backtest (20d repo): correlation −0.07, combined Sharpe 1.48
- [x] Signal generator (Hermes) — CHAND4 + XSMOM, no-lookahead (last completed bar)
- [x] VPS executor — entries, chandelier trailing, XSMOM rebalance, sizing
- [x] git-bus handoff (`combined/` in bybit-execution-data)
- [x] VPS deploy (update_vps.sh + crontab drop-in)
- [ ] **Awaiting: new sub-account API keys** (then paper-review, then mainnet cutover)
- [ ] Live forward-test before trusting the strategy's edge

## Disclaimer

Signal-only research and automation. Backtests are not indicative of future
results. Trading cryptocurrency derivatives involves substantial risk. No
auto-trading on mainnet until explicitly enabled (`COMBINED_AUTO_TRADE=true`)
with real keys reviewed by the account owner.
