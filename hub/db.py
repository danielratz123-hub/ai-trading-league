"""The hub's own small SQLite database: plans, tasks and their timeline."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

# Task status, in order of a normal life:
#   (waiting ->) queued -> building -> reviewing -> (queued fix round -> fixing -> reviewing)*
#   -> ready -> merged
# Off the happy path: needs_joe (stopped, waiting for Joe), failed, cancelled.
# "waiting" = a later task of an approved plan; it starts when the one before it is merged.
ACTIVE = ("queued", "building", "fixing", "reviewing")
RUNNING = ("building", "fixing")
FINAL = ("merged", "cancelled")

# Plan status: planning -> managing (manager running) -> proposed -> approved -> done
# Off the happy path: rejected, failed, cancelled. "Ask for changes" goes back to planning.
PLAN_FINAL = ("done", "rejected", "cancelled")

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
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    agent    TEXT NOT NULL,
    model    TEXT NOT NULL DEFAULT '',
    role     TEXT NOT NULL,
    task_id  INTEGER,
    plan_id  INTEGER,
    seconds  REAL,
    ok       INTEGER
);
CREATE INDEX IF NOT EXISTS runs_at ON runs(at);
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    request     TEXT NOT NULL,
    manager     TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'planning',
    summary     TEXT,
    tasks_json  TEXT,
    feedback    TEXT,
    note        TEXT
);
"""

# Columns added after the first version (added to an existing database on start).
TASK_COLUMNS = {"reviewer": "TEXT", "plan_id": "INTEGER", "plan_order": "INTEGER",
                "review_sha": "TEXT", "review_verdict": "TEXT", "review_text": "TEXT",
                "model": "TEXT", "reviewer_model": "TEXT", "merge_sha": "TEXT", "deploy": "TEXT",
                "merged_at": "REAL"}
PLAN_COLUMNS = {"manager_model": "TEXT", "reviewer_model": "TEXT"}

FIELDS = {"title", "body", "done_when", "agent", "status", "issue", "branch", "pr", "round",
          "pushed_at", "head_sha", "feedback", "note", "cancel", *TASK_COLUMNS}
PLAN_FIELDS = {"status", "summary", "tasks_json", "feedback", "note", "reviewer", *PLAN_COLUMNS}


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
            for table, cols in (("tasks", TASK_COLUMNS), ("plans", PLAN_COLUMNS)):
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, kind in cols.items():
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # Tasks -------------------------------------------------------------------

    def create_task(self, title: str, body: str, done_when: str, agent: str, reviewer: str | None = None,
                    plan_id: int | None = None, plan_order: int | None = None,
                    status: str = "queued", model: str = "", reviewer_model: str = "") -> int:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO tasks (created_at, updated_at, title, body, done_when, agent, reviewer, "
                "plan_id, plan_order, status, model, reviewer_model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, now, title, body, done_when, agent, reviewer or agent, plan_id, plan_order, status,
                 model or "", reviewer_model or ""))
            return int(cur.lastrowid)

    def get(self, task_id: int) -> dict | None:
        rows = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else None

    def list(self, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))

    def with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self._q(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", statuses)

    def plan_tasks(self, plan_id: int) -> list[dict]:
        return self._q("SELECT * FROM tasks WHERE plan_id = ? ORDER BY plan_order, id", (plan_id,))

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

    # Plans -------------------------------------------------------------------

    def create_plan(self, request: str, manager: str, reviewer: str, manager_model: str = "",
                    reviewer_model: str = "") -> int:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO plans (created_at, updated_at, request, manager, reviewer, manager_model, "
                "reviewer_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, now, request, manager, reviewer, manager_model or "", reviewer_model or ""))
            return int(cur.lastrowid)

    def plan(self, plan_id: int) -> dict | None:
        rows = self._q("SELECT * FROM plans WHERE id = ?", (plan_id,))
        return _plan_row(rows[0]) if rows else None

    def plans(self, limit: int = 20) -> list[dict]:
        return [_plan_row(r) for r in self._q("SELECT * FROM plans ORDER BY id DESC LIMIT ?", (limit,))]

    def plans_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return [_plan_row(r) for r in
                self._q(f"SELECT * FROM plans WHERE status IN ({marks}) ORDER BY id", statuses)]

    def setting(self, key: str, default=None):
        rows = self._q("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                              (key, json.dumps(value)))

    def add_run(self, agent: str, model: str, role: str, task_id: int | None, plan_id: int | None,
                seconds: float, ok: bool) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO runs (at, agent, model, role, task_id, plan_id, seconds, ok) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (time.time(), agent, model or "", role, task_id, plan_id, seconds, int(ok)))

    def runs_since(self, since: float) -> dict[str, dict]:
        """{agent: {"runs": n, "minutes": m}} since a time."""
        rows = self._q("SELECT agent, COUNT(*) AS n, COALESCE(SUM(seconds), 0) AS s FROM runs "
                       "WHERE at >= ? GROUP BY agent", (since,))
        return {r["agent"]: {"runs": r["n"], "minutes": round(r["s"] / 60)} for r in rows}

    def update_plan(self, plan_id: int, **fields) -> None:
        bad = set(fields) - PLAN_FIELDS
        if bad:
            raise ValueError(f"unknown plan fields: {sorted(bad)}")
        if "tasks" in fields:
            raise ValueError("use tasks_json")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE plans SET {cols}, updated_at = ? WHERE id = ?",
                              (*fields.values(), time.time(), plan_id))


def _plan_row(row: dict) -> dict:
    row = dict(row)
    try:
        row["tasks"] = json.loads(row.get("tasks_json") or "[]")
    except json.JSONDecodeError:
        row["tasks"] = []
    return row
