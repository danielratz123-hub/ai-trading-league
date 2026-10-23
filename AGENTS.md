# AGENTS.md

Instructions for outside coding agents (Jules, Codex and others) that send
pull requests to this repository. Read all of it before you change anything.

## What this is

The AI Trading League: two AI agents (Claude and GPT) each manage a virtual
$1,000 portfolio on real US stock prices. Every weekend each agent researches
with a fixed toolbox and submits a decision; on Monday the engine executes it
at the open. Every trading day the engine updates prices and applies risk
rules (stops, take-profits, forced covers, borrow fees). Points are scored
weekly and a dashboard shows who is winning. Optionally the league is copied
into Alpaca **paper** accounts (`arena/mirror.py`).

It runs 24/7 in Docker on a VPS. Every push to `main` deploys there
automatically, so **main is production**.

More detail: `README.md` (commands), `LEARNING.md` (learning layer),
`LAB.md` (backtest lab), `DEPLOY.md` (server), `ROADMAP.md` (plan).

## Folder structure

```
arena/
  main.py              command-line entry point (python -m arena.main <command>)
  scheduler.py         runs the league's jobs on US market time, forever
  league.py            weekly and daily league cycle; pause / resume
  ledger.py            the ledger: the only code that moves money
  risk.py              hard limits and daily risk processing
  costs.py             trading costs (pure functions)
  scoring.py           weekly scoring, points, 4-week bonus, scoreboard numbers
  config.py            all tunable settings, including the competition Limits
  db.py                SQLite storage (schema, transactions, event log)
  prices.py            daily price bars from Alpaca, stored and read back
  trading_calendar.py  trading days and holidays from Alpaca's calendar
  universe.py          tradable universe: S&P 500 + a fixed set of ETFs
  backtest.py          historical simulation of a weekly-rebalanced portfolio
  mirror.py            optional copy of the league into Alpaca paper accounts
  staging.py           replay a full trading day on a COPY of the league
  reports.py           weekly strategy reports
  summary.py           the 08:00 ET Telegram status
  alerts.py            Telegram alerts (best effort, never blocks)
  health.py            health checks for Docker, /healthz and alerts
  backup.py            daily database backups to S3-compatible storage
  envfile.py           read / update the .env file from the setup page
  logs.py              structured JSON logs with a run_id (redacts secrets)
  mcp_server.py        MCP server giving a CLI agent its research tools
  agents/              model adapters: shared loop, Claude/GPT API, CLI agents,
                       briefing, decision parsing, Landlock jail, model lists
  tools/               research toolbox: data tools, indicators, sandboxed code
  sandbox/             runs agent-written code in a locked-down child process
  learning/            data, review and learning layer (journal, grader,
                       regime, reviews, hourly risk check, hard limits, scores)
  lab/                 backtest lab: history, market states, engine,
                       walk-forward, evidence rules (backtest only)
  api/                 FastAPI: read-only dashboard API, login, setup/control
                       panel, live prices, learning and lab data
  web/                 static dashboard and setup page (HTML/JS/CSS)
  tests/               pytest suite: no network, no keys
config/
  learning.yaml        learning layer thresholds and hard_limits
  lab.yaml             backtest lab settings
scripts/               deploy.sh, restore.sh, start.sh, demo db builder
.github/workflows/     CI: lint + tests; deploy on push to main
Dockerfile, docker-compose.yml, Caddyfile   the production stack
```

## Setup and tests

