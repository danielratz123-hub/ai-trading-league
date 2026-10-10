"""Runs the league on US market time, forever:

    execute   every trading day at 09:50 ET     (fills at the open)
    daily     every trading day, 45 min after the close (16:45, or 13:45 on half days)
    decide    the morning after the week's last trading day (normally Saturday 10:00)
    calendar  Sundays at noon (refreshes holidays)
    check-logins  every day at 07:00, and 2 hours before every decide (08:00)
    backup    every day at 17:30 (after daily)
    reconcile trading days at 17:00: Alpaca paper account vs the league's records
    heartbeat every day at 18:00: a "still alive" Telegram message
    morning-report  Mon-Sat at 08:00: profit/loss status (the week's on Saturday)
    reports   trading days from 10:15, once execute has succeeded (weekly strategy reports)

With LEARNING_ENABLED=1 (arena/learning, times from config/learning.yaml):
    risk-check     trading days, hourly 10:00-15:30 (each within 30 minutes of its time, never replayed later)
    daily-review   trading days at 16:30, once that day's `daily` has succeeded
    weekly-review  Saturday 12:00; `decide` then waits for it (or for 4 hours, if it keeps failing)
    + the 1-minute bar collector, a thread inside this process (not a job)

    python -m arena.main scheduler

Times are computed in America/New_York with zoneinfo, so daylight saving is
handled automatically, and trading days / half days come from the Alpaca
calendar, so holidays are skipped. Each job runs as its own process
(`python -m arena.main <job> --date ...`): a crash in one job can't take the
scheduler down. Every run is recorded in the job_runs table, which makes the
scheduler safe to restart: it catches up on what it missed and never runs a
successful job twice.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT, SETTINGS
from .db import connect, transaction
from .prices import ET

log = logging.getLogger("arena.scheduler")

EXECUTE_AT = (9, 50)                 # Alpaca's free data plan lags 15 minutes
DAILY_AFTER_CLOSE = timedelta(minutes=45)
DECIDE_AT = (10, 0)                  # the morning after the week's last close
CALENDAR_AT = (12, 0)                # Sundays
LOGIN_CHECK_AT = (7, 0)              # daily
BACKUP_AT = (17, 30)                 # daily, after the daily job (16:45)
REPORTS_AT = (10, 15)                # after execute (09:50) has scored the week
RECONCILE_AT = (17, 0)               # trading days, after the close
HEARTBEAT_AT = (18, 0)               # daily "still alive" message
MORNING_REPORT_AT = (8, 0)           # Telegram status: the last day (Mon-Fri), the week (Saturday)
MORNING_REPORT_LATEST = (12, 0)      # not sent after this (a late "morning" report is just noise)
DISK_CHECK_EVERY = timedelta(hours=1)
PRE_DECIDE_CHECK = timedelta(hours=2)
TEMPFAIL = 75                        # "retry until fixed" (a login problem at decide)
CATCH_UP_DAYS = 4                    # don't replay days older than this after downtime
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
TIMEOUTS = {"decide": 3 * 3600, "execute": 1800, "daily": 3600, "update-calendar": 600,
            "backup": 1800, "check-logins": 1200, "reports": 3600, "reconcile": 600, "heartbeat": 300, "morning-report": 300,
            "risk-check": 1800, "daily-review": 3600, "weekly-review": 3600}
RISK_CHECK_WINDOW = timedelta(minutes=30)   # a risk check later than this is skipped, not replayed
WEEKLY_REVIEW_GRACE = timedelta(hours=4)    # decide stops waiting for a failing weekly review


@dataclass(frozen=True)
class LearningTimes:
    """The learning layer's schedule (config/learning.yaml), or None when it's off."""
    risk_first: tuple[int, int]
    risk_every_minutes: int
    risk_last: tuple[int, int]
    daily_review: tuple[int, int]
    weekly_weekday: int                 # Monday = 0
    weekly_review: tuple[int, int]

    def risk_times(self, close_hm: tuple[int, int]) -> list[tuple[int, int]]:
        limit = min(self.risk_last[0] * 60 + self.risk_last[1], close_hm[0] * 60 + close_hm[1] - 30)
        first = self.risk_first[0] * 60 + self.risk_first[1]
        out = list(range(first, limit + 1, self.risk_every_minutes))
        if limit >= first and limit not in out:
            out.append(limit)
        return [(m // 60, m % 60) for m in out]

    def review_at(self, week_end: str) -> datetime:
        """The weekly review after a week that ended on week_end."""
        d = date.fromisoformat(week_end) + timedelta(days=1)
        while d.weekday() != self.weekly_weekday:
            d += timedelta(days=1)
        return _at(d.isoformat(), self.weekly_review)


def learning_times() -> LearningTimes | None:
    from .learning import config as lconfig
    if not lconfig.enabled():
        return None
    s = lconfig.get().schedule
    return LearningTimes(s.risk_first, s.risk_every_minutes, s.risk_last, s.daily_review,
                         s.weekly_review_weekday, s.weekly_review)



@dataclass(frozen=True)
class Job:
    name: str          # a main.py command
    key: str           # what it's for (a date, or a week label)
    date: str | None   # passed as --date

    def argv(self, db_path: str | Path) -> list[str]:
        args = [sys.executable, "-m", "arena.main", self.name, "--db", str(db_path)]
        if self.name == "check-logins":
            args += ["--reason", self.key]
        return args + (["--date", self.date] if self.date else [])


# --------------------------------------------------------------------------
# Deciding what's due (pure: easy to test)
# --------------------------------------------------------------------------

def _at(d: str, hm: tuple[int, int]) -> datetime:
    return datetime.fromisoformat(d).replace(hour=hm[0], minute=hm[1], tzinfo=ET)


def _close(d: str, calendar: dict[str, str]) -> datetime:
    h, m = map(int, calendar[d].split(":"))
    return _at(d, (h, m))


def due_jobs(now: datetime, calendar: dict[str, str], history: dict[tuple[str, str], list[dict]],
             league_started: bool, learning: LearningTimes | None = None) -> list[Job]:
    """Jobs that should run now, in the order to run them.

    calendar: trading day -> close time "HH:MM" (Eastern)
    history:  (job, key) -> previous runs [{"exit_code": int|None, "started_at": datetime}]
    learning: the learning layer's times, or None when it is off
    """
    now = now.astimezone(ET)
    today = now.date().isoformat()
    days = sorted(calendar)
    past = [d for d in days if d <= today]
    jobs: list[Job] = []

    def wants(job: str, key: str) -> bool:
        runs = history.get((job, key), [])
        if any(r["exit_code"] == 0 for r in runs):
            return False
        # Login problems (exit 75) don't use up attempts: decide keeps
        # retrying every 30 minutes until its window closes.
        if sum(1 for r in runs if r["exit_code"] != TEMPFAIL) >= MAX_ATTEMPTS:
            return False                       # give up; a human should look at it
        return not runs or now - runs[-1]["started_at"] >= RETRY_AFTER

    def add(job: str, d: str | None, key: str | None = None) -> None:
        key = key or d
        if wants(job, key):
            jobs.append(Job(job, key, d))

    # Calendar refresh: Sundays at noon, or right away if today isn't in it.
    year, week, weekday = now.isocalendar()
    if not days or days[-1] < today:
        add("update-calendar", None, f"missing-{today}")
    elif weekday == 7 and now >= _at(today, CALENDAR_AT):
        add("update-calendar", None, f"{year}-W{week:02d}")

    if not league_started:
        return jobs        # kickoff is a manual, one-off command

    # Yesterday's (or the last trading day's) daily, if it was missed.
    prev = [d for d in past if d < today]
    recent = prev and (now.date() - date.fromisoformat(prev[-1])).days <= CATCH_UP_DAYS
    if recent and now >= _close(prev[-1], calendar) + DAILY_AFTER_CLOSE:
        add("daily", prev[-1])

    if today in calendar:
        if _at(today, EXECUTE_AT) <= now < _close(today, calendar):
            add("execute", today)
        if learning:
            close_hm = tuple(map(int, calendar[today].split(":")))
            for hm in learning.risk_times(close_hm):
                if _at(today, hm) <= now < _at(today, hm) + RISK_CHECK_WINDOW:
                    add("risk-check", today, f"risk-{today}-{hm[0]:02d}{hm[1]:02d}")
        if now >= _close(today, calendar) + DAILY_AFTER_CLOSE:
            add("daily", today)
        daily_ok = any(r["exit_code"] == 0 for r in history.get(("daily", today), []))
        if learning and daily_ok and now >= _at(today, learning.daily_review):
            add("daily-review", today, f"daily-review-{today}")

    # decide: for the most recent completed week, between the morning after
    # its last trading day and the next trading day's open.
    week_end = _last_completed_week_end(days, today, now, calendar)
    if week_end:
        nxt = next((d for d in days if d > week_end), None)
        start = _at((date.fromisoformat(week_end) + timedelta(days=1)).isoformat(), DECIDE_AT)
        end = _at(nxt, (9, 30)) if nxt else start + timedelta(days=3)
        if start - PRE_DECIDE_CHECK <= now < start:
            add("check-logins", None, f"pre-decide-{week_end}")
        if learning:
            # The weekly review runs first; decide follows once it succeeded,
            # gave up, or has been failing for WEEKLY_REVIEW_GRACE.
            review_at = learning.review_at(week_end)
            key = f"weekly-review-{week_end}"
            if review_at <= now < end:
                add("weekly-review", review_at.date().isoformat(), key)
            runs = history.get(("weekly-review", key), [])
            finished = (any(r["exit_code"] == 0 for r in runs)
                        or sum(1 for r in runs if r["exit_code"] != TEMPFAIL) >= MAX_ATTEMPTS
                        or now >= review_at + WEEKLY_REVIEW_GRACE)
            if finished and start <= now < end:
                add("decide", week_end)
        elif start <= now < end:
            add("decide", week_end)

    # Strategy reports: after a successful execute (which scores the week).
    executed = any(r["exit_code"] == 0 for r in history.get(("execute", today), []))
    if today in calendar and executed and now >= _at(today, REPORTS_AT):
        add("reports", None, f"reports-{today}")

    if today in calendar and now >= _at(today, RECONCILE_AT):
        add("reconcile", None, f"reconcile-{today}")

    if now >= _at(today, BACKUP_AT):
        add("backup", None, f"backup-{today}")

    # "Still alive": if this message doesn't arrive, something is wrong.
    if now >= _at(today, HEARTBEAT_AT):
        add("heartbeat", None, f"heartbeat-{today}")

    if weekday != 7 and _at(today, MORNING_REPORT_AT) <= now < _at(today, MORNING_REPORT_LATEST):
        add("morning-report", today, f"morning-{today}")

    # Login health check, daily. Last in the list so it never delays
    # execute or daily (a probe turn can take a few minutes).
    if now >= _at(today, LOGIN_CHECK_AT):
        add("check-logins", None, f"daily-{today}")
    return jobs


def _last_completed_week_end(days: list[str], today: str, now: datetime, calendar: dict[str, str]) -> str | None:
    """Last trading day of the latest trading week whose final close has passed."""
    for i in range(len(days) - 1, -1, -1):
        d = days[i]
        if d > today or (d == today and now < _close(d, calendar)):
            continue
        nxt = days[i + 1] if i + 1 < len(days) else None
        same_week = nxt and date.fromisoformat(nxt).isocalendar()[:2] == date.fromisoformat(d).isocalendar()[:2]
        if not same_week:
            return d
    return None


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def _history(conn) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for r in conn.execute("SELECT * FROM job_runs ORDER BY id"):
        out.setdefault((r["job"], r["key"]), []).append(
            {"exit_code": r["exit_code"], "started_at": datetime.fromisoformat(r["started_at"])})
    return out


def _calendar(conn) -> dict[str, str]:
    return {r["date"]: r["close_time"] for r in conn.execute("SELECT date, close_time FROM calendar")}


def run_job(conn: sqlite3.Connection, job: Job, log_dir: Path, db_path: str | Path) -> int:
    started = datetime.now(ET)
    with transaction(conn):
        run_id = conn.execute("INSERT INTO job_runs(job, key, started_at) VALUES (?,?,?)",
                              (job.name, job.key, started.isoformat())).lastrowid
    log.info("starting %s %s", job.name, job.key)
    log_file = log_dir / f"{started:%Y-%m-%d}_{job.name}.log"
    try:
        p = subprocess.run(job.argv(db_path), cwd=PROJECT_ROOT, capture_output=True, text=True,
                           timeout=TIMEOUTS.get(job.name, 3600), encoding="utf-8", errors="replace")
        code, output = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        code, output = 124, f"timed out after {e.timeout}s"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"=== {started.isoformat()} {' '.join(job.argv(db_path)[2:])} -> exit {code}\n{output}\n")
    with transaction(conn):
        conn.execute("UPDATE job_runs SET finished_at=?, exit_code=?, output_tail=? WHERE id=?",
                     (datetime.now(ET).isoformat(), code, output[-4000:], run_id))
    (log.info if code == 0 else log.error)("%s %s finished with exit code %s", job.name, job.key, code)
    _alert_job(conn, job, code, output)
    return code


def _alert_job(conn, job: Job, code: int, output: str) -> None:
    """Telegram: every failed run, and a note when a retry succeeds."""
    from . import alerts
    try:
        runs = conn.execute("SELECT exit_code FROM job_runs WHERE job=? AND key=? ORDER BY id",
                            (job.name, job.key)).fetchall()
        earlier = [r[0] for r in runs[:-1]]
        if code == 0:
            if earlier:
                alerts.send(f"Job {job.name} ({job.key}) succeeded on retry {len(earlier)}.", level="good")
            return
        if code == TEMPFAIL:
            return       # login problem: league.decide already alerted (throttled)
        hard = sum(1 for c in earlier + [code] if c != TEMPFAIL)
        tail = "\n".join(output.strip().splitlines()[-6:])[-800:]
        source = alerts.api_source(output)
        nxt = (f"Will retry in {int(RETRY_AFTER.total_seconds() // 60)} minutes (attempt {hard}/{MAX_ATTEMPTS})."
               if hard < MAX_ATTEMPTS else
               f"Gave up after {MAX_ATTEMPTS} attempts: please look at it (data/logs, docker compose logs engine).")
        head = f"{source} API error: job" if source else "Job"
        alerts.send(f"{head} {job.name} ({job.key}) FAILED with exit code {code}. {nxt}\n{tail}", level="error")
    except Exception:  # noqa: BLE001 - alerts must never break the scheduler
        log.exception("could not send the job alert")


def _check_disk(db_path) -> None:
    from . import alerts
    from .health import LOW_DISK_GB, disk_free_gb
    try:
        free = disk_free_gb(db_path)
        if free < LOW_DISK_GB:
            alerts.send(f"Low disk space on the server: {free:.1f} GB free. Backups and the database "
                        f"need room; clean up with `docker system prune` or grow the disk.",
                        level="error", key="low-disk", every=timedelta(hours=6))
    except Exception:  # noqa: BLE001
        log.exception("disk check failed")


def _market_open(conn, now: datetime) -> bool:
    row = conn.execute("SELECT open_time, close_time FROM calendar WHERE date=?", (now.date().isoformat(),)).fetchone()
    if row is None:
        return False
    hm = now.strftime("%H:%M")
    return row["open_time"] <= hm <= row["close_time"]


def _collector_loop(db_path, stop: threading.Event) -> None:
    """Every minute while the market is open: 1-minute bars (learning layer).
    Its own connection; a failure is logged (alert at most hourly) and the
    next minute simply tries again, catching up the gap."""
    from . import alerts
    from .learning.collector import collect_minute
    from .prices import AlpacaPriceSource
    conn = connect(db_path)
    source = None
    while not stop.is_set():
        now = datetime.now(ET)
        if _market_open(conn, now):
            try:
                source = source or AlpacaPriceSource()
                collect_minute(conn, source.minute_bars)
            except Exception as e:  # noqa: BLE001
                log.warning("collector: %s: %s", type(e).__name__, e)
                alerts.send(f"1-minute collector failing: {type(e).__name__}: {str(e)[:300]}", level="warning",
                            key="collector", every=timedelta(hours=1))
        # Wake 5 seconds into the next minute (the bar just closed).
        stop.wait(max(5.0, 65 - datetime.now(ET).second))
    conn.close()


class AlreadyRunning(RuntimeError):
    pass


def single_instance_lock(db_path: str | Path, wait_seconds: float = 5.0):
    """Hold an exclusive OS lock on <data>/scheduler.lock for as long as this
    process lives. Two schedulers on one league could run the same job at the
    same moment, so a second one refuses to start.

    The lock belongs to the process, not to the file: the kernel drops it when
    the process ends, even on a crash or `docker kill`, so there is never a
    stale lock to clean up (the empty file staying behind is harmless).
    Windows releases a dead process's lock "when resources allow", usually
    within a second, hence the short retry before giving up.

    This only protects one machine. It can't see a scheduler on another
    computer (see MIGRATION.md)."""
    path = Path(db_path).resolve().parent / "scheduler.lock"
    f = open(path, "a+")
    f.seek(0)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f   # keep a reference: closing it releases the lock
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                raise AlreadyRunning("Another scheduler is already running for this league.") from None
            time.sleep(0.25)


_STOP = threading.Event()


def _request_stop(signum, frame) -> None:
    """SIGTERM (docker stop, systemd, the app shutting down): finish the job
    that is running, if any, then exit. Every job is safe to re-run, so even
    a hard kill only means the job runs again after the restart."""
    log.info("stop requested (signal %s); exiting after the current job", signum)
    _STOP.set()


def _die_with_parent() -> None:
    """When the dashboard app started us (ARENA_PARENT_PID), stop when it
    stops, so closing the app never leaves a scheduler running on its own.
    Windows does this with a Job Object in control.py. On Linux the kernel
    sends us SIGTERM when the parent dies (PR_SET_PDEATHSIG); the per-tick
    parent check below covers other systems and the race at startup."""
    if not os.getenv("ARENA_PARENT_PID") or not sys.platform.startswith("linux"):
        return
    import ctypes
    import signal
    PR_SET_PDEATHSIG = 1
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)


def _parent_gone() -> bool:
    want = os.getenv("ARENA_PARENT_PID")
    return bool(want) and os.name != "nt" and os.getppid() != int(want)


def run_forever(db_path: str | Path = SETTINGS.db_path, tick_seconds: int = 30) -> None:
    lock = single_instance_lock(db_path)   # noqa: F841 - held until the process exits
    import signal
    signal.signal(signal.SIGTERM, _request_stop)
    _die_with_parent()
    from .health import start_heartbeat
    start_heartbeat(db_path, _STOP)        # Docker's healthcheck watches this file
    conn = connect(db_path)
    log_dir = Path(db_path).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log.info("scheduler started (US/Eastern now %s); logs in %s", datetime.now(ET).strftime("%Y-%m-%d %H:%M"), log_dir)
    from . import alerts
    alerts.send(f"Scheduler started ({datetime.now(ET):%a %Y-%m-%d %H:%M} ET). If you didn't restart it, "
                f"the server or container restarted; missed jobs are caught up automatically.",
                level="info", key="scheduler-start", every=timedelta(minutes=10))
    last_disk_check = datetime.min.replace(tzinfo=ET)
    learning = None
    try:
        learning = learning_times()
    except Exception:  # noqa: BLE001 - a broken learning config must not stop the league
        log.exception("learning layer config error; its jobs are OFF until it is fixed")
        alerts.send("Learning layer: config/learning.yaml could not be read; its jobs are off. The league runs "
                    "as before.", level="error", key="learning-config", every=timedelta(hours=6))
    if learning:
        threading.Thread(target=_collector_loop, args=(db_path, _STOP), name="collector", daemon=True).start()
        log.info("learning layer on: risk checks, daily/weekly reviews and the 1-minute collector")
    while not _STOP.is_set() and not _parent_gone():
        if datetime.now(ET) - last_disk_check >= DISK_CHECK_EVERY:
            _check_disk(db_path)
            last_disk_check = datetime.now(ET)
        started = conn.execute("SELECT 1 FROM agents LIMIT 1").fetchone() is not None
        for job in due_jobs(datetime.now(ET), _calendar(conn), _history(conn), started, learning):
            if _STOP.is_set():
                break
            run_job(conn, job, log_dir, Path(db_path).resolve())
        _STOP.wait(tick_seconds)
    log.info("scheduler stopped")
    conn.close()
