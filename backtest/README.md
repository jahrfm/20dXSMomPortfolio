# Combined backtest — CHAND4 40/15 + XSMOM on one account

`combined_backtest.py` simulates the book **exactly as the executor trades it**:
one shared-equity account, day by day, calling the same
`strategy.build_signal()` and `planner.plan_day()` / `trail_updates()` that the
live signal generator and VPS executor call.

```bash
python3 backtest/combined_backtest.py            # ~1 min; data from ../MOMSXperp/data_cache
python3 backtest/combined_backtest.py --data DIR --start 2023-01-01 --capital 10000
```

Outputs `results/combined_backtest_results.json` (summary, checked in) plus
`combined_equity_curve.json` and `combined_trades.json` (regenerated, not
checked in).

## What it models

| | |
|---|---|
| Data | Every Bybit USDT perp ever listed (806 with ≥30 bars, **delisted included**), daily klines + actual funding history, 2020-03 → 2026-09-27 |
| Universe | Point-in-time: top-50 by trailing 30-day turnover, ≥110 bars of history, stablecoins excluded |
| Timing | Signal on the completed bar D-1; fills at D's open; stops checked on D's bar **including the entry day**; gap-through stops fill at the open |
| Costs | 5.5 bps taker + 5 bps slippage per side; funding charged/received daily from real 8h/4h/1h rates (0.01%/8h assumed where history is missing) |
| Sizing | 0.5% of equity risked per trade to the stop; max 35% notional per position; margin ≤ 90% of equity at stop-derived leverage (max 5x) |
| Book | CHAND4 ≤ 5 concurrent per side; XSMOM top-5 on 14-day Mondays (anchor 2026-08-17); one leg per symbol |
| Period | 2022-02-27 → 2026-09-27 (first day the universe had 50 names), $10,000 start |

## Results (run 2026-09-28)

| Variant | CAGR | Sharpe | Max DD | Final | Trades |
|---|---:|---:|---:|---:|---:|
| **CHAND4 only (0.5% risk)** | **8.9%** | **1.05** | **13.1%** | $14,756 | 465 |
| CHAND4 only, 1% risk | 17.6% | 1.09 | 22.2% | $20,993 | 465 |
| CHAND4 only, no funding | 6.6% | 0.81 | 12.7% | $13,421 | 465 |
| XSMOM only | 0.9% | 0.21 | 55.8% | $10,408 | 367 |
| XSMOM only, no funding | −14.5% | −0.24 | 67.1% | $4,884 | 367 |
| XSMOM only, equal weights | −3.9% | −0.06 | 38.3% | $8,338 | 442 |
| XSMOM only, research universe (today's top-50, lookahead) | −0.7% | 0.05 | 32.8% | $9,673 | 231 |
| Combined (0.5% risk) | 8.3% | 0.46 | 51.7% | $14,417 | 687 |
| Combined, CHAND4 long-only | 4.7% | 0.31 | 55.5% | $12,345 | 525 |
| Combined, 1% risk | 12.0% | 0.47 | 74.2% | $16,829 | 687 |

Daily-return correlation CHAND4 vs XSMOM: **+0.27** (the research reported
−0.07 on a biweekly grid that dropped 21 of 125 periods).

CHAND4 by year (0.5% risk): 2022 −2.4% · 2023 +32.0% · 2024 −4.4% ·
2025 +3.6% · 2026 YTD +15.6%. Trades: PF 1.54, win rate 38%, avg +0.18R,
best +19.9R, avg hold 26 days; longs PF 1.50, shorts PF 1.65.

## Conclusions

1. **CHAND4 has an edge that survives realistic execution.** Sharpe ~1.0, DD
   13% at 0.5% risk. Returns are lumpy (2023 and 2026 carry most of it) and
   ~2 points of CAGR come from funding. The parameters (40/15, 4×ATR) were
   picked on 2021–2026 data, so this is still an in-sample estimate —
   expect less forward.
2. **XSMOM has no edge once the universe is point-in-time and costs/funding
   are included.** Without funding it loses 14.5%/yr; its small positive
   result depends on collecting extreme negative funding on freshly listed
   small caps, which is not something to underwrite. Signal-proportional
   weighting also concentrates 75–90% of the leg's risk in one name.
   The uncommitted re-run in `MOMSXperp` (point-in-time universe, costs,
   funding) independently reaches the same verdict: Sharpe 0.03, PF 1.01.
3. **Combining makes the book worse, not better.** Correlation is positive
   (+0.27), and XSMOM adds a 50%+ drawdown for no return.

**Default config therefore runs CHAND4 only** (`COMBINED_XSMOM_ENABLED=false`).
The XSMOM leg is still coded, signalled and tested, and can be switched on.

## Known limits

- Fills at the daily open; live entries happen ~20 min after 00:00 UTC at
  the mark (the 5% staleness guard skips entries that moved too far).
- Stops are evaluated on daily bars; intraday path within a bar is unknown
  (a bar that touches both a new high and the stop is counted as a stop).
- No capacity/market-impact model beyond 5 bps slippage; fine at small size.
- Funding for ~40% of (mostly delisted) symbols is missing and assumed at
  +0.01%/8h (longs pay).
