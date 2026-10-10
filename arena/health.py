"""Health checks used by Docker, the /healthz endpoint and the alerts.

Cheap and read-only: nothing here calls an AI or Alpaca. (The AI login
check is separate, in league/scheduler, because it uses a little plan
usage.)
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from pathlib import Path

HEARTBEAT_FILE = "scheduler.heartbeat"
HEARTBEAT_EVERY = 30          # seconds
HEARTBEAT_STALE = 180         # older than this = the scheduler is stuck or gone
LOW_DISK_GB = 2.0


def heartbeat_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / HEARTBEAT_FILE


def start_heartbeat(db_path: str | Path, stop: threading.Event) -> threading.Thread:
    """Touch the heartbeat file every 30 s from a side thread, so it keeps
    beating during a long job (an agent turn can take 30 minutes)."""
    path = heartbeat_path(db_path)

    def beat() -> None:
        while not stop.is_set():
            try:
                path.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass   # a full disk shows up in the disk check instead
            stop.wait(HEARTBEAT_EVERY)

    t = threading.Thread(target=beat, name="heartbeat", daemon=True)
    t.start()
    return t


def heartbeat_age(db_path: str | Path) -> float | None:
    try:
        return time.time() - float(heartbeat_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def disk_free_gb(db_path: str | Path) -> float:
    return shutil.disk_usage(Path(db_path).resolve().parent).free / 1e9


def check(db_path: str | Path, scheduler: bool = False) -> dict:
    """{'ok': bool, 'problems': [...], ...details}."""
    problems: list[str] = []
    path = Path(db_path)
    db_ok = False
    if path.exists():
        try:
            conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                conn.execute("SELECT 1 FROM league_state LIMIT 1").fetchall()
                db_ok = True
            finally:
                conn.close()
        except sqlite3.Error as e:
            problems.append(f"database not readable: {e}")
    else:
        problems.append("database file is missing")
    age = heartbeat_age(db_path)
    if scheduler and (age is None or age > HEARTBEAT_STALE):
        problems.append("scheduler heartbeat is " + ("missing" if age is None else f"{age:.0f}s old"))
    free = disk_free_gb(db_path) if path.parent.exists() else 0.0
    if free < LOW_DISK_GB:
        problems.append(f"low disk space: {free:.1f} GB free")
    return {"ok": not problems, "problems": problems, "db": db_ok,
            "scheduler_heartbeat_age_s": None if age is None else round(age),
            "disk_free_gb": round(free, 1)}


# --------------------------------------------------------------------------
# Outside monitoring (ROADMAP 0.9)
# --------------------------------------------------------------------------
# A monitor on this server dies with it. So the scheduler pings an outside
# service (e.g. Healthchecks.io, free) every PING_EVERY seconds; if the pings
# stop (server down, engine stuck) or report a failure, THAT service alerts
# you. Set HEALTHCHECK_PING_URL in .env to the check's ping URL.

PING_EVERY = 300              # seconds
ALPACA_CHECK_EVERY = 1800     # seconds


def alpaca_reachable() -> str | None:
    """None if Alpaca's API answers with these keys, else the problem."""
    try:
        from alpaca.trading.client import TradingClient

        from .config import require_env
        TradingClient(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"), paper=True).get_clock()
        return None
    except Exception as e:  # noqa: BLE001
        return f"Alpaca not reachable: {type(e).__name__}: {str(e)[:150]}"


def ping_once(url: str, db_path: str | Path, alpaca_problem: str | None, post=None) -> bool:
    """One ping: success, or <url>/fail with the problems as the body."""
    h = check(db_path, scheduler=True)
    problems = h["problems"] + ([alpaca_problem] if alpaca_problem else [])
    if post is None:
        import httpx
        post = lambda u, body: httpx.post(u, content=body.encode("utf-8"), timeout=10)  # noqa: E731
    try:
        post(url.rstrip("/") + ("/fail" if problems else ""), "; ".join(problems) or "ok")
        return not problems
    except Exception:  # noqa: BLE001 - a failed ping is exactly what the service notices
        return False


def start_outside_ping(db_path: str | Path, stop: threading.Event, url: str | None) -> threading.Thread | None:
    if not url:
        return None

    def loop() -> None:
        alpaca_problem, last_alpaca = None, 0.0
        while not stop.is_set():
            if time.time() - last_alpaca >= ALPACA_CHECK_EVERY:
                alpaca_problem, last_alpaca = alpaca_reachable(), time.time()
            ping_once(url, db_path, alpaca_problem)
            stop.wait(PING_EVERY)

    t = threading.Thread(target=loop, name="outside-ping", daemon=True)
    t.start()
    return t
