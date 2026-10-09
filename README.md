# AI Trading League (Claude vs GPT)

Two AI agents trade virtual money on real US stock prices. The engine enforces the rules,
the ledger moves the money, and a dashboard (phase 4) shows who's winning.

## Quick start (the easy way)

1. Double-click **`start.cmd`** in this folder. The first time, it installs everything (a few minutes).
2. Your browser opens the **Setup** page. Follow its five steps:
   connect Alpaca → connect the two AIs → download data → start the league → keep it running.
3. Keep the black `start.cmd` window open while the league runs. The dashboard is at
   http://127.0.0.1:8000 (the **⚙ Setup** button takes you back to setup at any time).

Everything below is the same thing done by hand from PowerShell.

## Setup (Windows, PowerShell)

```powershell
cd $HOME\Desktop\ai-trading-league
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then put your keys in .env
```

## Phase 1 commands

```powershell
python -m pytest -q                              # tests (no network, no keys needed)
python -m arena.main init-db                     # creates data\arena.db
python -m arena.main update-calendar             # trading days + holidays
python -m arena.main update-prices --tickers SPY,AAPL   # quick check
python -m arena.main update-prices               # full universe since 2015 (a few minutes)
python -m arena.main daily                       # end-of-day risk processing
python -m arena.main status
```

Every command accepts `--dry-run` (runs, then rolls back all DB changes).

## Phase 2: try the research tools by hand

Needs prices in the DB first. Results are what an agent would see.

```powershell
python -m arena.main tool market_overview
python -m arena.main tool screen --args '{\"conditions\": [\"price > sma_200\", \"pct_rank(return_63) >= 0.9\"], \"sort_by\": \"return_63\"}'
python -m arena.main tool backtest --args '{\"start\": \"2023-01-01\", \"portfolio\": {\"SPY\": 0.6, \"TLT\": 0.4}}'
python -m arena.main tool run_python --args '{\"code\": \"print(prices[''close''][''SPY''].tail())\"}'
```

Add `--date 2024-06-28` to research "as of" a past date.

## Phase 3: running the league

### Agents run on your subscriptions (no API bills)

By default (`AGENT_BACKEND=cli`) each agent's weekly turn runs through the official CLI,
signed in with your plan: **Claude Code** for Claude, **Codex** for GPT. Each CLI is
locked down to our research tools (served by `arena/mcp_server.py`) plus its own web
search: no shell and no file access.

```powershell
npm install -g @anthropic-ai/claude-code @openai/codex   # install / update both
claude auth login        # sign in with your Claude subscription
codex login              # sign in with your ChatGPT account
python -m arena.main check-agents --probe                # verify both work end-to-end
```

Turns count against your plans' usage limits. `AGENT_BACKEND=api` switches to the
pay-per-token API adapters instead.

### Weekly rhythm (US Eastern time)

| When | Command | What happens |
|---|---|---|
| Once, on a weekend | `python -m arena.main kickoff` | Creates both agents with $1,000 and runs week 1's decisions |
| Every Sat/Sun | `python -m arena.main decide` | Updates prices; both agents research and submit; orders wait for Monday |
| Every trading day, 9:50 | `python -m arena.main execute` | Fetches opening prices; fills pending stops at the open; on execution days scores last week and rebalances **at the open** |
| Every trading day, 16:30 | `python -m arena.main daily` | Updates prices; borrow fees; stop / take-profit / forced-cover checks (filled at the next open) |
| Any time | `python -m arena.main status` | Scoreboard, equity, positions |

`execute` waits until 9:46 because Alpaca's free data plan serves prices with a
15-minute delay. If Monday is a holiday, execution moves to the next trading day.

Try the loop on past weeks first (separate `data\simulation.db`, web search off):

```powershell
python -m arena.main simulate --start 2025-06-02 --weeks 1
```

`pause` / `resume` stop and restart `decide` and `execute` (`daily` keeps protecting positions).
`--dry-run` runs any command on a throwaway copy of the database.

## Phase 4: dashboard

