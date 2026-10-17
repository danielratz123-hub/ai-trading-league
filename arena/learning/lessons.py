"""Lessons the agents write about themselves.

    candidate --(confirmed on lesson_confirmations_to_activate different days)--> active
    candidate / active --(contradicted by recent evidence, weekly review)--> retired

Anti-overfitting: a lesson only reaches the agents' prompts once it has been
confirmed several times, each confirmation needs evidence (decision ids the
agent could see graded), and a lesson is confirmed at most once a day.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import config as lconfig

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
KINDS = ("keep", "stop", "improve", "observation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert(conn: sqlite3.Connection, *, agent_id: str, key: str, lesson: str, kind: str, evidence: list[int],
           day: str, regime: str | None = None, strategy_id: int | None = None, source: str = "review",
           at: str | None = None) -> tuple[int, str]:
    """New candidate, or a confirmation of the agent's lesson with that key.
    Returns (lesson id, what happened)."""
    at = at or _now()
    row = conn.execute("SELECT * FROM lessons WHERE agent_id=? AND key=?", (agent_id, key)).fetchone()
    if row is None:
        lid = conn.execute(
            "INSERT INTO lessons(agent_id, strategy_id, regime, key, lesson, kind, evidence_decisions, times_confirmed, "
            "status, source, last_confirmed_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,'candidate',?,?,?,?)",
            (agent_id, strategy_id, regime, key, lesson[:600], kind, json.dumps(sorted(set(evidence))), source, day,
             at, at)).lastrowid
        return lid, "new candidate"
    merged = sorted(set(json.loads(row["evidence_decisions"])) | set(evidence))
    if row["status"] == "retired":
        conn.execute("UPDATE lessons SET status='candidate', times_confirmed=1, lesson=?, kind=?, regime=?, "
                     "evidence_decisions=?, last_confirmed_on=?, retired_reason=NULL, updated_at=? WHERE id=?",
                     (lesson[:600], kind, regime, json.dumps(merged), day, at, row["id"]))
        return row["id"], "retired lesson restarted as a candidate"
    if row["last_confirmed_on"] == day:
        conn.execute("UPDATE lessons SET evidence_decisions=?, updated_at=? WHERE id=?",
                     (json.dumps(merged), at, row["id"]))
        return row["id"], "already confirmed today (evidence added)"
    conn.execute("UPDATE lessons SET times_confirmed=times_confirmed+1, lesson=?, evidence_decisions=?, "
                 "last_confirmed_on=?, updated_at=? WHERE id=?", (lesson[:600], json.dumps(merged), day, at, row["id"]))
    return row["id"], f"confirmed (x{row['times_confirmed'] + 1})"


def promote_ready(conn: sqlite3.Connection, agent_id: str, at: str | None = None) -> list[int]:
    n = lconfig.get().learning.lesson_confirmations_to_activate
    ids = [r[0] for r in conn.execute("SELECT id FROM lessons WHERE agent_id=? AND status='candidate' "
                                      "AND times_confirmed >= ?", (agent_id, n))]
    for i in ids:
        conn.execute("UPDATE lessons SET status='active', updated_at=? WHERE id=?", (at or _now(), i))
    return ids


def retire(conn: sqlite3.Connection, agent_id: str, lesson_id: int, reason: str, at: str | None = None) -> bool:
    cur = conn.execute("UPDATE lessons SET status='retired', retired_reason=?, updated_at=? WHERE id=? AND agent_id=? "
                       "AND status != 'retired'", (reason[:600], at or _now(), lesson_id, agent_id))
    return cur.rowcount > 0


def for_prompt(conn: sqlite3.Connection, agent_id: str, regime: str | None, as_of: str) -> list[dict]:
    """Active lessons for the current regime (or any regime), as of a time.
    Under the evidence rules (EVIDENCE_RULES=1) only lessons confirmed with
    backtest + live evidence count as active: they are the agents' rules."""
    cap = lconfig.get().learning.max_lessons_in_prompt
    from ..lab import config as labconfig
    only = ""
    if labconfig.evidence_enabled():
        from ..lab import evidence
        evidence.ensure(conn)
        only = " AND id IN (SELECT lesson_id FROM lesson_confirmations)"
    rows = conn.execute(
        "SELECT * FROM lessons WHERE agent_id=? AND status='active' AND created_at < ? AND updated_at < ? "
        "AND (regime IS NULL OR regime = ?)" + only + " ORDER BY times_confirmed DESC, updated_at DESC LIMIT ?",
        (agent_id, as_of, as_of, regime, cap)).fetchall()
    return [_public(r) for r in rows]


def active_all(conn: sqlite3.Connection, agent_id: str, as_of: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM lessons WHERE agent_id=? AND status='active' AND created_at < ? "
                        "ORDER BY updated_at DESC LIMIT 20", (agent_id, as_of)).fetchall()
    return [_public(r) for r in rows]


def candidates(conn: sqlite3.Connection, agent_id: str, as_of: str, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM lessons WHERE agent_id=? AND status='candidate' AND created_at < ? "
                        "ORDER BY updated_at DESC LIMIT ?", (agent_id, as_of, limit)).fetchall()
    return [_public(r) for r in rows]


def _public(r) -> dict:
    return {"id": r["id"], "key": r["key"], "lesson": r["lesson"], "kind": r["kind"], "regime": r["regime"],
            "strategy_id": r["strategy_id"], "times_confirmed": r["times_confirmed"], "status": r["status"]}
