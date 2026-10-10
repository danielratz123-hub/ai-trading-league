"""Weekly scoring, points, the 4-week bonus, and scoreboard numbers.

Week N's return = equity at week N+1's execution open (the 'mark'
snapshot, before trades) / equity right after week N's trades (the
'post_trade' snapshot) - 1, with any deposits in between removed.
"""
from __future__ import annotations

import json
import math
import sqlite3

from . import ledger
from .config import SETTINGS
from .db import log_event


def _snap(conn, snap_id: int):
    return conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()


def week_start_snapshot(conn: sqlite3.Connection, agent_id: str, exec_date: str):
    """The post_trade snapshot written on a week's execution day."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE agent_id=? AND kind='post_trade' AND date=? "
        "ORDER BY seq DESC LIMIT 1", (agent_id, exec_date)).fetchone()


def score_week(conn: sqlite3.Connection, week_no: int, exec_date: str,
               marks: dict[str, int]) -> dict[str, float]:
    """Score `week_no` (which executed on exec_date) using the new marks.
    Returns {agent: weekly return}. Awards the point."""
    rets: dict[str, float] = {}
    for agent, mark_id in marks.items():
        start = week_start_snapshot(conn, agent, exec_date)
        if start is None:
            continue
        rets[agent] = ledger.period_return(conn, agent, start["id"], mark_id)
        _score_slots(conn, week_no, agent, start, _snap(conn, mark_id))

    best = max(rets.values()) if rets else None
    winners = [a for a, r in rets.items() if math.isclose(r, best, rel_tol=0, abs_tol=1e-12)]
    weekly = _learning_scores(conn, week_no, exec_date, marks, rets)
    if weekly and next(iter(weekly.values()))["mode"] == "risk_adjusted":
        # From scoring.start_week: the best risk-adjusted score wins the point.
        from .learning.scores import winner as risk_winner
        w = risk_winner(weekly)
        winners = [w] if w else []
    for agent, r in rets.items():
        start = week_start_snapshot(conn, agent, exec_date)
        point = int(len(winners) == 1 and agent == winners[0])   # tie -> nobody
        if weekly:
            conn.execute("UPDATE weekly_scores SET point_won=? WHERE week_no=? AND agent_id=?", (point, week_no, agent))
        conn.execute(
            "INSERT OR REPLACE INTO scores(week_no, agent_id, weekly_return, point, start_equity, end_equity) "
            "VALUES (?,?,?,?,?,?)",
            (week_no, agent, r, point, start["equity"], _snap(conn, marks[agent])["equity"]))
    conn.execute("UPDATE weeks SET status='scored' WHERE week_no=?", (week_no,))
    log_event(conn, "info", "scoring", f"week {week_no}: " + json.dumps({a: round(r, 6) for a, r in rets.items()}))
    return rets


def _learning_scores(conn, week_no: int, exec_date: str, marks: dict[str, int],
                     rets: dict[str, float]) -> dict[str, dict] | None:
    """weekly_scores rows (learning layer on only). None = layer off."""
    from .learning import config as lconfig
    if not lconfig.enabled():
        return None
    from .learning.scores import compute
    return compute(conn, week_no, exec_date, marks, rets)


def _score_slots(conn, week_no, agent, start, end) -> None:
    a, b = json.loads(start["slots_json"]), json.loads(end["slots_json"])
    for slot, eq0 in a.items():
        if slot == "0" or eq0 <= 0:
            continue
        name = conn.execute("SELECT name FROM slots WHERE agent_id=? AND slot=?", (agent, int(slot))).fetchone()
        conn.execute("INSERT OR REPLACE INTO slot_scores(week_no, agent_id, slot, name, weekly_return) "
                     "VALUES (?,?,?,?,?)",
                     (week_no, agent, int(slot), name["name"] if name else "", b.get(slot, 0.0) / eq0 - 1))


def maybe_award_bonus(conn: sqlite3.Connection, week_no: int, on_date: str) -> str | None:
    """After scoring week_no: every `bonus_every_weeks` weeks, the agent with
    the higher chained return over the period gets a deposit."""
    n = SETTINGS.bonus_every_weeks
    if week_no % n != 0:
        return None
    weeks = list(range(week_no - n + 1, week_no + 1))
    period: dict[str, float] = {}
    for agent in ledger.agent_ids(conn):
        rows = conn.execute(
            f"SELECT weekly_return FROM scores WHERE agent_id=? AND week_no IN ({','.join('?' * n)})",
            (agent, *weeks)).fetchall()
        if len(rows) == n:
            period[agent] = math.prod(1 + r["weekly_return"] for r in rows) - 1
    from .learning.scores import mode_for
    if mode_for(weeks[0]) == "risk_adjusted":
        # A whole period under the new rules: risk-adjusted period score.
        from .learning.scores import period_scores
        period = period_scores(conn, weeks)
    winner = None
    if len(period) >= 2:
        best = max(period.values())
        top = [a for a, r in period.items() if math.isclose(r, best, rel_tol=0, abs_tol=1e-12)]
        winner = top[0] if len(top) == 1 else None
    conn.execute("INSERT OR REPLACE INTO bonuses(period_end_week, winner, amount, returns_json) VALUES (?,?,?,?)",
                 (week_no, winner, SETTINGS.bonus_amount if winner else 0.0, json.dumps(period)))
    if winner:
        # Deposited after scoring, before this week's trades: it never counts as return.
        ledger.deposit(conn, winner, SETTINGS.bonus_amount, on_date, f"bonus weeks {weeks[0]}-{weeks[-1]}")
        conn.execute("UPDATE weekly_scores SET bonus_added=? WHERE week_no=? AND agent_id=?",
                     (SETTINGS.bonus_amount, week_no, winner))
    return winner


# --------------------------------------------------------------------------
# Scoreboard
# --------------------------------------------------------------------------

def points(conn, agent_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(point), 0) FROM scores WHERE agent_id=?", (agent_id,)).fetchone()[0]


def weekly_returns(conn, agent_id: str) -> list[tuple[int, float]]:
    return [(r["week_no"], r["weekly_return"]) for r in conn.execute(
        "SELECT week_no, weekly_return FROM scores WHERE agent_id=? ORDER BY week_no", (agent_id,))]


def cumulative_return(conn, agent_id: str) -> float:
    return math.prod(1 + r for _, r in weekly_returns(conn, agent_id)) - 1


def week_to_date(conn, agent_id: str, on_date: str) -> float | None:
    """Return since the last post_trade snapshot, at on_date's close."""
    start = ledger.latest_snapshot(conn, agent_id, "post_trade")
    if start is None or start["equity"] <= 0:
        return None
    now = ledger.value_agent(conn, agent_id, on_date, "close").equity
    added = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE agent_id=? AND seq > ?",
                         (agent_id, start["seq"])).fetchone()[0]
    return (now - added) / start["equity"] - 1


def slot_since_inception(conn, agent_id: str, slot: int) -> float | None:
    rows = conn.execute("SELECT weekly_return FROM slot_scores WHERE agent_id=? AND slot=?",
                        (agent_id, slot)).fetchall()
    return math.prod(1 + r[0] for r in rows) - 1 if rows else None


def scoreboard(conn, on_date: str) -> list[dict]:
    out = []
    for a in ledger.agent_ids(conn):
        wr = weekly_returns(conn, a)
        out.append({
            "agent": a, "points": points(conn, a),
            "equity": ledger.value_agent(conn, a, on_date, "close").equity,
            "cumulative_return": cumulative_return(conn, a),
            "last_week_return": wr[-1][1] if wr else None,
            "week_to_date": week_to_date(conn, a, on_date),
            "deposits": conn.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE agent_id=?",
                                     (a,)).fetchone()[0],
        })
    return out