```powershell
python -m arena.main serve                     # http://127.0.0.1:8000  (API docs: /api/docs)
python scripts\make_demo_db.py                 # optional: demo data, no AI usage
python -m arena.main serve --db data\demo.db   # look at the dashboard with demo data
```

- Scoreboard (points, equity, return vs SPY), daily equity chart (both agents + SPY,
  in $ or deposit-free %), a tab per agent (slots, holdings, exposure vs limits,
  trades, strategy code), the weekly timeline with both agents' reasoning side by side
  (hidden until the week is scored), and research activity & costs.
- The API is read-only (it opens the database in read-only mode). The only write is
  **pause / resume** (Admin button), which needs `ADMIN_TOKEN` from `.env`.
- Pages send `noindex`, so search engines (and the agents' web searches) don't pick
  up the opponent's live positions.
- Light and dark mode follow your OS; the ◐ button overrides it.

## Phase 5: running it for real

See **[DEPLOY.md](DEPLOY.md)**: the scheduler (`python -m arena.main scheduler`) runs
`decide` / `execute` / `daily` on US market time, either on this PC or on a VPS with
Docker (`docker compose up -d`). It also covers the dashboard on Vercel and the optional
Alpaca paper-account mirror (`MIRROR_PAPER=1`).

## Layout

| File | What it does |
|---|---|
| `arena/config.py` | All rules and limits (one place to change them) |
| `arena/db.py` | SQLite schema, nestable transactions |
| `arena/universe.py` | S&P 500 + ETFs, sector map |
| `arena/prices.py` | Alpaca adjusted daily bars; detects splits/dividends and re-downloads |
| `arena/trading_calendar.py` | Trading days from Alpaca (not `calendar.py`: that name shadows Python's stdlib) |
| `arena/costs.py` | Commission, slippage, borrow fee |
| `arena/ledger.py` | Agents → slots → positions; trades, deposits, snapshots, rebalance |
| `arena/risk.py` | Hard-limit validation; daily borrow fees, stops, forced covers |
| `arena/backtest.py` | Weekly-rebalance simulator with the real cost model |
| `arena/tools/context.py` | Loads prices **only up to the decision date** (the no-look-ahead guarantee) |
| `arena/tools/indicators.py` | return, sma, ema, rsi, vol, drawdown, rs, beta, volume_ratio, ... |
| `arena/tools/data_tools.py` | get_prices, get_indicators, screen, market_overview |
| `arena/tools/sandbox_tools.py` | run_python, backtest, save_strategy_code, code-slot runner |
| `arena/tools/registry.py` | Tool definitions, dispatcher, per-week budget, call log |
| `arena/sandbox/` | Runs agent code in a locked-down child process |
| `arena/agents/base.py` | The shared agent loop: budget, retries, nudges, logging |
| `arena/agents/cli.py` | Subscription backend: runs Claude Code / Codex, locked down to our tools |
| `arena/mcp_server.py` | The tool server those CLIs talk to (JSON-RPC over stdin/stdout) |
| `arena/agents/claude.py`, `gpt.py` | Optional pay-per-token API adapters (`AGENT_BACKEND=api`) |
| `arena/agents/briefing.py` | System prompt + weekly briefing (own state, scoreboard, opponent returns only) |
| `arena/agents/decision.py` | submit_decision schema, parsing, code-slot runs, validation |
| `arena/scoring.py` | Weekly returns, points, 4-week bonus, scoreboard |
| `arena/league.py` | kickoff / decide / execute / daily / simulate orchestration |
| `arena/api/` | Read-only FastAPI endpoints (`queries.py` = the SQL, `app.py` = routes + admin) |
| `arena/web/` | The dashboard: plain HTML/CSS/JS + Chart.js, no build step |
| `arena/scheduler.py` | Runs the jobs on US market time (DST, holidays, half days, catch-up) |
| `arena/mirror.py` | Optional: copy the leader into the Alpaca paper account |
| `Dockerfile`, `docker-compose.yml` | Engine + API containers (+ Caddy for HTTPS) |
