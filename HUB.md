# Agent Hub

One page to give coding work to **Claude, Codex and Gemini**, running on **your
subscriptions**. You choose who builds, who reviews and, for bigger requests, who
manages. You write it on your phone; the hub does the rest and tells you when a
PR is ready to merge.

**Where:** https://databasedstrategy.com/hub (same login as the dashboard)

## Two ways to start

**One task:** pick the **builder** and the **reviewer**, write the task, press **Start**.

**Plan with a manager:** write what you want in a few sentences, pick the
**manager** and the **reviewer**. The manager reads the code and proposes a
plan of 1–6 small tasks, each with the builder it thinks fits (and why). You
can change any task's builder or reviewer, then:
- **Approve & start** → the tasks run one after another; each starts when the
  one before it is merged (so later steps build on earlier ones)
- **Ask for changes** → write what to change; the manager makes a new plan
- **Reject**

Nothing is built before you approve a plan. Any agent can take any role. If
the builder and the reviewer are the same agent, the page warns you: a second
opinion from another agent catches more.

## The page

- **Running now** at the top: which agent is working on what, with its live output.
- Your work grouped as **Needs you** (plans to approve, stuck tasks), **Ready to merge**,
  **Working** and **Done**. Every task has a progress rail:
  Build → PR → Review → Ready → Merged → Live. Tap a task for its review, timeline and log.
- After **Merge and deploy**, the task shows **Deploying…**, then **Live** (or **Deploy
  failed**, with what to do). Wait for Live before running commands on the VPS yourself.
- The chips at the top show how many runs each agent did today, so you can keep an eye on
  your subscriptions' limits.

## Models and effort

