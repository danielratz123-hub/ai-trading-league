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