Python 3.12+ (CI uses 3.14).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt ruff==0.6.9
ruff check --select F arena        # lint, the same rule set CI uses
python -m pytest -q -p no:cacheprovider arena/tests
```

The tests need no network, no `.env` and no keys. Both commands must pass
before you open a PR; CI runs exactly these two.

To look at the dashboard with fake data: `python scripts/make_demo_db.py`
builds `data/demo.db` from scripted agents.

## Mock data, never live calls

- Never call Alpaca, OpenAI, Anthropic, Telegram or any other live service,
  in tests or while working. You don't have keys and must not ask for any.
- Use what the suite already has: the in-memory DB and fake calendar in
  `arena/tests/conftest.py` (`conn`, `agent`, `set_price`, `flat_prices`),
  `ScriptedAgent` in `arena/tests/fakes.py` in place of a model,
  `arena/tests/fake_cli.py` in place of the agent CLIs, and the fake broker /
  data source pattern in `test_mirror.py`, `test_prices.py`, `test_live.py`
  (a small fake class put in with `monkeypatch.setattr`).
- New code that talks to a service takes the client as a parameter or behind
  a function the tests can replace, so it can be tested with a fake.

## Coding conventions

- Every module starts with a docstring that says what it is for, in plain
  English, then `from __future__ import annotations`.
- Type hints on functions. Plain `sqlite3` with the helpers in `db.py`
  (`transaction(conn)` for anything that writes). No ORM.
- Dates are ISO strings (`"2024-01-02"`); market times are US Eastern.
- No magic numbers: thresholds live in `arena/config.py` or `config/*.yaml`
  (frozen dataclasses), not in the code.
- Comments explain why, not what. Keep them short and keep the existing ones.
- Small, focused functions; match the style of the file you are in.
- Every behaviour change comes with a test in `arena/tests/`.
- LF line endings (see `.gitattributes`); `.cmd` files keep CRLF.
- No new dependencies unless the PR explains why; pin exact versions in
  `requirements.txt`.

## DO NOT TOUCH

Do not modify these files or the behaviour they implement. If your task
seems to need a change here, stop and say so in the PR description instead.

- **Trade execution**: the order path in `arena/league.py` (execute, fills at
  the open), `arena/learning/execution.py`, `arena/mirror.py` (Alpaca orders),
  `arena/agents/decision.py` (turns a decision into orders).
- **Risk limits and guardrails**: `arena/risk.py`,
  `arena/learning/hardlimits.py`, `arena/learning/riskcheck.py`,
  `arena/costs.py`, the agents' limits (tool-call, spend and web-search caps
  in `arena/config.py`), `arena/sandbox/`, `arena/agents/jail.py`.
- **The config with limits**: the `Limits` class and every limit or cap in
  `arena/config.py`, the `hard_limits:` and `risk:` sections of
  `config/learning.yaml`.
- **Kill switch**: pause / resume (`league.is_paused`, `league.set_paused`,
  `/api/admin/pause` and `/api/admin/resume` in `arena/api/app.py`) and any
  emergency halt.
- **Ledger writes**: `arena/ledger.py`, and any new code that writes to its
  tables (`agents`, `slots`, positions, cash). Money moves only through the
  ledger's existing functions.
- **The immutable journal**: `arena/learning/journal.py` and the `decisions`
  table and its triggers in `arena/learning/schema.py`. Never add an UPDATE or
  DELETE on journal rows, and never drop or weaken the triggers.
- **Deploy**: `.github/workflows/`, `scripts/deploy.sh`, `scripts/restore.sh`.
- **Infrastructure**: `Dockerfile`, `docker-compose.yml`, `Caddyfile`,
  `.dockerignore`, `.gitignore`, `.gitattributes`.
- **Anything that reads secrets**: `arena/envfile.py`, `arena/api/auth.py`,
  `arena/api/control.py`, `arena/alerts.py`, `arena/backup.py`, the redaction
  in `arena/logs.py`, `.env.example`; and in any file, every line that reads
  `os.environ`, `os.getenv`, `require_env` or `.env`. Don't add new ones.

## How to contribute

- Work only on your own branch, never on `main`. Never push to `main`, force
  push, or rewrite history.
- Open a pull request into `main`. Describe what changed, why, and how you
  tested it. One topic per PR; keep it small.
- CI must be green. A human reviews and merges every PR; merging deploys.
- Never commit secrets: no keys, tokens, passwords, `.env` files,
  credentials JSON, databases (`*.db`, `data/`), backups or personal files.
  Test values must be obviously fake (`"k"`, `"PKTEST..."`).
- Don't change anything outside the scope of your task, including
  formatting of untouched code, docs you weren't asked to edit, or the files
  listed above.
