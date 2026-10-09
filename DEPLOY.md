# Deploying the AI Trading League

Two pieces run the league, plus an optional third:

| Piece | What it does | Where it can run |
|---|---|---|
| **Engine** (`scheduler`) | Runs `decide`, `execute`, `daily` on US market time; starts the agents' turns | This PC, or a VPS (Docker) |
| **API** (`serve`) | Read-only data for the dashboard + the pause button | Same machine as the engine (they share the database) |
| **Dashboard** (`arena/web`) | Static HTML/JS | Served by the API, or separately on Vercel |

The engine must **not** run on Vercel: Vercel is serverless (no long-running
processes, no persistent disk), and the engine needs both.

---

## The schedule (US Eastern time; DST and holidays handled automatically)

| Job | When | Notes |
|---|---|---|
| `execute` | every trading day, 09:50 | fills orders at the **open** price (Alpaca's free data lags 15 min) |
| `daily` | every trading day, 45 min after the close | 16:45, or 13:45 on half days |
| `decide` | the morning after the week's last trading day, 10:00 | normally Saturday; Friday if Friday is a holiday |
| `update-calendar` | Sundays 12:00 | refreshes holidays / half days |

Every run is logged in the `job_runs` table and in `data/logs/`. If the
scheduler was down, it catches up (up to 4 days back) when it restarts, and
a job that already succeeded never runs twice. A failing job is retried up
to 3 times, 30 minutes apart, then left for you to look at.

Before the scheduler can do anything useful you need to start the league
once by hand: `python -m arena.main kickoff` (on a weekend).

---

## Option A: run everything on this Windows PC (simplest)

Your Claude and ChatGPT logins already live here, so nothing extra is needed.

```powershell
cd $HOME\Desktop\ai-trading-league
.venv\Scripts\Activate.ps1
python -m arena.main check-agents --probe      # both agents OK?
python -m arena.main scheduler                  # leave this window open
```

To start it automatically when you log in (Task Scheduler):

```powershell
schtasks /Create /TN "AI Trading League" /SC ONLOGON /RL LIMITED /TR "\"$HOME\Desktop\ai-trading-league\scripts\run_scheduler.cmd\""
```

Then the dashboard, in a second window: `python -m arena.main serve` →
http://127.0.0.1:8000.

Caveats: the PC must be on and awake at 09:50 and after the close (Settings →
System → Power: set sleep to "Never" while plugged in), and Windows updates can
restart it; the scheduler catches up when it comes back.

---

## Option B: a small VPS with Docker (engine + API), dashboard on Vercel

Any Linux VPS with 2 GB RAM and Docker works (e.g. Hetzner, DigitalOcean).
*These Docker files were written for this project but not test-built yet
(Docker isn't installed on the development PC). Expect to fix small things
on the first build.*

### 1. Copy the project and configure

```bash
git clone <your repo> league && cd league      # or copy the folder over with scp
cp .env.example .env && nano .env
```

In `.env` set at least: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ADMIN_TOKEN`
(a long random string), and for HTTPS `DOMAIN=league.example.com`.

### 2. Sign the agents in (subscriptions, no API bills)

**Claude:** on your PC, run `claude setup-token`. It prints a long-lived token
for your Claude subscription. Put it in the VPS `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=...
```

**Codex:** sign in once inside the container with the device-code flow (open
the printed URL on any device and confirm):

```bash
docker compose build
docker compose run --rm engine codex login --device-auth
```

The login is kept in the `codex-home` volume. If Codex complains that its
sandbox isn't supported inside Docker, add `CODEX_SANDBOX=danger-full-access`
to `.env`: Codex's shell tool is switched off anyway, and the container is the
sandbox.

Check both:

```bash
docker compose run --rm engine python -m arena.main check-agents --probe
```

### 3. Load data, kick off, start

```bash
docker compose run --rm engine python -m arena.main update-calendar
docker compose run --rm engine python -m arena.main update-prices
docker compose run --rm engine python -m arena.main kickoff        # on a weekend
docker compose up -d --build                                        # engine + API
docker compose --profile https up -d                                # + HTTPS via Caddy
```

Caddy gets a certificate for `DOMAIN` automatically (point the domain's DNS
A record at the VPS first and open ports 80/443). Without the `https`
profile, the API is only on `127.0.0.1:8000` of the VPS; reach it with an SSH
tunnel: `ssh -L 8000:127.0.0.1:8000 you@vps`.

Logs: `docker compose logs -f engine` and the `data/logs/` folder in the
`arena-data` volume.

### 4. Dashboard on Vercel (frontend only)

1. In `arena/web/config.js` set `window.ARENA_API = "https://league.example.com";`
2. In the VPS `.env` set `DASHBOARD_ORIGINS=https://<your-project>.vercel.app`
   (CORS), then `docker compose up -d`.
3. On vercel.com: **Add New → Project**, import the repo, set **Root
   Directory** to `arena/web`, Framework **Other**, no build command. Deploy.

The API itself also serves the dashboard at its own URL, so Vercel is
optional.

---

## Optional: mirror the leader into your Alpaca paper account

Set `MIRROR_PAPER=1` in `.env`. After each morning's `execute`, the engine
copies the **leading agent** (most points; ties broken by cumulative return;
still tied → nothing) into your Alpaca **paper** account, scaled to the
paper account's equity.

Real brokers can't do everything the virtual ledger does, so:

- fractional shares are used for longs, but **shorts are rounded down to whole
  shares** and shorts under 1 share are skipped;
- a long ↔ short flip becomes two orders (close, then open);
- orders under $1 are skipped.

Every order and every difference from the league is logged in the
`mirror_log` table. Preview without placing anything:

```bash
python -m arena.main mirror            # plan only
python -m arena.main mirror --apply    # place the paper orders now
```

It only ever uses the paper endpoint (`paper=True`), never a live account.

---

## Security notes

- Agent-written code runs in the sandbox (separate process, no secrets in its
  environment, CPU/memory limits, file/network/process access blocked). On
  Linux, where user namespaces are allowed, it also runs in an empty network
  namespace (`unshare -rn`). Docker's default seccomp profile blocks that, so
  inside Docker the other layers do the work.
- The containers run as a non-root user with all Linux capabilities dropped.
- The API opens the database read-only; only pause/resume writes, and it needs
  `ADMIN_TOKEN`. Pages are marked `noindex`.
- Never commit `.env`.