Open **Agents** (top right). For each agent pick its default **model** and **effort**, or
switch it off (it's then not offered for new work, including by managers). Model names
are whatever the CLI accepts: Claude takes `opus`, `sonnet`, `haiku` or a full name;
Gemini's **Find models** asks the Antigravity CLI for its list; for Codex type the name
you see in the Codex app's model picker. "The CLI's own default" uses whatever that CLI
picks. When you start a task or a plan you can also choose a model just for that one.

## Add an agent

Agents are listed in `hub/agents.yaml` (Claude, Codex, Gemini). To add one, for example
a future CLI called `newagent`:

1. **Install it** in `Dockerfile.hub`: one more `RUN` line, like the Antigravity one.
2. **Describe it** in `hub/agents.yaml`:
   ```yaml
   newagent:
     label: New Agent
     color: "#3DD68C"
     cmd: [newagent, -p, "{prompt}", --yes]   # how to run one task headless
     model_args: [--model, "{model}"]         # how to choose a model (optional)
     efforts: []                              # effort levels, if it has them
   ```
3. Merge (the hub can even build this PR for you), then sign it in once on the server:
   `docker compose exec -it hub newagent` and press **Test agents**.

Trying an agent on the server only, without a PR: put the same entry in
`/hub/data/agents.yaml` inside the container. An entry like `codex: false` there hides
an agent. Agents can't be added from the web page on purpose: anyone who stole your
login could otherwise run any command on your server.

## What happens to a task

1. The hub opens a GitHub issue, makes a fresh copy of the repo and a branch
   `hub/<agent>-<issue>-<name>`, and runs the builder's CLI on the task.
2. It runs the same checks as CI. If they fail, the builder gets the errors and
   two more tries.
3. It pushes the branch and opens a PR. **CI** runs on GitHub, and the hub runs
   **the reviewer you picked** on the PR. The review is posted on the PR with a
   verdict: ✅ ready / 🔧 needs fixes / ⛔ Joe must look. (The automatic GitHub
   Claude review skips hub PRs, so there's one verdict, from your reviewer.)
4. 🔧, or CI red → the hub sends the review to the builder, which fixes and
   pushes; the reviewer checks again. Up to 4 rounds.
5. ✅ and CI green → **Ready to merge**. You get a Telegram message (if set
   up), open the page, press **Merge & deploy**.
6. Anything off (⛔, an agent error, too many rounds) → **Needs you**. Read the
   note, then **Continue** (optionally with instructions for the builder) or
   **Cancel**. If you push to the branch yourself, the reviewer looks again.

The hub **never merges by itself**. One agent runs at a time.

## Safety

- Its own container (`hub`), its own volumes and its own `hub.env`. It can't see
  the league's database, `.env`, backups or the league agents' logins.
- The GitHub token never reaches the agents: git gets it per command, and the
  agents' environment is cleaned. Claude's login token goes to Claude only.
- Nothing is pushed if the change contains `.env`, `data/`, keys, a database
  file, or one of the hub's own secrets.
- The token has no "Workflows" permission, so no agent change can modify
  `.github/workflows` (GitHub rejects the push).
- The page sits behind the dashboard login (Caddy checks `/auth/check` first),
  and every button needs a header other websites can't send.
- If the hub breaks, the league doesn't notice: deploys health-check and roll
  back only `engine`, `api` and `setup`.

## Set it up (once, about 15 minutes)

You need SSH to the server, in the league folder (where `docker-compose.yml` is).
Merging the hub PR changes nothing until you do this.

**1. A GitHub token for the hub**

GitHub → your picture → **Settings → Developer settings → Personal access
tokens → Fine-grained tokens → Generate new token**

- Name: `agent-hub`, expiration: 1 year
- Repository access: **Only select repositories** → `ai-trading-league`
- Permissions → Repository:
  - **Contents**: Read and write
  - **Issues**: Read and write
  - **Pull requests**: Read and write
  - **Actions**: Read-only
  - leave **Workflows** at *No access*
- Generate and copy it (starts with `github_pat_`).

**2. Claude's login** (on your PC): `claude setup-token` → copy the
`sk-ant-oat01-...` token. You can reuse the one from the league.

**3. `hub.env` on the server**

```bash
cp hub.env.example hub.env
nano hub.env          # paste HUB_GITHUB_TOKEN and CLAUDE_CODE_OAUTH_TOKEN
chmod 600 hub.env
```

Optional: copy `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env` to get
"ready to merge" messages.

**4. Switch it on**

```bash
echo "COMPOSE_PROFILES=hub" >> .env
docker compose up -d --build hub caddy
docker compose ps hub          # "healthy" after a minute
```

From now on every automatic deploy also updates the hub.

**5. Sign in Codex and Gemini** (your subscriptions, once; saved in volumes)

```bash
# Codex: prints a link + code; open it on your phone, sign in with ChatGPT
docker compose exec hub codex login --device-auth

# Gemini (Antigravity CLI): prints a Google sign-in link; open it on your phone,
# sign in with the account that has your Gemini subscription, paste the code
# back if it asks. When it says you're signed in, type /quit.
docker compose exec -it hub agy
```

Gemini runs through Google's **Antigravity CLI** (`agy`): the old Gemini CLI no
longer signs in with a personal Google AI subscription. If `agy` can't keep the
sign-in inside the container (it may complain about a "keyring"), use a free key
from Google AI Studio instead: put `GEMINI_API_KEY=...` in `hub.env` and run
`docker compose up -d hub`.

**6. Check** — open `/hub`, press **Test agents**. After a few minutes each
agent gets a green dot (hover/tap shows the version). A red dot shows why
(usually "not signed in": repeat step 5 or 2).

## Day to day

- **Turn it off:** `docker compose stop hub` (or remove `COMPOSE_PROFILES=hub`
  from `.env` and `docker compose up -d --remove-orphans`).
- **Logs:** `docker compose logs -f hub`. Each run's full output is also on the
  task's page ("Latest log").
- **Change an agent's command**: edit `hub/agents.yaml` (or, server only, set
  `HUB_CMD_GEMINI` etc. in `hub.env` to a JSON list with `"{prompt}"` where the task goes).
- **Old branches**: the hub deletes each task's `hub/` branch after its PR is merged.
- Your last choices of builder, reviewer and manager are remembered in your browser.
- **Limits:** `HUB_MAX_ROUNDS` (4), `HUB_AGENT_TIMEOUT` (45 min),
  `HUB_REVIEW_TIMEOUT` (40 min) in `hub.env`.

Use it for a few tasks a day; it runs the official CLIs on your own server for
your own project, which is normal personal use of the subscriptions.
