"""The hub's own small SQLite database: tasks and their timeline."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# Task status, in order of a normal life:
#   queued -> building -> reviewing -> (fixing -> reviewing)* -> ready -> merged
# Off the happy path: needs_joe (stopped, waiting for Joe), failed, cancelled.
ACTIVE = ("queued", "building", "fixing", "reviewing")
RUNNING = ("building", "fixing")
FINAL = ("merged", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    done_when   TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    issue       INTEGER,
    branch      TEXT,
    pr          INTEGER,
    round       INTEGER NOT NULL DEFAULT 0,
    pushed_at   REAL,
    head_sha    TEXT,
    feedback    TEXT,
    note        TEXT,
    cancel      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL,
    at       REAL NOT NULL,
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
"""

FIELDS = {"title", "body", "done_when", "agent", "status", "issue", "branch", "pr", "round",
          "pushed_at", "head_sha", "feedback", "note", "cancel"}


class DB:
    """Thread-safe enough for one worker thread plus the web server."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def create_task(self, title: str, body: str, done_when: str, agent: str) -> int:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO tasks (created_at, updated_at, title, body, done_when, agent) "
                "VALUES (?, ?, ?, ?, ?, ?)", (now, now, title, body, done_when, agent))
            return int(cur.lastrowid)

    def get(self, task_id: int) -> dict | None:
        rows = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else None

    def list(self, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))

    def with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self._q(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", statuses)

    def update(self, task_id: int, **fields) -> None:
        bad = set(fields) - FIELDS
        if bad:
            raise ValueError(f"unknown task fields: {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?",
                              (*fields.values(), time.time(), task_id))

    def event(self, task_id: int, kind: str, text: str = "") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO events (task_id, at, kind, text) VALUES (?, ?, ?, ?)",
                              (task_id, time.time(), kind, text[:20000]))
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (time.time(), task_id))

    def events(self, task_id: int, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                       (task_id, limit))
        return list(reversed(rows))
