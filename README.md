# 20dXSMomPortfolio — CHAND4 (+ optional XSMOM) automated execution

Automated execution on a **dedicated Bybit sub-account** of the CHAND4 40/15
breakout strategy (from [`jahrfm/20d`](https://github.com/jahrfm/20d)), with
the XSMOM momentum leg (from [`jahrfm/MOMSXperp`](https://github.com/jahrfm/MOMSXperp))
available behind a flag.

**One copy of the rules.** `combined_exec/strategy.py` + `planner.py` are the
only place the strategy is defined. The backtest, the Hermes signal generator
and the VPS executor all call them, so what is backtested is what is traded.

**XSMOM is OFF by default.** The joint backtest (`backtest/`) shows CHAND4
has an edge after realistic costs (Sharpe ~1.0, max DD 13% at 0.5% risk)
and XSMOM does not (Sharpe ~0.2, max DD 56%; negative without funding), and
adding XSMOM makes the combined book worse. See [`backtest/README.md`](backtest/README.md).

---

## How it works

```
┌─ Hermes box (~00:05 UTC) ──────────────────────────────────┐
│  scripts/run_combined_signal.py → combined_signal.py       │
│    public Bybit klines, forming candle dropped             │
│    strategy.build_signal(asof = yesterday UTC)             │
│    → combined_signals_<asof>.json (schema 2)               │
│    → git-bus push: jahrfm/bybit-execution-data/combined/   │
└────────────────────────────────────────────────────────────┘
                          │
┌─ Bybit VPS (hourly, :20) ──────────────────────────────────┐
│  scripts/run_combined_execution.py → combined_exec.run     │
│    lock → reconcile exchange vs state → trail/re-assert    │
│    stops → ONCE per signal: planner.plan_day() → market    │
│    entries with stop attached + leverage set               │
│  state file: which leg owns which position (Bybit's        │
│  position list has no order tags)                          │
└────────────────────────────────────────────────────────────┘
```

**Rules** (full detail in the `strategy.py` docstring):

- **CHAND4 40/15.** Signal: a new 40-day high or low on the completed daily bar, ranked by `|close − SMA40| / ATR14`. Entry: next day at market. Initial stop: the 15-day low (15-day high for shorts). Exit: a daily chandelier trail at highest-high-since-entry − 4×ATR, which only ever tightens. Book: up to 5 positions per side.
- **XSMOM (optional).** Every 14 days on Mondays (anchored 2026-08-17). Rank the universe by 30-day volatility-normalised return; take the top 5 longs that are above their MA100 with a positive 30-day return. Hard stop at −15%. Names that drop out of the top 5 are closed at the rebalance.
- **Universe.** Top 50 USDT perps by trailing 30-day turnover, point-in-time, stablecoins excluded.
- **Sizing.** Risk 0.5% of equity per trade to the stop. Max 35% of equity in any one position's notional. Leverage is derived from the stop distance (max 5x). Total margin is capped at 90% of equity, and a symbol can be held by only one leg.

**Idempotent by construction:**
- Entries happen once per signal, tracked by `last_signal_asof` in the state file.
- Order link IDs are deterministic, so a duplicate order is rejected by Bybit.
- A lock (`flock`) prevents two runs overlapping.
- Only the signal for yesterday's completed UTC bar can open positions. A missing or stale signal means manage-only.

---

## Layout

```
combined_signal.py            Hermes: build + write the daily signal book
combined_exec/
  strategy.py                 THE rules (pure functions, stdlib)
  planner.py                  exits/entries/sizing/caps + stop trailing
  market.py                   public Bybit market data (mainnet prices)
  broker.py                   BybitBroker (live) / PaperBroker (simulated)
  state.py                    leg ownership + trade history (atomic JSON)
  run.py                      VPS executor (paper or live)
  report.py                   open positions + per-leg P&L
  config.py                   COMBINED_* env (fail-closed)
  tests/test_logic.py         28 unit/integration tests (no network)
backtest/
  combined_backtest.py        joint shared-capital backtest
  README.md                   method + results
scripts/
  run_combined_signal.py      Hermes cron wrapper (generate + git-bus push)
  run_combined_execution.py   VPS cron wrapper (pull + run executor)
  verify_combined_api.py      read-only key/permission check
deploy/
  update_vps.sh               VPS self-update (code + data + crontab)
  cron/combined.crontab       self-update every 15 min, executor hourly
docs/SETUP.md                 VPS runbook
```

---

## Configuration (`/root/.hermes/.env` on the VPS, or `$COMBINED_ENV_FILE`)

| Key | Default | Meaning |
|---|---|---|
| `COMBINED_AUTO_TRADE` | `false` | `false` = **paper**: simulated account on mainnet prices, no orders. `true` = live orders (requires keys) |
| `COMBINED_TESTNET` | `true` | Order endpoint. `false` = mainnet. `COMBINED_BASE_URL` may override but must agree |
| `COMBINED_API_KEY` / `COMBINED_API_SECRET` | — | Sub-account keys (Contract trade + Read, IP-whitelisted) |
| `COMBINED_RISK_PCT` | `0.005` | Equity risked per trade (to the stop) |
| `COMBINED_CHAND4_RISK_PCT` / `COMBINED_XSMOM_RISK_PCT` | = risk_pct | Per-leg override |
| `COMBINED_XSMOM_ENABLED` | `false` | Turn the XSMOM leg on (a switched-off leg exits at its next rebalance) |
| `COMBINED_CHAND4_SHORTS` | `true` | Allow CHAND4 shorts |
| `COMBINED_CHAND4_TOP_N` / `COMBINED_XSMOM_TOP_N` | `5` / `5` | Max concurrent per side / leg |
| `COMBINED_MAX_POSITIONS` | `15` | Book cap (unmanaged positions count) |
| `COMBINED_CAP_DAILY` | `0.90` | Max total initial margin / equity |
| `COMBINED_MAX_POSITION_NOTIONAL` | `0.35` | Max single-position notional / equity |
| `COMBINED_GLOBAL_MAX_LEV` / `COMBINED_MMR` / `COMBINED_LIQ_SAFETY` | `5` / `0.005` / `1.5` | Stop-derived leverage |
| `COMBINED_STALE_PCT` | `5.0` | Skip an entry if the price moved >5% from the signal close |
| `COMBINED_SIGNALS_DIR` | `/opt/bybit-execution-data/combined` | Where signals land |
| `COMBINED_STATE_DIR` | `~/.combined_exec` | State + paper account (outside the repo) |
| `COMBINED_PAPER_EQUITY` | `10000` | Paper starting equity |

Strategy-shaping parameters (lookbacks, stop windows, multipliers) are
deliberately **not** env-overridable, so the signal box and the VPS cannot
silently diverge from each other or from the backtest.

---

## Quick start

```bash
# tests (no network)
python3 -m unittest combined_exec.tests.test_logic -v

# backtest (needs ../MOMSXperp/data_cache)
python3 backtest/combined_backtest.py

# Hermes: generate today's signal (add --no-push via the wrapper to skip git-bus)
python3 combined_signal.py --out /tmp/signals

# VPS: see what it would do right now, without touching anything
python3 -m combined_exec.run --dry-plan
python3 -m combined_exec.report
```

See [`docs/SETUP.md`](docs/SETUP.md) for the runbook.

## Status

- [x] Joint backtest with real costs, funding, point-in-time universe → CHAND4 only
- [x] Signal, executor, paper broker share one rule set; 28 tests
- [ ] Paper forward-test on the VPS (a few weeks) → compare with backtest
- [ ] Sub-account keys → testnet smoke test → mainnet with `COMBINED_AUTO_TRADE=true`

## Disclaimer

Research and automation only. Backtests are not indicative of future
results; the CHAND4 parameters were selected in-sample. Trading crypto
derivatives involves substantial risk.
