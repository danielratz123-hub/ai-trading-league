# AGENTS.md — rules for every coding agent in this repo

This file is read by Codex (automatically), Claude (via CLAUDE.md) and Gemini (via GEMINI.md).
Owner: Joe (GitHub: danielratz123-hub). Only Joe merges.

## What this project is

AI Trading League: Claude and GPT agents trade virtual money on real US stock prices
(Alpaca paper data). Python 3.14, SQLite, FastAPI dashboard, Docker on a Contabo VPS,
live 24/7 at databasedstrategy.com. Read README.md (layout table) and ROADMAP.md before
starting. LAB.md and LEARNING.md explain the backtest lab and the learning layer.

## Team roles

| Agent | Role |
|---|---|
| Codex | Builder: implements issues labeled `codex`, opens a PR |
| Gemini | Builder: implements issues labeled `gemini`, opens a PR |
| Claude | Reviewer of every PR; builder for issues labeled `claude` |
| Joe | Writes issues, reads the PR conversation, merges |

## Hard rules (never break these)

1. **Merging to `main` deploys to the live VPS automatically** (`.github/workflows/ci.yml`).
   Never push to `main`, never merge, never approve your own work as final.
2. **No secrets anywhere in git.** Never read, print, create or commit `.env`, keys, tokens,
   passwords, IPs or `data/`. New settings go in `.env.example` with a blank value.
3. **Don't touch these without an issue that explicitly asks for it:**
   `.github/workflows/`, `scripts/deploy.sh`, `scripts/restore.sh`, `arena/backup.py`,
   `docker-compose.yml`, `Dockerfile`, `Caddyfile`, `arena/api/auth.py`,
   risk limits (`arena/config.py`, `arena/risk.py`, `arena/learning/hardlimits.py`,
   `config/learning.yaml`), the league agents' prompts (`arena/agents/`),
   and the Agent Hub itself (`hub/`, `Dockerfile.hub`).
4. **No look-ahead.** Anything that reads prices for a decision must go through
   `arena/tools/context.py`, which only loads data up to the decision date.
5. **Database changes must be backward compatible** with the live `arena.db`
   (add columns/tables; never drop or rename). Include the migration in the same PR.
6. Never make network calls or require keys in tests.

## How to work on an issue

1. One issue = one branch = one PR. Branch name: `<agent>/<issue-number>-<short-name>`
   (e.g. `codex/12-spy-benchmark`). Put `Closes #<issue>` in the PR description.
   Tasks started from the Agent Hub (see HUB.md) get `hub/<agent>-<issue>-<name>`
   branches; the hub pushes and opens the PR, so don't push or open PRs yourself there.
2. Read the existing code first and extend it. Don't rewrite working modules.
3. Keep PRs small: one roadmap item or less. If an issue is too big, say so in a
   comment and propose how to split it.
4. Before opening the PR, run exactly what CI runs and make sure both pass:
   ```
   pip install -r requirements.txt ruff==0.6.9
   ruff check --select F arena hub
   python -m pytest -q -p no:cacheprovider arena/tests hub/tests
   ```
5. Add tests for new behavior in `arena/tests/` (use `fakes.py` / `fake_cli.py`, no network).
6. If you finish a ROADMAP.md item, tick it and add a short `> Done.` note, like the existing ones.
7. PR description: what changed, why, how you tested it, and anything Joe must do on the
   VPS after merging (new `.env` setting, one-time command). Write it in plain English;
   Joe reads it on his phone.

## When you get review feedback

Fix every point marked **Must fix** by pushing more commits to the same branch.
Reply to each point you disagree with instead of silently ignoring it. Don't open a new PR.

## Code Review Rules

(Used by every reviewer, including Codex code review.)

Must fix (block the merge):
- Any secret, key, IP or `.env` content in the diff
- Breaking a hard rule above, or changing a protected file without an issue asking for it
- Look-ahead bias, or anything that could place real-money / mirror orders unexpectedly
- Bugs, unhandled errors in scheduled jobs, or a DB change that breaks the live database
- Missing tests for new behavior, or CI failing

Should fix (comment, don't block): unclear names, duplication, missing ROADMAP tick,
PR too big.

Don't comment on: formatting and style nits that ruff doesn't flag.
