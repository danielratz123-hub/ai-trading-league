# AI Trading League — Improvement Roadmap

Instructions for Claude Code working on the live league (databasedstrategy.com, Contabo VPS, Alpaca paper).

## Ground rules

1. **Read the existing codebase first.** Map the current structure, stack, database, scheduler and agent loop before changing anything. Adapt the items below to what already exists instead of rewriting.
2. **The league is live 24/7. Don't break it.** Work on a branch; deploy only after it runs cleanly in staging (Phase 0.6).
3. **One phase at a time,** in order. Phases build on each other. Each item has a "Done when" check — verify it before moving on.
4. **Never put secrets in code or git.** API keys live in a `.env` file on the VPS (and a `.env.example` with blank values in the repo).
5. After each phase, update this file: tick the items and note anything that changed.

---

## Phase 0 — Foundation and safety

Protect the data and make the system survivable before adding features.

- [x] **0.1 Nightly backups off the VPS.** Daily job dumps the database plus agent reports/journals, compresses them, and uploads to off-site storage (e.g. Backblaze B2 or a private GitHub repo). Keep 30 daily + 12 monthly copies.
  *Done when:* a backup lands off-site automatically, and a restore into a fresh database has been tested once.
  > Done (2026-10-11). Already existed: nightly at 17:30 ET to Google Drive (rclone), 30 daily + 12 monthly; reports and journals are tables in the one SQLite file, so they're in it. Test restore of the latest off-site backup run on the VPS (`scripts/restore.sh latest --test`): OK, scoreboard matched.
- [x] **0.2 Docker + docker-compose.** Containers for the bot/scheduler, database, website, and any workers. Data lives in named volumes.
  *Done when:* `docker compose up -d` on a clean Ubuntu box brings the whole league up from the repo + `.env` + a backup.
  > Already existed (DEPLOY.md, MIGRATION.md). The database is SQLite in the `arena-data` volume, so there is no separate database container.
- [x] **0.3 Hard-coded risk guardrails (outside the LLM).** Every order an agent proposes passes through plain code that rejects it if: ticker invalid, position > max % of the strategy's capital, daily loss limit hit, price moved > X% since the agent's analysis, or order size is malformed. Limits live in config.
  *Done when:* unit tests show each rule blocking a bad order, and rejected orders are logged with the reason.
  > Done. `arena/learning/hardlimits.py`, limits in `config/learning.yaml`; checked before every fill (and at decision time), refusals in `rejected_changes` + Telegram. Added now: daily loss limit (5%) and price moved since the analysis (8%). Applies from week 5 with LEARNING_ENABLED=1 (agreed: the next 4-week period). Tests: `test_learning_limits.py`, `test_phase0.py`.
- [x] **0.4 Kill switch.** One flag (config/DB + admin command) that pauses all trading instantly, per agent or globally, without stopping the rest of the system.
  *Done when:* flipping it stops new orders within one cycle.
  > Done. `pause` / `resume` with optional `--agent`, dashboard admin endpoint `?agent=`. Engine stop-losses keep running (they only reduce risk). Telegram on every flip.
- [x] **0.5 Phone alerts (Telegram bot).** Notify on: crash/restart, failed order, guardrail rejection, drawdown past threshold, missed scheduled job, weekly winner.
  *Done when:* each alert type has fired once in testing.
  > Done. Existing: crash/restart, failed jobs (each attempt + give-up), failed turns, mirror order rejections, weekly winner, daily/weekly status at 08:00. Added: guardrail rejection, strategy drawdown limit, kill switch. A job missed because the server was down is caught by the outside monitor (0.9).
- [x] **0.6 Staging mode.** A second copy of the league that runs on replayed historical data or a separate paper account, never touching the live league.
  *Done when:* staging can run a full day/week cycle on demand.
  > Done. `staging [--date] [--with-week] [--fake-agents]`: a full day (open, hourly risk checks, close, daily review, optional weekly review) on a fresh copy `data/staging.db`, paper mirror forced off. Whole weeks: `simulate`.
- [x] **0.7 Job queue with retries.** Move hourly risk checks and the daily/weekly combined reviews to a queue (Celery, RQ, or a Postgres-backed queue) with retries and a dead-letter log. Respect market hours and holidays.
  *Done when:* a deliberately failed review job retries and alerts if it still fails.
  > Done without Celery/RQ: the existing scheduler already is a durable queue (`job_runs` table = the dead-letter log), runs every job as its own process, retries 3 times 30 minutes apart, alerts on every failure and on giving up, follows the Alpaca calendar (holidays, half days), and catches up after downtime. Test: `test_phase0.py::test_failed_review_job_retries_then_gives_up_with_alerts`.
- [x] **0.8 Structured logging.** JSON logs with a `run_id` per agent cycle, plus agent, strategy, and model. Optional: Grafana + Loki.
  *Done when:* you can filter every log line from one review cycle by its `run_id`.
  > Done (`arena/logs.py`). JSON on the server; `run_id` = `job-<job_runs id>`, plus agent/model for agent work; every line of every job in `data/logs/<date>.jsonl`. Grafana/Loki not added.
