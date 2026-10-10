# The learning layer

Data, review and learning on top of the running league: minute data and
market context, an hourly risk check, daily and weekly reviews, a frozen
journal of every decision, graded outcomes, lessons, champion-vs-challenger
tests of strategy rewrites, anti-gambling hard limits and risk-adjusted
scoring.

Everything is off until you set `LEARNING_ENABLED=1` in `.env`. While it is
off, the league runs exactly as before: no new jobs, no prompt changes, no
new rules. All thresholds live in **`config/learning.yaml`**.

## How it fits the league

The league trades a **virtual ledger** (`arena/ledger.py`) and mirrors it into
the Alpaca paper account (`MIRROR_MODE`). The agents decide **once a week**
(Saturday). The layer keeps that design:

| When (ET) | Job | LLM? | What happens |
|---|---|---|---|
| every minute, market hours | collector (thread in the scheduler) | no | 1-minute IEX bars for held + watchlist symbols, SPY, sector ETFs, VIXY → `bars_1m` |
| 10:00, 11:00, …, 15:00, 15:30 | `risk-check` | only if a rule fires | stop-loss / gap / VIX spike / strategy drawdown; wakes only the affected agent, who may reduce, close or raise cash. Fills **immediately** at the latest minute price |
| 09:50 | `execute` (existing) | no | also fills the daily review's capital tilts / emergency rewrites at the open (superseded on a weekly execution day). From week 5 every fill passes the hard-limit check |
| 16:45 → 16:30+ | `daily` (existing), then `daily-review` | yes | daily snapshots + indicators, market context + regime, grader, strategy equity, then one review per agent |
| Saturday 12:00 | `weekly-review` | yes | grader, champion-vs-challenger verdicts, one review per agent, lesson promotion |
| after it | `decide` (existing) | yes | waits for the weekly review (at most 4 hours if that keeps failing). Every accepted decision is journaled |
| Monday open | `execute` (existing) | no | scores the week: `weekly_scores` with return, max drawdown and the risk-adjusted score |

The daily review starts after the `daily` job (it needs the day's bars,
which Alpaca only finalizes after 16:30), so "16:30" in the config means
"not before 16:30".

## What changed, and where

| Where | What |
|---|---|
| `config/learning.yaml` | every threshold (schedule, risk, regime, param ranges, hard limits, learning, scoring) |
| `arena/learning/` | the layer itself (see the list in `arena/learning/__init__.py`) |
| `arena/db.py` | creates the new tables on connect (`learning/schema.py`); adds `trades.decision_id` |
| `arena/ledger.py` | `rebalance(order_check=...)`: an optional check before every fill |
| `arena/league.py` | journals accepted decisions; order checks + trade links at execute; tilts at the open |
| `arena/scoring.py` | writes `weekly_scores`; risk-adjusted points and bonus from `scoring.start_week` |
| `arena/agents/decision.py`, `base.py`, `briefing.py`, `mcp_server.py` | the extended `submit_decision` schema and checks, the briefing's strategies section, the `submit_review` tool server |
| `arena/scheduler.py` | the new jobs, decide after the weekly review, the collector thread |
| `arena/main.py` | `risk-check`, `daily-review`, `weekly-review`, `grade`, `collect`, `learning-bootstrap`, `learning-status`, `review-dry-run` |
| `arena/api/learning.py`, `arena/web/learning.js` | `GET /api/learning` and the dashboard's "How they learn" section |
| `arena/tests/test_learning_*.py` | 74 tests |

## The database (and how it differs from `schema.sql`)

The league stores everything in one SQLite file, so the layer does too, as
agreed: one atomic store, the existing nightly backups (30 daily + 12
monthly, plus 7 local) and tests keep working, and ~20,000 minute bars a
day are well within SQLite's range. `db.py` keeps its SQL portable for the
later move to PostgreSQL + TimescaleDB. Differences from `schema.sql`:

- **agents / strategies**: the league's `agents` table (text ids) is reused.
  `strategies` is per slot without `allocated_capital`: the money stays in
  the ledger's `slots` table, the one place money moves.
- **orders / fills**: the existing `pending_orders` + `trades` are the
  orders and fills; `trades.decision_id` links each fill to its decision.
