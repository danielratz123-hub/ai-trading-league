# Running the AI Trading League on a server (24/7)

Everything runs on one small Linux server (VPS) with Docker: the scheduler,
the agents, the dashboard, HTTPS, backups and alerts. Your PC can be off.

| Service | What it does | Reachable from |
|---|---|---|
| `caddy` | HTTPS for your domain (automatic Let's Encrypt certificate) | the internet, ports 80 and 443 only |
| `api` | dashboard + API, behind a password | only Caddy (internal Docker network) |
| `engine` | the scheduler: decide, execute, daily, login checks, backups, alerts | nothing (it only connects out) |
| `setup` | the setup page | only the server itself, via an SSH tunnel |

Moving an existing league from your PC: read **MIGRATION.md** first, then
follow this guide up to step 8 and continue in MIGRATION.md.

> The old "dashboard on Vercel" option is **no longer supported**: the
> dashboard now needs a login, which is served by the same app.

Contents: 1 Server · 2 Hardening · 3 Docker · 4 Code · 5 `.env` · 6 DNS ·
7 Start · 8 Sign the agents in · 9 Start the league · 10 Setup page ·
11 Alerts · 12 Backups · Schedule · Day-to-day · Codex's sandbox in Docker ·
Security · Troubleshooting

---

## 1. The server

- Ubuntu **24.04** LTS, **4 GB RAM**, 2 vCPUs, 40 GB disk or more
  (Hetzner CX22/CPX21, DigitalOcean, Vultr, ...). Choose a US East region:
  shorter round-trips to Alpaca at 09:50 ET.
- At creation, add your **SSH public key** (on Windows: `ssh-keygen -t ed25519`
  in PowerShell, then paste the contents of `C:\Users\<you>\.ssh\id_ed25519.pub`).
- Note the server's IPv4 address. Below it's `SERVER_IP`.

From your PC: `ssh root@SERVER_IP`

## 2. Hardening (once, as root)

Do these before anything else. Each block can be pasted as-is.

**A user for you, with sudo** (replace `daniel` if you like):

```bash
adduser --gecos "" daniel
usermod -aG sudo daniel
mkdir -p /home/daniel/.ssh && cp /root/.ssh/authorized_keys /home/daniel/.ssh/
chown -R daniel:daniel /home/daniel/.ssh && chmod 700 /home/daniel/.ssh && chmod 600 /home/daniel/.ssh/authorized_keys
```

Open a **second** PowerShell window and check `ssh daniel@SERVER_IP` works
before continuing. Keep the first window open in case something goes wrong.

**SSH: keys only, no root login:**

```bash
cat >/etc/ssh/sshd_config.d/90-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
EOF
sshd -t && systemctl reload ssh
```

**Firewall (UFW): only SSH, HTTP and HTTPS:**

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp      # HTTP/3
ufw --force enable
ufw status verbose
```

> Docker note: ports *published* by Docker bypass UFW. That's why only
> Caddy publishes ports (80/443, which are open anyway); the app and the
> setup page publish nothing. Don't add `ports:` to other services.

**fail2ban** (bans addresses that keep failing SSH logins):

```bash
apt-get update && apt-get install -y fail2ban
cat >/etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban && fail2ban-client status sshd
```

**Automatic security updates:**

```bash
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
cat >/etc/apt/apt.conf.d/52arena-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
```

Reboots happen at 04:30 server time only when an update needs one. The
containers come back by themselves (`restart: unless-stopped`) and the
scheduler catches up on anything it missed. Set the server clock to UTC
(the default) and 04:30 UTC is 23:30/00:30 ET: no jobs run then.

**2 GB swap file** (a safety net for memory spikes during agent turns):

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
free -h
```

From now on log in as `daniel` (`ssh daniel@SERVER_IP`).

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit        # log out and back in so the group applies
```

Check: `docker run --rm hello-world`

## 4. The code

The project lives in a local git repository on your PC. Copy it over as a
git bundle (no `.env`, no database, nothing secret):

```powershell
# on the PC, in the project folder
git bundle create league.bundle --all
scp league.bundle daniel@SERVER_IP:~/
```

```bash
# on the server
git clone -b vps-deploy ~/league.bundle ~/league && cd ~/league
```

To update later: make a new bundle, copy it, then
`git pull ~/league.bundle vps-deploy && docker compose up -d --build`.

## 5. `.env`

```bash
cd ~/league
cp .env.example .env
chmod 600 .env
nano .env
```

Every setting is explained in the file. On the server you need at least:

| Setting | How |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca dashboard → Paper → API Keys |
| `DOMAIN` | e.g. `league.example.com` |
| `ADMIN_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` (at least 32 characters) |
| `DASHBOARD_PASSWORD_HASH` | after the first build: `docker compose run --rm api python -m arena.main hash-password`, paste the line it prints |
| `CLAUDE_CODE_OAUTH_TOKEN` | step 8 |
| `TELEGRAM_*` | step 11 |
| `BACKUP_S3_*` | step 12 |

Without `ADMIN_TOKEN` and `DASHBOARD_PASSWORD_HASH` the dashboard refuses to
start on the server (on purpose: it must never run unprotected).

The `setup` service edits `.env` from the setup page; it runs as uid 1000.
If your server user isn't uid 1000 (`id -u`), run `sudo chown 1000 .env`.

## 6. DNS

At your domain registrar, add an **A record**: name `league` (or `@` for the
bare domain), value `SERVER_IP`, TTL 300. If your server has IPv6, add an
**AAAA** record too. Check from the PC: `nslookup league.example.com` must
show `SERVER_IP`. Caddy can only get a certificate once this works.

## 7. Build and start

```bash
docker compose build
docker compose run --rm api python -m arena.main hash-password    # put the line in .env
docker compose up -d
docker compose ps                  # all "running"; health "healthy" after a minute
```

Open `https://league.example.com`: you should see the login page with a
valid certificate. (The first certificate can take a minute; see
`docker compose logs caddy`.)

## 8. Sign the agents in

The server has no browser, so both CLIs use a login that works without one.
Your subscriptions are used exactly as on the PC; no API keys, no token bills.

**Claude Code: a long-lived token (`claude setup-token`)**

1. On your **PC** (where you're already signed in to Claude Code), open a terminal and run:
   ```
   claude setup-token
   ```
2. A browser window opens; approve it with the Claude account that has your
   subscription. The terminal then prints a token starting with `sk-ant-oat01-`.
   It is valid for about a year.
3. On the **server**, put it in `.env` (one line, no quotes, no spaces):
   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```
4. Apply it: `docker compose up -d` (containers pick up `.env` changes when recreated).

Only the Claude Code process receives this token. Codex, the tool server and
the code sandbox never see it (`arena/agents/cli.py` removes every
secret-looking variable from the CLIs' environment).

When it expires or you revoke it, the login check alerts you (below);
repeat steps 1 to 4.

**Codex: device-code login (or copy your PC's login)**

Option 1, device code (recommended):

1. On the server:
   ```bash
   docker compose run --rm engine codex login --device-auth
   ```
2. It prints a URL and a short code. Open the URL on your phone or PC, sign
   in with your ChatGPT account and enter the code.
3. The terminal says you're logged in. The login is saved in the `codex-home`
   volume, so it survives restarts and rebuilds.

Option 2, copy the login from your PC: Codex keeps it in
`%USERPROFILE%\.codex\auth.json` on Windows.

```bash
# on your PC (PowerShell): copy the file to the server
scp $env:USERPROFILE\.codex\auth.json daniel@SERVER_IP:/tmp/codex-auth.json
# on the server: put it into the volume, then delete the copy
docker compose run --rm -v /tmp/codex-auth.json:/tmp/auth.json:ro engine \
  sh -c 'cp /tmp/auth.json /home/arena/.codex/auth.json && chmod 600 /home/arena/.codex/auth.json'
rm /tmp/codex-auth.json
```

Treat `auth.json` like a password. A copied login shares its refresh token
with the PC; if one machine refreshes it, the other can get signed out. If
you also use Codex on the PC, prefer option 1 (a separate login).

**Check both:**

```bash
docker compose run --rm engine python -m arena.main check-agents --probe
```

Both must say `OK` for "installed", "signed in" and "probe turn".

**Automatic login checks.** The scheduler runs `check-logins` (the same
probe) every day at 07:00 ET and again 2 hours before each decide (Saturday
08:00). If either agent fails, you get a Telegram alert. If a login is
broken at decide time itself, that agent's turn is **not** run and **not**
counted as failed: you get an alert right away, and decide retries every 30
minutes until Monday's open. Fix the login and the next retry completes the
week. (If it's still broken at Monday's open, run
`docker compose exec engine python -m arena.main decide` once it's fixed;
the trades then happen at the next open.)

## 9. Start the league

- **Moving your existing league from the PC:** follow **MIGRATION.md** now.
- **A brand-new league** (on a weekend):
  ```bash
  docker compose exec engine python -m arena.main update-calendar
  docker compose exec engine python -m arena.main update-prices
  docker compose exec engine python -m arena.main kickoff
  ```

## 10. The setup page (through an SSH tunnel)

The setup page can change settings and run commands, so it is never on the
internet. The `setup` service listens on the server's own `127.0.0.1:8000`
only. Reach it from your PC with an SSH tunnel:

```powershell
ssh -N -L 8000:localhost:8000 daniel@SERVER_IP
```

Leave that window open and browse to **http://localhost:8000/setup.html**
on the PC (sign in with the dashboard password). Close the window to end
the tunnel. If port 8000 is busy on your PC (the PC app is running), use
`-L 8800:localhost:8000` and http://localhost:8800/setup.html.

Settings saved there go into the server's `.env`; apply them with
`docker compose up -d`. On the server the scheduler is managed by the
`engine` container, so its start/stop buttons are hidden.

## 11. Alerts (Telegram)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. It replies
   with a token like `123456:ABC...`: that's `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it any message (bots can't write first).
3. Get your chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":-\?[0-9]*'
   ```
   The number is `TELEGRAM_CHAT_ID`.
4. Put both in `.env`, run `docker compose up -d`, then test:
   ```bash
   docker compose exec engine python -m arena.main test-alert
   ```

You'll get a message for: a failed or retried job; a failed or nudged agent
turn; a rejected decision; login problems; mirror sync errors; low disk
space (under 2 GB); the app or scheduler (re)starting; and after every
execute, a summary (trades, equity per agent, and the weekly winner when a
week is scored). Repeats of the same problem are limited to one every 2
hours. Sending an alert can never stop or crash a job.

## 12. Backups

Every day at 17:30 ET the engine makes a backup with SQLite's online backup
API (safe while the database is in use, unlike copying the file), checks
it, compresses it and uploads it to your S3-compatible storage. It keeps
**30 daily** and **12 monthly** (first backup of each month) copies and
deletes older ones. A local copy of the last 7 is kept in `data/backups`.

**Backblaze B2 setup:** Buckets → **Create a Bucket** (Private, encryption
on). App Keys → **Add a New Application Key**, allow access to that bucket
only, read and write. Put the values in `.env`:

```
BACKUP_S3_ENDPOINT=https://s3.us-west-004.backblazeb2.com   # shown on the bucket page
BACKUP_S3_REGION=us-west-004
BACKUP_S3_BUCKET=my-league-backups
BACKUP_S3_ACCESS_KEY_ID=<keyID>
BACKUP_S3_SECRET_ACCESS_KEY=<applicationKey>
```

Test it: `docker compose exec engine python -m arena.main backup`

**Restore** (see `scripts/restore.sh` for details):

```bash
docker compose exec engine python -m arena.main backups          # list them
./scripts/restore.sh latest                                       # or a name from the list
```

`restore.sh` stops the app, downloads and checks the backup, keeps the
current database as `arena.db.before-restore-<time>`, puts the backup in
place and starts everything again. Test a restore once, on a copy, without
touching the live league: `./scripts/restore.sh latest --test`.

---

## The schedule (US Eastern time; DST and holidays handled automatically)

| Job | When | Notes |
|---|---|---|
| `execute` | every trading day, 09:50 | fills orders at the open (Alpaca's free data lags 15 min) |
| `daily` | every trading day, 45 min after the close | 16:45, or 13:45 on half days |
| `reports` | trading days from 10:15, after a successful `execute` | each agent's weekly strategy report for the week just scored |
| `backup` | every day, 17:30 | after `daily` |
| `decide` | the morning after the week's last trading day, 10:00 | normally Saturday |
| `check-logins` | every day 07:00, and Saturday 08:00 | 2 hours before decide |
| `update-calendar` | Sundays 12:00 | holidays / half days |

Every run is logged in the `job_runs` table and in `data/logs/`. After
downtime the scheduler catches up (up to 4 days back); a job that succeeded
never runs twice. A failing job is retried up to 3 times, 30 minutes apart
(each failure and retry sends an alert), then left for you.

## Day-to-day

```bash
docker compose ps                                  # health of every service
docker compose logs -f engine                      # the scheduler
docker compose exec engine python -m arena.main status
docker compose exec engine python -m arena.main health --scheduler
docker compose restart engine                      # after changing .env: docker compose up -d
docker compose exec engine python -m arena.main refresh-universe   # current S&P 500 list (a few times a year)
docker compose exec engine python -m arena.main reports --week 3 --force   # rewrite a strategy report
```

**S&P 500 changes.** `refresh-universe` downloads the current list (from the
`datasets/s-and-p-500-companies` dataset on GitHub, or `--source <csv>`),
records its date (shown on the dashboard and in the briefing) and prints
what was added and removed. New tickers get their price history right away.
A ticker that left the index is never force-sold: an agent holding it may
keep or reduce the position, not add to it, and it keeps getting prices.

**Data feed.** `ALPACA_DATA_FEED=iex` (free plan) or `sip` (paid). It's the
only place the feed is set; the dashboard shows which one is used.

Update: `git pull ~/league.bundle vps-deploy && docker compose up -d --build`.
The data and logins are in volumes and survive.

## Codex's sandbox in Docker

Codex's `--sandbox read-only` uses bubblewrap (Linux user namespaces). Tested
on the Ubuntu 24.04 VPS, it can't start inside a container: with Docker's
defaults it can't create a namespace; with seccomp **and** AppArmor both
unconfined it still fails setting up its network namespace ("Failed
RTM_NEWADDR"), with or without extra capabilities. Only `--privileged` would
work, and that would remove the container's own protection for everything
in it, so it's not used.

Instead, the Compose file sets `CODEX_SANDBOX=danger-full-access` for the
server services. This is safe here because Codex's shell, command runner,
patch tool, connectors, sub-agents, browser and plugins are all switched off
by `arena/agents/cli.py` (so the model can't run commands or edit files),
and the probe turn checks that no shell command was run. The boundary is the
container: non-root, all capabilities dropped, `no-new-privileges`, no
secrets in Codex's environment.

## Security

- Only Caddy is reachable from the internet. The dashboard and API need the
  password (scrypt hash in `.env`; signed, HttpOnly, Secure, SameSite=Strict
  session cookies; 5 wrong tries per 15 minutes per address). Every page is
  `noindex` and `/robots.txt` disallows everything.
- The setup page exists only on the server's loopback address (SSH tunnel).
- Agent-written code runs in the sandbox: separate process, no secrets,
  CPU/memory/process limits that are checked before any code runs (it refuses
  to run if one can't be enforced), and file, network and process access
  blocked inside Python.
- The agent CLIs never receive secrets other than Claude Code's own token.
- Containers run as a non-root user with all Linux capabilities dropped and
  `no-new-privileges`.
- `.env` stays on the server (`chmod 600`) and is never committed.

## Troubleshooting

| Symptom | Check |
|---|---|
| No certificate / browser warning | `docker compose logs caddy`; DNS A record; ports 80/443 open (`ufw status`) |
| Login page says "Too many attempts" | wait 15 minutes, or `docker compose restart api` |
| `api` keeps restarting | `docker compose logs api`: probably `ADMIN_TOKEN` too short or no `DASHBOARD_PASSWORD_HASH` |
| `engine` unhealthy | `docker compose logs engine`; `... arena.main health --scheduler` |
| Login alerts | step 8 |
| Setup page "only works on the computer running the league" | use the SSH tunnel and `localhost`, not the server IP |