- [ ] **0.9 Health monitoring.** Uptime Kuma (or similar) watching the site, the bot heartbeat, and the Alpaca connection; VPS CPU/RAM/disk metrics.
  *Done when:* stopping the bot container triggers an alert.
  > Live (2026-10-10): the scheduler pings Healthchecks.io every 5 min (`/fail` on database/disk/Alpaca problems; a test `/fail` ping went through). UptimeRobot watches https://databasedstrategy.com every 5 min. Left: the stop-the-engine test. CPU/RAM metrics not added.
- [ ] **0.10 CI/CD.** GitHub Actions: lint + tests on push; on merge to `main`, deploy to the VPS (pull, rebuild, restart, health check, roll back on failure).
  *Done when:* a merged change reaches production with no manual SSH.
  > Code done: `.github/workflows/ci.yml` (ruff + tests; deploy on main) and `scripts/deploy.sh` (waits for a quiet moment, rebuild, health check, automatic rollback, Telegram). Waiting for: the private GitHub repo, deploy keys and secrets (DEPLOY.md "GitHub and automatic deploys").

---

## Phase 1 — Measurement

Make results meaningful before making agents smarter.

- [ ] **1.1 Benchmark competitors.** Add an "SPY buy-and-hold" player and a "random picker" player, scored on the same risk-adjusted weekly system as the agents.
- [ ] **1.2 Proper stats per agent and per strategy.** Sharpe, Sortino, max drawdown, win rate, average win/loss, equity curve, all over weekly, monthly and all-time windows.
- [ ] **1.3 Trade journal with predictions.** Before each trade the agent records: thesis, expected move, timeframe, confidence (0–100%). After the timeframe, the system scores it automatically. Track calibration (are 80% calls right ~80% of the time?).
- [ ] **1.4 Strategy versioning.** Each strategy has a version number and a changelog entry (what changed, why, what evidence). Performance is tracked per version.
- [ ] **1.5 Model and prompt version tracking.** Every decision stores the model name and prompt version that produced it.
- [ ] **1.6 Cost tracking.** Log API tokens and cost per call, per agent. Report "return per dollar of API spend."

*Done when:* the dashboard can answer "is this agent beating SPY, is it good or lucky, what does it cost, and which strategy version is best?"

---

## Phase 2 — Smarter agents

- [ ] **2.1 Tool use.** Replace prompt-stuffed data with tools agents call as needed: `get_price_history`, `get_quote`, `get_news`, `get_earnings_calendar`, `get_my_positions`, `get_my_journal`, `run_backtest`, `place_order` (routed through the guardrails).
- [ ] **2.2 Backtesting sandbox.** Agents can test a new or modified strategy on historical data before adopting it. The result is saved with the strategy version.
- [ ] **2.3 Evidence bar for changes.** An agent can't change a strategy unless the change is backed by a backtest and/or a minimum number of weeks of evidence. One bad week alone is not enough.
- [ ] **2.4 Multi-agent teams.** Each side runs an analyst, a risk manager, and a devil's advocate that must argue against each trade before it's sent. Log the debate in the journal.
- [ ] **2.5 Long-term memory.** Vector database (e.g. pgvector, if Postgres) over each agent's past reports and journal entries, exposed as a `search_my_history` tool.
- [ ] **2.6 Model routing.** Cheap/fast model for hourly risk checks, strong model for daily/weekly reviews. Configurable per task.
- [ ] **2.7 Strategy evolution.** Monthly, each agent's worst strategy (risk-adjusted) is retired and the agent must design and backtest a replacement.

*Done when:* in staging, an agent can research with tools, backtest an idea, get challenged by its devil's advocate, and only then adopt a new strategy version.

---

## Phase 3 — Website and control

- [ ] **3.1 Admin panel (mobile-friendly, password + 2FA).** Pause/resume agents (kill switch), force a review, edit risk limits, view logs, alerts and health, trigger a backup.
- [ ] **3.2 Live updates.** WebSockets (or SSE) so positions, P&L and agent activity update without refresh.
- [ ] **3.3 Agent thought feed.** Live timeline of what each agent is analyzing, deciding and why (from the journal and debates).
- [ ] **3.4 Strategy leaderboard.** Rank all strategies across agents, not just agent vs agent.
- [ ] **3.5 Trade replay.** Click a trade to see the price chart around it, entry/exit, and the agent's reasoning and debate at that moment.
- [ ] **3.6 "Ask the agent" chat.** Ask Claude or GPT about its trades; it answers from its own journal and memory (read-only, cannot trade). Rate-limited.
- [ ] **3.7 Seasons.** Monthly or quarterly seasons with resets, a hall of fame, and badges (best comeback, lowest drawdown, most accurate calls).
- [ ] **3.8 PWA.** Installable app with push notifications.
- [ ] **3.9 Public read-only API.** League standings, trades and stats via documented endpoints, with rate limits.

---

## Phase 4 — More competitors

- [ ] **4.1 Plug-in agent interface.** A standard interface so any model can join on the same rules.
- [ ] **4.2 Add Gemini and one open-source model.**
- [ ] **4.3 Human player.** Joe can trade his own $1,000 demo portfolio through the admin panel and is scored on the same system.

---

## Parked for the future (not now)

- Open the league to outside bots via API (platform idea)
- Visitor prediction mini-games with a human leaderboard
- Auto-generated weekly video/podcast recap
- Plug-in strategy marketplace
- Content engine / newsletter / social accounts around the league
- Graduation criteria and a move to real money on IBKR
