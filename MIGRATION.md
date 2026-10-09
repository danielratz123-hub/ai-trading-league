# Moving the league from your PC to the server

This moves the running league (scores, positions, history, settings) from
the Windows PC to the VPS. It takes about 30 minutes. Read it once to the
end before you start.

> **⚠️ Never run the PC and the server at the same time.**
> **The scheduler's lock only works on one machine: it can't see a scheduler
> on another computer. Both would run decide and execute on their own copy
> of the league, and both would place orders in the same Alpaca paper
> account. Before the server starts, the PC must be fully stopped, and it
> must stay stopped.**

---

## When

**On a weekend: after Saturday's decide has finished, and before Monday
09:50 ET** (the first execute). Saturday afternoon is ideal: the agents have
made their decisions on the PC, and the server only has to execute them on
Monday.

Check on the PC that decide is done. Open the setup page (start.cmd) and
look at "Recent jobs", or run:

```powershell
cd $HOME\Desktop\ai-trading-league
.venv\Scripts\python -m arena.main status
```

The latest week must be **decided** (not "deciding"), and both agents' turns
must be finished.

## Before the weekend: prepare the server

Follow **DEPLOY.md steps 1 to 8**: server, hardening, Docker, code, `.env`,
DNS, build, sign the agents in. Check that `check-agents --probe` says OK
for both. That `docker compose up -d` in step 7 creates an **empty**
database on the server; that's expected, and it is replaced below.

Then stop the server's app until the migration:

```bash
cd ~/league && docker compose stop
```

## 1. Stop the PC completely

1. Close the **start.cmd** window (or press Ctrl+C in it). That stops the
   app and the scheduler.
2. If you ever added the Windows Task Scheduler task from the old guide,
   disable it (PowerShell):
   ```powershell
   schtasks /Change /TN "AI Trading League" /DISABLE
   ```
3. Check nothing is left (PowerShell). The list must be empty:
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -match 'arena' | Select-Object ProcessId, CommandLine
   ```
   If something is listed: `Stop-Process -Id <ProcessId>`.
4. In the PC's `.env`, set `AUTOSTART_SCHEDULER=0`, so opening start.cmd by
   habit later can't start a scheduler.

## 2. Checkpoint the database and record the scoreboard

The database uses WAL mode: recent changes can still be in
`data\arena.db-wal`. A checkpoint writes them into `arena.db`, so that one
file is complete. In PowerShell, in the project folder:

```powershell
.venv\Scripts\python -c "import sqlite3; c=sqlite3.connect('data/arena.db'); print('checkpoint', c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

It must print `checkpoint (0, 0, 0)` and `integrity ok`. If the first number
isn't 0, something still has the database open: go back to step 1.
Afterwards `data\arena.db-wal` is 0 bytes or gone.

Record the league as it is now, to compare later:

```powershell
.venv\Scripts\python -m arena.main status > status-pc.txt
```

Also take a screenshot of the dashboard's scoreboard.

## 3. Copy the database and `.env` to the server

```powershell
scp data\arena.db daniel@SERVER_IP:~/arena.db
scp .env daniel@SERVER_IP:~/pc.env
scp status-pc.txt daniel@SERVER_IP:~/status-pc.txt
```

## 4. Put them in place on the server

**`.env`**: the server's `.env` already has the server-only settings
(`DOMAIN`, `DASHBOARD_PASSWORD_HASH`, `ADMIN_TOKEN`, Telegram, backups,
`CLAUDE_CODE_OAUTH_TOKEN`). Copy over from `~/pc.env` what the league uses:
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `CLAUDE_MODEL`, `GPT_MODEL`,
`REASONING_EFFORT`, `MIRROR_MODE` and any `ALPACA_MIRROR_*` keys. The models
and effort **must** match the PC, or the agents change mid-league. Then:

```bash
nano ~/league/.env                   # copy the values in
rm ~/pc.env                          # it contains keys; don't leave a copy lying around
```

**The database**, into the `arena-data` volume (removing the empty one from
step 7, including its -wal/-shm files):

```bash
cd ~/league
docker compose stop
docker compose run --rm --no-deps -v ~/arena.db:/tmp/arena.db:ro engine sh -c '
  rm -f /app/data/arena.db /app/data/arena.db-wal /app/data/arena.db-shm &&
  cp /tmp/arena.db /app/data/arena.db &&
  sqlite3 /app/data/arena.db "PRAGMA integrity_check"'
```

It must print `ok`.

## 5. Start on the server and check

```bash
docker compose up -d
sleep 60
docker compose ps                                                  # all running, (healthy)
docker compose exec engine python -m arena.main health --scheduler # "ok": true
docker compose exec engine python -m arena.main status > ~/status-server.txt
diff ~/status-pc.txt ~/status-server.txt && echo "SCOREBOARD MATCHES"
docker compose exec engine python -m arena.main check-agents --probe
docker compose exec engine python -m arena.main backup
docker compose exec engine python -m arena.main test-alert
```

- `diff` must print nothing and then `SCOREBOARD MATCHES`. (If the only
  differences are lines with the current time, that's fine.)
- Open `https://<your domain>`, sign in, and compare the scoreboard with
  your screenshot: same equity, points and positions.
- In the setup page (SSH tunnel, DEPLOY.md step 10), "Recent jobs" shows
  the PC's job history, including Saturday's successful decide. The server
  won't redo it.

When everything matches:

- Rename the PC's database so start.cmd can never pick it up again:
  ```powershell
  Rename-Item data\arena.db "arena.db.migrated-$(Get-Date -Format yyyy-MM-dd)"
  ```
- Delete `~/arena.db` on the server (the volume has its own copy):
  `rm ~/arena.db`.

On Monday at 09:50 ET you should get the execute summary on Telegram.

## Rolling back to the PC

Only one machine may run at a time, so always **stop one before starting
the other**.

**Before Monday 09:50 (the server hasn't traded):**

```bash
cd ~/league && docker compose stop        # NOT "down -v": that deletes the volumes
```

On the PC, rename the database back (`arena.db.migrated-...` → `arena.db`),
set `AUTOSTART_SCHEDULER` back the way it was, and double-click start.cmd.
Nothing was lost: the PC's copy is exactly where you left it.

**After the server has traded:** the server's database is now the newest.
Bring it back to the PC:

```bash
cd ~/league
docker compose stop engine api setup
mkdir -p /tmp/arena-out && chmod 777 /tmp/arena-out
docker compose run --rm --no-deps -v /tmp/arena-out:/out engine sh -c '
  sqlite3 /app/data/arena.db "PRAGMA wal_checkpoint(TRUNCATE);" &&
  sqlite3 /app/data/arena.db "PRAGMA integrity_check;" &&
  cp /app/data/arena.db /out/arena-for-pc.db'
```

It must print `0|0|0` and `ok`.

```powershell
scp daniel@SERVER_IP:/tmp/arena-out/arena-for-pc.db data\arena.db
```

Then on the server: `rm -r /tmp/arena-out`.

Then start start.cmd on the PC, and keep the server stopped
(`docker compose stop`) until you migrate again.
