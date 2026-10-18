# Security

What protects the league, what was changed and when, and what is still open.
Updated at the end of every hardening stage. Secrets are never written here.

## Setup at a glance

- **Server:** Contabo VPS, Ubuntu 24.04, admin user `daniel` (sudo with a
  password, SSH key only). Tailscale is installed (Tailscale SSH on).
- **Public surface:** Caddy on 80/443 only (Let's Encrypt, HTTP→HTTPS). SSH
  on 22. Everything else is internal: the app listens on the Docker network
  and on `127.0.0.1:8000` (setup page, through an SSH tunnel only).
- **App:** Docker Compose: `caddy`, `api` (dashboard + API), `engine`
  (scheduler, agents), `setup`. The app containers run as the non-root user
  `arena`, with all capabilities dropped and `no-new-privileges`.
- **Data:** SQLite in the `arena-data` volume (no network port). The API
  opens it read-only.
- **Secrets:** `.env` on the server (`chmod 600`), never committed (git
  history checked 2026-10-18: clean). The GitHub repo is private.
- **Dashboard:** everything behind a password login (scrypt, rate-limited,
  HttpOnly/Secure/SameSite=Strict session cookie). Admin actions also need
  `ADMIN_TOKEN`.

## Stage 0 — audit (2026-10-18, read-only)

Already in place before this work: SSH keys only, root login off,
`MaxAuthTries 3`, ufw active, fail2ban (sshd), unattended-upgrades (incl.
`-security`), only 22/80/443 public, non-root containers, HTTPS + HSTS +
nosniff + X-Frame-Options + Referrer-Policy, daily backups to Google Drive,
risk limits in code, `paper=True` hardcoded, a kill switch, secrets
stripped from the agents' environment, Landlock write jail for agent CLIs,
no secrets in logs, `pip-audit` clean (all 49 installed packages).

| # | Sev | Finding | Status |
|---|---|---|---|
| H1 | High | Agent-code sandbox not isolated on the server: Docker blocks `unshare`, so the child had network access; it ran as the scheduler's user and could read `/proc/1/environ` (all secrets); only a Python audit hook stood in the way | **Fixed in code** (Stage 1+, below); deploy pending |
| M1 | Medium | Backups unencrypted on Google Drive (`drive` remote, not `crypt`); agent reports/logs not included; no scheduled restore test | Open → Stage 3 |
| M2 | Medium | Kill switch didn't stop Alpaca: `daily` runs while paused and mirrors stop-losses; no cancel of open orders | **Fixed in code** (`halt`); deploy pending |
| M3 | Medium | No Content-Security-Policy | Open → Stage 4 |
| M4 | Medium | Single-factor dashboard login; admin endpoints reachable from the internet | Open → Stage 4 (Tailscale-only admin, TOTP before going public) |
| M5 | Medium | Cloud image's `ubuntu` user in `sudo` + `lxd` | Script ready (`scripts/harden-host.sh`) |
| L1 | Low | HSTS without `includeSubDomains`; `www` resolves to the server but has no certificate | Open → Stage 4 |
| L2 | Low | Global login limit lets an attacker lock you out; 7-day stateless sessions | Open → Stage 4 |
| L3 | Low | SSH: `X11Forwarding yes`, no `AllowUsers`, main config still says `PermitRootLogin yes` (overridden) | Script ready |
| L4 | Low | Pinned images (Node, Caddy, Python) and agent CLIs never updated | Open → Stage 3/4 |
| L5 | Low | Only `/login` is rate-limited (rest is behind the login) | Open → Stage 4 |
| L6 | Low | Docker port mappings bypass ufw (only 80/443 today) | Rule: bind any new port to `127.0.0.1:` |

## Stage 1 — server hardening

`scripts/harden-host.sh` (run by you with `sudo`; backs up every file to
`.bak-<date>`, tests the config before reloading, never touches the
firewall or Docker):

- SSH: `AllowUsers daniel`, no X11 / agent forwarding, the main config's
  leftover `PermitRootLogin yes` → `no`. TCP forwarding stays on (setup
  page tunnel). Reloaded, not restarted.
- `ubuntu` user: locked, expired, out of `sudo` and `lxd`, keys set aside.
- fail2ban: Tailscale and localhost are never banned; `recidive` jail bans
  repeat offenders for 7 days.

Already true, verified in Stage 0: unattended-upgrades, ufw default-deny
with 22/80/443, fail2ban sshd, internal services on 127.0.0.1 / the Docker
network only.

**Decision left open:** the containers run as uid `APP_UID`, which equals
`daniel`'s uid so the setup page can edit `.env`. A container escape would
therefore land as `daniel` (who is in the `docker` group, i.e. effectively
root). A separate uid with no host account would close that, at the cost
of editing `.env` with sudo. Not changed yet.

## H1 — sandbox kernel lockdown (code, 2026-10-19)

`arena/sandbox/child.py`, Linux: after loading the price data and before
any agent code runs:

- **Landlock:** read-only access to Python's own library folders and system
  libraries only. No `/proc` (other processes' secrets), no `/app/data`, no
  CLI logins or rclone config, no writes anywhere. On ABI 4+ (the server
  has 4) no TCP bind/connect either.
- **seccomp:** `socket`, `socketpair`, `ptrace`, `process_vm_readv/writev`
  and io_uring return EPERM; on x86_64 the x32 syscall ABI is refused; any
  other architecture is killed.
- Both are **proven** right after they are applied (reading `/proc`,
  writing a file and opening a socket must all fail); otherwise the sandbox
  refuses to run the code, as it already did for missing rlimits.
- The audit hook and import allowlist stay as extra layers.

Verified: `arena/tests/test_sandbox.py` passes (21/21) inside the
production image on the server, in a throwaway container with the same
restrictions as production.

## M2 — emergency stop (code, 2026-10-19)

`python -m arena.main halt` (or `POST /api/admin/halt` with the
ADMIN_TOKEN): global pause + a `halted` flag that every mirror entry point
checks before sending anything to Alpaca + cancel all open orders in each
mirrored paper account + Telegram alert. `resume` lifts it. `status` shows
`[LEAGUE HALTED]`. Tests: `arena/tests/test_mirror.py`.

## How to verify

```bash
# on the server, after deploy
docker compose exec engine python -m arena.main status
docker compose run --rm --no-deps engine python -m pytest -q arena/tests/test_sandbox.py
# after harden-host.sh
sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|x11forwarding|allowusers) '
sudo fail2ban-client status
```

## Rules

- Never print keys, passwords or tokens in full.
- Every config change on the server: `.bak-<date>` copy first.
- No new published Docker ports except through Caddy; internal ones bind to
  `127.0.0.1`.
- Risk limits live in code (`arena/risk.py`, `arena/learning/hardlimits.py`),
  never only in a prompt.
