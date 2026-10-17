# The backtest lab

Phases 1-3 of the backtesting upgrade: 2016+ historical data, SPY-only
market-state labels, and an event-driven backtest engine with walk-forward
testing and a locked holdout. The goal is for agents to test ideas on years
of history first and treat live trading as confirmation.

**Nothing here changes the live league.** The lab is its own package
(`arena/lab/`). Its tables are only created when a lab command runs. Its one
scheduler job (the nightly history append) is off until `HISTORY_ENABLED=1`.
Phase 4 (connecting the lab to the agents) has not started; it needs approval.

## What is where

| Where | What |
|---|---|
| `config/lab.yaml` | every lab setting: history download, market-state thresholds, costs, engine, walk-forward windows, holdout. **Edited by a human only.** No agent tool writes files, and agent code runs in the sandbox, which can't open it |
| `config/learning.yaml` `hard_limits:` | the hard risk limits. The engine reads them from here, so backtests use exactly the live numbers |
| `data/history.db` | SIP daily bars from 2016 (split- and dividend-adjusted), plus the exchange calendar. Separate from the live `prices` table (IEX, ~2020+). Not backed up: it can be downloaded again |
| `arena.db`, lab tables | `market_states`, `backtest_runs`, `backtest_trades`, and the views `trade_market_states` and `strategy_by_market_state` (`arena/lab/schema.py`) |
| `arena/learning/regime.py` `classify_spy` | the one function that labels every day, history and live |
| `arena/lab/engine.py` | the engine |
| `arena/lab/walkforward.py`, `runs.py` | walk-forward windows, the holdout, and the run log |
| `arena/lab/strategies.py` | the live strategies written as code, plus SPY buy-and-hold |
| `arena/tests/test_lab_*.py` | the tests (no network) |

## Commands

```bash
python -m arena.lab history-download            # first fill (~520 symbols); resumable, rerun after a failure
python -m arena.lab history-update --arena-db data/arena.db   # nightly append + label the new days
python -m arena.lab history-gaps                 # gap check: exit 1 if any symbol misses a trading day
python -m arena.lab market-states                # label every day + check every day has a label
python -m arena.lab spy-check                    # engine check: SPY buy-and-hold vs SPY's own return
python -m arena.lab repro                        # coded live strategies vs the agents' actual picks
python -m arena.lab backtest --strategy gpt_six_month_momentum --idea gpt-momo --purpose walk_forward \
    --grid '[{"stop_loss_pct": 0.05}, {"stop_loss_pct": 0.08}]'
python -m arena.lab backtest --code my_idea.py --idea my-idea --purpose design --start 2018-01-02 --end 2022-12-30
```

`history-download` and `history-update` need `ALPACA_API_KEY` and
`ALPACA_SECRET_KEY` in `.env`. The free Alpaca plan includes SIP history; the
download stops at the last finished trading day.

## Phase 1: historical data

* SIP feed only, with no IEX fallback (IEX history starts ~2020 and would
  leave holes). Batches of 50 symbols at ≤120 requests a minute (Alpaca
  allows 200). A failed batch is retried 5 times (2, 4, 8 … seconds), then
  marked `failed`, and the next run retries just those symbols. Progress is
  saved after every batch.
* Nightly update: re-reads the last 10 days. If a stored close changed (a
  split or dividend rescales adjusted history), that symbol's whole history
  is downloaded again.
* Gap check: each symbol's days are compared with the exchange calendar,
  between its first and last bar. A symbol that starts later than 2016 is a
  listing, not a gap. One that stops early is reported as `stale` (renamed,
  delisted or bought).
* **Survivorship bias.** The universe is *today's* S&P 500. Companies that
  left the index since 2016 are missing, so historical results are
  optimistic. The note is stored in `history.db`, in every run row, and in
  every result (`data_note`).

## Phase 2: market-state labels (SPY only)

`trend` is `up` if SPY's close is above its 200-day average, otherwise
`down`. `vol` is SPY's 20-day realized volatility, annualized: `low` below
12%, `high` at 25% or more, `normal` in between. Together they make six labels
(`up_low` … `down_high`).

* One function, `classify_spy`, labels every day from the closes up to and
  including that day. The nightly history job labels each new live day with
  the same function.
* The first 199 trading days of 2016 have no 200-day average yet, so they
  have no label. Backtests start after them.
* Live trades are tagged by the `trade_market_states` view (the label at the
  close before the trade). Backtest trades carry `entry_state`.
* The learning layer's four regime labels (`trend_up` / `trend_down` /
  `choppy` / `high_vol`, which use the VIX) are unchanged. They still drive
  the live daily-review tilts. `strategy_by_market_state` sits next to the
  existing `strategy_by_regime` view, with live and backtest rows marked by
  `source`.

## Phase 3: the engine

* **Same interface as a live code slot:** `target_weights(prices, date)`.
  Agent code runs in the same sandbox. Trusted Python strategies are called
  with the frame cut at their decision day.
* **No lookahead.** The strategy decides at the close of day t (weekly: the
  week's last trading day), and orders fill at the open of the next trading
  day. A strategy that reads a later date raises `LookaheadError`.
* **Costs.** 5 bps spread plus 5 bps slippage per side, plus the league's
  commission and short borrow fee. Names whose 20-day dollar volume is under
  $20M get whole shares only.
* **Hard limits**, from `learning.yaml`:
  * the universe only;
  * price ≥ $5 and 20-day volume ≥ 1M;
  * each position ≤ min(`position_max_pct`, 20%);
  * shorts ≤ 30% and gross ≤ 100% (buys are scaled so cash never goes
    negative);
  * a stop on every position (from the decision close, as live; it fills at
    the stop, or at the open if the price gapped through);
  * in daily mode, at most 5 rebalancing trades a day.
* **Walk-forward.** Design on 24 months, test on the next 6, roll forward 6.
  With a parameter grid, the best design-window score is used on the test
  window. Only the test windows count, stitched into one out-of-sample
  record.
* **Locked holdout.** The last 12 months of data. A design or walk-forward
  run that reaches into it is refused (and still logged). The `holdout`
  purpose runs once per idea, ever: a database index enforces it.
* **Every run is logged** in `backtest_runs`: who asked, the strategy and its
  code hash, params, purpose, period, settings and results. The row is
  written before the run and frozen after it (triggers block UPDATE and
  DELETE). Results include `attempts` (this idea's run count), so a lucky
  winner after many tries is visible.
* **Results:** total and annual return, max drawdown, the league's
  risk-adjusted score (the mean weekly `return % / max(drawdown %, 1%)`,
  plus `period_score` over the whole run), Sharpe, trades, win rate, SPY and
  the difference from SPY, and a per-market-state breakdown.
* **Benchmark mode** (no stops, no position cap, no price/volume filter) is
  only allowed for the `check` purpose. A 100% SPY buy-and-hold needs it,
  because the live limits cap any position, ETFs included, at 20%.
