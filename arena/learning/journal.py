"""The decisions journal: one frozen row per thing an agent decided.

Every LLM decision (Saturday's weekly decision, risk wake-ups, daily and
weekly reviews) writes rows here, holds included: on Saturday one row per
ticker (buy / sell / short / cover, or hold for a position kept as it was),
in a review one row per strategy. `input_snapshot` points at
the exact text/JSON the agent was given (decision_inputs, stored once and
addressed by its SHA-256, so the five rows of one review share one copy).

Rows can't be changed afterwards (database triggers); outcomes go to the
outcomes table, written later by the grader.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

ACTIONS = ("buy", "sell", "short", "cover", "hold")
MIN_WEIGHT_CHANGE = 0.005      # smaller weight changes count as "hold"


def now_utc() -> str:
    """UTC, microseconds included: a review and the grading just before it
    must never share a timestamp (see jobs.after)."""
    return datetime.now(timezone.utc).isoformat()


def store_input(conn: sqlite3.Connection, content: str | dict | list, at: str | None = None) -> str:
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO decision_inputs(sha256, content, created_at) VALUES (?,?,?)",
                 (sha, text, at or now_utc()))
    return sha


def read_input(conn: sqlite3.Connection, sha: str) -> str | None:
    row = conn.execute("SELECT content FROM decision_inputs WHERE sha256=?", (sha,)).fetchone()
    return row[0] if row else None


def regime_as_of(conn: sqlite3.Connection, as_of: str) -> str | None:
    """The regime label of the last close at or before as_of (UTC ISO)."""
    row = conn.execute("SELECT regime FROM market_context WHERE kind='close' AND ts<=? AND regime IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", (as_of,)).fetchone()
    return row[0] if row else None


def record(conn: sqlite3.Connection, *, agent_id: str, version_id: int, checkpoint: str, input_sha: str,
           input_kind: str, reasoning: str, action: str, confidence: float | None = None,
           symbol: str | None = None, target_weight: float | None = None, regime: str | None = None,
           lessons_used: list[int] | None = None, week_no: int | None = None, shadow: bool = False,
           fallback: bool = False, source_ref: str | None = None, decided_at: str | None = None) -> int:
    if action not in ACTIONS:
        raise ValueError(f"bad action {action!r}")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError(f"confidence {confidence} outside 0..1")
    return conn.execute(
        "INSERT INTO decisions(agent_id, strategy_version_id, decided_at, checkpoint, regime, input_snapshot, "
        "lessons_used, reasoning, confidence, action, symbol, target_weight, week_no, shadow, fallback, source_ref) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, version_id, decided_at or now_utc(), checkpoint, regime,
         json.dumps({"sha256": input_sha, "kind": input_kind}), json.dumps(sorted(lessons_used or [])),
         (reasoning or "").strip()[:4000] or "(no reasoning given)", confidence, action, symbol, target_weight,
         week_no, int(shadow), int(fallback), source_ref)).lastrowid


def action_for(old_w: float, new_w: float) -> str | None:
    """buy / sell / short / cover from a weight change within a slot, or None
    if the change is too small to count."""
    if abs(new_w - old_w) < MIN_WEIGHT_CHANGE:
        return None
    if new_w > old_w:
        return "cover" if old_w < 0 and new_w <= 0 else "buy"
    return "sell" if old_w > 0 and new_w >= 0 else "short"


def link_trades(conn: sqlite3.Connection, agent_id: str, d: str, checkpoint: str, week_no: int | None,
                after_seq: int = 0) -> int:
    """Point each fill of (agent, day) at the decision that asked for it
    (same strategy slot and ticker, latest decision of that checkpoint).
    Returns the number of fills linked."""
    rows = conn.execute("SELECT id, slot, ticker FROM trades WHERE agent_id=? AND date=? AND seq>? "
                        "AND decision_id IS NULL", (agent_id, d, after_seq)).fetchall()
    n = 0
    for t in rows:
        # The decision on that ticker; else (a small fill that only keeps the
        # weights where the agent left them) the strategy's decision to hold.
        dec = None
        for symbol_sql, extra in (("d.symbol=?", [t["ticker"]]), ("d.symbol IS NULL", [])):
            sql = ("SELECT d.id FROM decisions d JOIN strategy_versions v ON v.id = d.strategy_version_id "
                   "JOIN strategies s ON s.id = v.strategy_id WHERE d.agent_id=? AND d.checkpoint=? AND d.shadow=0 "
                   f"AND s.slot=? AND {symbol_sql}")
            params: list = [agent_id, checkpoint, t["slot"], *extra]
            if week_no is not None:
                sql += " AND d.week_no=?"
                params.append(week_no)
            dec = conn.execute(sql + " ORDER BY d.id DESC LIMIT 1", params).fetchone()
            if dec:
                break
        if dec:
            conn.execute("UPDATE trades SET decision_id=? WHERE id=?", (dec[0], t["id"]))
            n += 1
    return n