- **decisions**: `input_snapshot` points at `decision_inputs` (the exact
  prompt, stored once by SHA-256, since one prompt produces many rows).
  Checkpoints are `weekly_rebalance`, `risk_exit`, `daily_review` and
  `weekly_review` (there are no intraday `open/midday/close` turns). Extra
  columns: `week_no`, `shadow` (a challenger's positions), `fallback`
  (invalid output, forced hold), `source_ref`. **Frozen by triggers**: an
  UPDATE or DELETE fails.
- **outcomes** keeps the latest grade; **`outcomes_history`** (append-only)
  keeps every version, so a review "as of" time T reads exactly the grades
  that existed before T. All `*_pct` columns are in percent.
- **lessons** has a `key` (the agent reuses it to confirm a lesson) and
  `last_confirmed_on` (at most one confirmation per day).
- **reports** is a view over the existing weekly `strategy_reports` (worked /
  didn't work / to improve / to preserve per slot), so there is one report
  system, not two.
- **weekly_scores** is keyed by league week (`week_no`); `week_start` is its
  execution day. Extra columns: `mode`, `violations`.
- New process tables: `shadow_runs`, `review_runs`, `rejected_changes`,
  `risk_checks`, `risk_events`, `risk_last_prices`, `watchlists`,
  `portfolio_snapshots` (per strategy, at every risk check and close).
- `bars_1m`: no compression in SQLite; bars older than
  `minute_bar_retention_days` (30) are deleted instead.
- `pg_dump` backups don't apply; the existing backup job already backs up
  the whole file every night (`BACKUP_KEEP_DAILY=30`, local 7).

**Migrating the running league** happens automatically on the first job
(`learning-bootstrap`, idempotent): each slot used so far becomes a strategy
with a v1 version (no cooldown starts), every accepted weekly decision is
journaled with its original prompt and its fills linked, 60 days of regime
history and the VIX are backfilled, and the scored weeks get `weekly_scores`
(mode `raw_return`, for the record). Nothing existing is modified.

## The rules (all enforced in code)

- **Golden rule**: decisions are frozen; outcomes come later from the
  grader; a review reads only grades evaluated before it started, lessons
  and market context from before it. The grader itself only uses closes that
  had happened at its own time.
- **Hard limits** (from week 5, `scoring.start_week`): 20% per position
  of a strategy, gross ≤ 100%, shorts ≤ 30%, a stop on every position, 5
  trades per strategy per day (the weekly rebalance and tilts don't count,
  nor do engine stops or reduce-only risk exits), allowed instruments only
  (the league's S&P 500 + plain ETF universe; no options, leveraged/inverse
  ETFs or crypto), price ≥ $5, 20-day volume ≥ 1M. Checked when the agent
  decides (it gets the errors and can fix them) and again before every fill
  (refused fills are skipped and logged). An after-the-fact audit of each
  week's fills sets `broke_hard_limit` (score 0).
- **Saturday's decision** stays free on positions, but each slot must keep
  its strategy and follow its params (position size ≤ `position_max_pct`,
  stop within `stop_loss_pct`). Code slots get engine stops automatically.
  A new strategy in an empty slot needs written `rules` and uses the week's
  one rewrite.
- **Daily review**: tilts only when a regime change has held
  `confirm_days` (once per regime run, ≤ `max_tilt_pct` of equity moved);
  params only within `param_ranges` and outside cooldown; emergency
  rewrites only for strategies the drawdown limit flagged (they ignore the
  cooldown); candidate lessons need evidence (decision ids it saw graded).
- **Weekly review**: lessons confirmed `lesson_confirmations_to_activate`
  times become active (engine); lessons can be retired with evidence; one
  rewrite per agent per week, only with a true trigger, enough trades, no
  cooldown. The rewrite becomes a **challenger**: in the next decision the
  agent gives its positions too, they run in shadow for a week, and it is
  promoted only if it beat the champion on return **and** drawdown. The
  result is recorded as a lesson either way.
- **Every LLM answer** is structured JSON through one tool
  (`submit_review`), validated; invalid → one retry; invalid again → hold
  (logged, `fallback = 1`, Telegram alert). Every refused change is in
  `rejected_changes` and on the dashboard.
- **Scoring**: weeks 1-4 finish under the original rules (raw return wins
  the point; the $1,000 bonus every 4 weeks goes to the higher chained
  return). From week 5: `score = return % / max(max drawdown %, 1%)`, 0 if
  a limit was broken; the best score wins the point; the bonus period winner
  is the chained return / max(worst weekly drawdown, 1%).

## Dry run

A full daily + weekly review on a **throwaway copy** of the database: no
order is placed, nothing is saved, the real database is untouched.

```bash
# on the server, after deploying the branch (LEARNING_ENABLED can still be 0)
docker compose exec engine python -m arena.main review-dry-run                 # real agents (uses plan usage)
docker compose exec engine python -m arena.main review-dry-run --fake-agents   # no model calls
```

It prints each review's input summary, what the engine applied and refused,
and the resulting strategies / lessons. Single jobs also take `--dry-run`,
e.g. `daily-review --dry-run`, `risk-check --dry-run --fake-agents`.

## Going live

1. On the PC: `git bundle create league.bundle learning-layer` and copy it
   to the server (as in DEPLOY.md step 4).
2. On the server, **on a weekday evening or the weekend** (not during
   `execute`):
   ```bash
   cd ~/league && git pull ~/league.bundle learning-layer
   docker compose up -d --build          # LEARNING_ENABLED is still 0: same league, new tables
   docker compose exec engine python -m pytest -q arena/tests
   docker compose exec engine python -m arena.main review-dry-run
   ```
3. Read the dry run's output. If it looks right, set `LEARNING_ENABLED=1`
   in `.env` and `docker compose up -d`. Before Saturday 12:00 is best: the
   first weekly review then runs before the next decide.
4. Back out at any time: `LEARNING_ENABLED=0` and `docker compose up -d`.
   The new tables stay (harmless); the league runs as before.

## Checking that it runs

```bash
docker compose logs -f engine | grep -Ei "risk-check|review|collector|learning"
docker compose exec engine python -m arena.main learning-status
docker compose exec engine sqlite3 /app/data/arena.db \
  "SELECT job, key, exit_code, finished_at FROM job_runs WHERE job IN ('risk-check','daily-review','weekly-review') ORDER BY id DESC LIMIT 20"
ls data/logs/   # inside the arena-data volume: <date>_risk-check.log, _daily-review.log, _weekly-review.log
```

The dashboard's "How they learn" section shows weekly scores, the
strategy-by-regime table, the latest reports, lessons, version history,
challenger results and the rejected-changes log. Failures (a job, a review
that fell back to hold, the collector) arrive on Telegram like every other
job.
