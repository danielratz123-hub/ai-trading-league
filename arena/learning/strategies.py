"""Strategies, their versions, and the rules for changing them.

A strategy lives in one of an agent's slots (1-5). Its rules (plain words)
and params (stop_loss_pct, position_max_pct) are versioned: every change is a
new strategy_versions row with a change_reason, so "v3 of momentum" can be
told apart from v1.

Who may change what (all enforced here, in code):
  * Saturday's weekly decision picks positions freely, but must follow each
    slot's live (champion) version: same strategy, position sizes within
    position_max_pct, a stop on every position within stop_loss_pct.
  * Params move only inside param_ranges, only in a review, and not while
    the strategy is in cooldown (cooldown_weeks_after_change after any change).
  * Rules are rewritten only by:
      - the weekly review: one rewrite per agent per week, with a trigger
        (underperformed SPY N weeks in a row / drawdown limit hit / an active
        lesson supports it), >= min_trades_for_rewrite trades, no cooldown.
        The rewrite becomes a challenger, tested in shadow the next week.
      - an emergency rewrite in the daily review, only for a strategy the
        hourly risk check flagged (drawdown limit). Emergencies ignore the
        cooldown: they exist for the case where waiting is the bigger risk.
  * A brand-new strategy in an empty slot counts as that week's rewrite.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..prices import ET
from . import config as lconfig

PARAM_KEYS = ("stop_loss_pct", "position_max_pct")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_week(ts: str) -> tuple[int, int]:
    """ISO (year, week) of a UTC timestamp, in New York time: a Saturday
    review and the Saturday decision after it fall in the same week."""
    return tuple(datetime.fromisoformat(ts).astimezone(ET).isocalendar()[:2])


@dataclass
class Strategy:
    strategy_id: int
    agent_id: str
    slot: int
    name: str
    status: str
    created_at: str
    emergency_flagged_at: str | None
    version_id: int            # the champion version
    version: int
    rules_text: str
    params: dict
    challenger: dict | None    # {version_id, version, rules_text, params, hypothesis} or None


def _strategy(conn, row) -> Strategy:
    ch = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND role='challenger'",
                      (row["id"],)).fetchone()
    return Strategy(row["id"], row["agent_id"], row["slot"], row["name"], row["status"], row["created_at"],
                    row["emergency_flagged_at"], row["vid"], row["version"], row["rules_text"],
                    json.loads(row["params"] or "{}"),
                    None if ch is None else {"version_id": ch["id"], "version": ch["version"],
                                             "rules_text": ch["rules_text"], "params": json.loads(ch["params"]),
                                             "hypothesis": ch["hypothesis"], "created_at": ch["created_at"]})


_SELECT = ("SELECT s.*, v.id AS vid, v.version, v.rules_text, v.params FROM strategies s "
           "JOIN strategy_versions v ON v.strategy_id = s.id AND v.role = 'champion' ")


def active(conn: sqlite3.Connection, agent_id: str) -> list[Strategy]:
    rows = conn.execute(_SELECT + "WHERE s.agent_id=? AND s.status='active' ORDER BY s.slot", (agent_id,)).fetchall()
    return [_strategy(conn, r) for r in rows]


def in_slot(conn: sqlite3.Connection, agent_id: str, slot: int) -> Strategy | None:
    row = conn.execute(_SELECT + "WHERE s.agent_id=? AND s.slot=? AND s.status='active'", (agent_id, slot)).fetchone()
    return _strategy(conn, row) if row else None


def by_id(conn: sqlite3.Connection, strategy_id: int) -> Strategy | None:
    row = conn.execute(_SELECT + "WHERE s.id=?", (strategy_id,)).fetchone()
    return _strategy(conn, row) if row else None


def version_row(conn: sqlite3.Connection, version_id: int):
    return conn.execute("SELECT v.*, s.agent_id, s.slot, s.name FROM strategy_versions v "
                        "JOIN strategies s ON s.id = v.strategy_id WHERE v.id=?", (version_id,)).fetchone()


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------

def create(conn: sqlite3.Connection, agent_id: str, slot: int, name: str, rules_text: str,
           params: dict | None, kind: str, reason: str, week_no: int | None, at: str | None = None) -> Strategy:
    """A new strategy in an (empty) slot, with its first version as champion."""
    at = at or _now()
    params = {**lconfig.default_params(), **(params or {})}
    sid = conn.execute("INSERT INTO strategies(agent_id, slot, name, status, created_at, created_week) "
                       "VALUES (?,?,?,'active',?,?)", (agent_id, slot, name.strip()[:80], at, week_no)).lastrowid
    conn.execute("INSERT INTO strategy_versions(strategy_id, version, rules_text, params, change_kind, change_reason, "
                 "role, created_at, created_week) VALUES (?,1,?,?,?,?,'champion',?,?)",
                 (sid, rules_text.strip()[:4000] or name, json.dumps(params), kind, reason[:1000], at, week_no))
    return by_id(conn, sid)


def add_version(conn: sqlite3.Connection, strategy_id: int, rules_text: str, params: dict, kind: str,
                reason: str, role: str, week_no: int | None, hypothesis: str | None = None,
                at: str | None = None) -> int:
    """New version. role='champion' makes it the live one (the old champion
    becomes 'superseded'); role='challenger' starts a shadow test."""
    at = at or _now()
    nxt = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_versions WHERE strategy_id=?",
                       (strategy_id,)).fetchone()[0]
    if role == "champion":
        conn.execute("UPDATE strategy_versions SET role='superseded' WHERE strategy_id=? AND role='champion'",
                     (strategy_id,))
    elif role == "challenger":
        conn.execute("UPDATE strategy_versions SET role='dropped' WHERE strategy_id=? AND role='challenger'",
                     (strategy_id,))
    return conn.execute(
        "INSERT INTO strategy_versions(strategy_id, version, rules_text, params, change_kind, change_reason, "
        "hypothesis, role, created_at, created_week) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (strategy_id, nxt, rules_text.strip()[:4000], json.dumps(params), kind, (reason or "")[:1000],
         (hypothesis or None) and hypothesis[:2000], role, at, week_no)).lastrowid


def promote(conn: sqlite3.Connection, version_id: int) -> None:
    """A challenger that won its shadow test becomes the champion."""
    v = conn.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
    conn.execute("UPDATE strategy_versions SET role='superseded' WHERE strategy_id=? AND role='champion'",
                 (v["strategy_id"],))
    conn.execute("UPDATE strategy_versions SET role='champion' WHERE id=?", (version_id,))


def drop(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE strategy_versions SET role='dropped' WHERE id=? AND role='challenger'", (version_id,))


def retire(conn: sqlite3.Connection, strategy_id: int, week_no: int | None = None, at: str | None = None) -> None:
    conn.execute("UPDATE strategies SET status='retired', retired_at=?, retired_week=? WHERE id=?",
                 (at or _now(), week_no, strategy_id))
    conn.execute("UPDATE strategy_versions SET role='dropped' WHERE strategy_id=? AND role='challenger'",
                 (strategy_id,))


def flag_emergency(conn: sqlite3.Connection, strategy_id: int, note: str, at: str | None = None) -> bool:
    """Returns True if newly flagged."""
    cur = conn.execute("UPDATE strategies SET emergency_flagged_at=?, emergency_note=? WHERE id=? "
                       "AND emergency_flagged_at IS NULL", (at or _now(), note[:500], strategy_id))
    return cur.rowcount > 0


def clear_emergency(conn: sqlite3.Connection, strategy_id: int) -> None:
    conn.execute("UPDATE strategies SET emergency_flagged_at=NULL, emergency_note=NULL WHERE id=?", (strategy_id,))


# --------------------------------------------------------------------------
# Rules for changes (each returns (ok, reason))
# --------------------------------------------------------------------------

def last_change(conn: sqlite3.Connection, strategy_id: int) -> str | None:
    """When the strategy last changed (the bootstrap's 'initial' version doesn't count)."""
    return conn.execute("SELECT MAX(created_at) FROM strategy_versions WHERE strategy_id=? AND change_kind != 'initial' "
                        "AND role IN ('champion', 'superseded')", (strategy_id,)).fetchone()[0]


def in_cooldown(conn: sqlite3.Connection, strategy_id: int, now: str) -> tuple[bool, str]:
    weeks = lconfig.get().learning.cooldown_weeks_after_change
    last = last_change(conn, strategy_id)
    if last is None:
        return False, "never changed"
    until = datetime.fromisoformat(last) + timedelta(weeks=weeks)
    if datetime.fromisoformat(now) < until:
        return True, f"changed {last[:10]}; in cooldown until {until.astimezone(ET):%Y-%m-%d %H:%M} ET"
    return False, f"last change {last[:10]}"


def check_params(params: dict) -> list[str]:
    """Known keys only, numbers only, inside param_ranges."""
    errors = []
    ranges = lconfig.get().param_ranges
    for k, v in params.items():
        if k not in ranges:
            errors.append(f"unknown param {k!r} (allowed: {', '.join(sorted(ranges))})")
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
            errors.append(f"{k} must be a number")
            continue
        lo, hi = ranges[k]
        if not lo - 1e-12 <= v <= hi + 1e-12:
            errors.append(f"{k}={v} is outside its allowed range [{lo}, {hi}]")
    return errors


def changes_this_week(conn: sqlite3.Connection, agent_id: str, now: str) -> int:
    """Rewrites + new strategies by the agent in the current (ET, ISO) week."""
    rows = conn.execute(
        "SELECT v.created_at FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id "
        "WHERE s.agent_id=? AND v.change_kind IN ('weekly_rewrite', 'new_strategy')", (agent_id,)).fetchall()
    wk = _iso_week(now)
    return sum(1 for r in rows if _iso_week(r[0]) == wk)


def trades_since_start(conn: sqlite3.Connection, s: Strategy) -> int:
    start = datetime.fromisoformat(s.created_at).astimezone(ET).date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM trades WHERE agent_id=? AND slot=? AND date>=?",
                        (s.agent_id, s.slot, start)).fetchone()[0]


def underperformed_spy(conn: sqlite3.Connection, s: Strategy, n: int) -> tuple[bool, list]:
    """Did the slot trail SPY in each of the last n scored weeks (all while
    this strategy ran it)?"""
    from ..api.queries import spy_week_return
    since = conn.execute("SELECT COALESCE(MIN(week_no), 0) FROM weeks WHERE exec_date >= ?",
                         (datetime.fromisoformat(s.created_at).astimezone(ET).date().isoformat(),)).fetchone()[0]
    rows = conn.execute("SELECT week_no, weekly_return FROM slot_scores WHERE agent_id=? AND slot=? "
                        "ORDER BY week_no DESC LIMIT ?", (s.agent_id, s.slot, n)).fetchall()
    detail = []
    for r in rows:
        spy = spy_week_return(conn, r["week_no"])
        detail.append({"week": r["week_no"], "slot": r["weekly_return"], "spy": spy})
    ok = (len(rows) == n and all(d["spy"] is not None and d["slot"] < d["spy"] for d in detail)
          and min(r["week_no"] for r in rows) >= since)
    return ok, detail


def rewrite_allowed(conn: sqlite3.Connection, s: Strategy, trigger: str, lesson_id: int | None,
                    now: str) -> tuple[bool, str]:
    """The weekly-review rewrite gate."""
    c = lconfig.get().learning
    if changes_this_week(conn, s.agent_id, now) >= c.rewrites_per_agent_per_week:
        return False, f"already used this week's {c.rewrites_per_agent_per_week} rewrite/new strategy"
    if s.challenger is not None:
        return False, f"slot {s.slot} already has a challenger (v{s.challenger['version']}) in its shadow test"
    cool, why = in_cooldown(conn, s.strategy_id, now)
    if cool:
        return False, why
    n = trades_since_start(conn, s)
    if n < c.min_trades_for_rewrite:
        return False, f"only {n} trades so far; a rewrite needs at least {c.min_trades_for_rewrite}"
    if trigger == "underperform_spy":
        ok, detail = underperformed_spy(conn, s, c.underperform_spy_weeks)
        return (True, "trailed SPY " + json.dumps(detail)) if ok else (
            False, f"did not trail SPY in each of the last {c.underperform_spy_weeks} scored weeks: {json.dumps(detail)}")
    if trigger == "drawdown_limit":
        return (True, f"drawdown limit hit {s.emergency_flagged_at}") if s.emergency_flagged_at else (
            False, "the drawdown limit was not hit for this strategy")
    if trigger == "lesson":
        if lesson_id is None:
            return False, "trigger 'lesson' needs lesson_id"
        row = conn.execute("SELECT * FROM lessons WHERE id=? AND agent_id=?", (lesson_id, s.agent_id)).fetchone()
        if row is None or row["status"] != "active":
            return False, f"lesson {lesson_id} is not one of your active lessons"
        if row["strategy_id"] not in (None, s.strategy_id):
            return False, f"lesson {lesson_id} is about another strategy"
        return True, f"supported by active lesson {lesson_id}"
    return False, f"unknown trigger {trigger!r} (underperform_spy | drawdown_limit | lesson)"


def new_strategy_allowed(conn: sqlite3.Connection, agent_id: str, slot: int, now: str) -> tuple[bool, str]:
    c = lconfig.get().learning
    if in_slot(conn, agent_id, slot) is not None:
        return False, f"slot {slot} already runs a strategy"
    if changes_this_week(conn, agent_id, now) >= c.rewrites_per_agent_per_week:
        return False, f"already used this week's {c.rewrites_per_agent_per_week} rewrite/new strategy"
    # Retiring a strategy and opening a new one in its place would be a
    # rewrite without trigger or shadow test, so the slot cools down too.
    # Retirements from before the new rules started don't count.
    retired = conn.execute("SELECT MAX(retired_at) FROM strategies WHERE agent_id=? AND slot=? AND status='retired' "
                           "AND retired_week >= ?", (agent_id, slot, lconfig.get().scoring.start_week)).fetchone()[0]
    if retired:
        until = datetime.fromisoformat(retired) + timedelta(weeks=c.cooldown_weeks_after_change)
        if datetime.fromisoformat(now) < until:
            return False, (f"slot {slot}'s last strategy was retired {retired[:10]}; the slot is in cooldown "
                           f"until {until.astimezone(ET):%Y-%m-%d}")
    return True, "ok"
