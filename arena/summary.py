"""The 08:00 ET Telegram status (scheduler job `morning-report`).

  Monday-Friday  the last trading day: each agent's profit/loss that day,
                 equity, and total profit/loss since the start
  Saturday       the week (last Friday's close -> this Friday's close): each
                 agent's profit/loss, who is leading, points, vs SPY

Profit/loss always leaves deposits (the starting cash, bonuses) out: they
are new money, not performance. Sunday sends nothing (no new prices).
"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import ledger, scoring
from .prices import get_price

NAMES = {"claude": "Claude", "gpt": "GPT"}


def _closes(conn, agent_id: str, upto: str, n: int):
    """The agent's last n daily (close) snapshots on or before `upto`, newest first."""
    return conn.execute("SELECT date, seq, equity FROM snapshots WHERE agent_id=? AND kind='daily' AND date<=? "
                        "ORDER BY seq DESC LIMIT ?", (agent_id, upto, n)).fetchall()


def _deposits(conn, agent_id: str, after_seq: int = 0, upto_seq: int | None = None) -> float:
    sql, params = "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE agent_id=? AND seq>?", [agent_id, after_seq]
    if upto_seq is not None:
        sql += " AND seq<=?"
        params.append(upto_seq)
    return conn.execute(sql, params).fetchone()[0]


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _spy(conn, a: str, b: str, from_open: bool = False) -> float | None:
    pa, pb = get_price(conn, "SPY", a, "open" if from_open else "close"), get_price(conn, "SPY", b, "close")
    return pb / pa - 1 if pa and pb else None


def _total(conn, agent_id: str, row) -> tuple[float, float]:
    put_in = _deposits(conn, agent_id, 0, row["seq"])
    pnl = row["equity"] - put_in
    return pnl, (pnl / put_in if put_in else 0.0)


def daily(conn: sqlite3.Connection, upto: str) -> str | None:
    """The last trading day on or before `upto` vs the one before it."""
    agents = ledger.agent_ids(conn)
    if not agents:
        return None
    lines, day, prev_day = [], None, None
    for a in agents:
        rows = _closes(conn, a, upto, 2)
        if not rows:
            continue
        last = rows[0]
        day = last["date"]
        total, total_pct = _total(conn, a, last)
        if len(rows) == 2:
            prev = rows[1]
            prev_day = prev["date"]
            change = last["equity"] - _deposits(conn, a, prev["seq"], last["seq"]) - prev["equity"]
            today = f"{_money(change)} ({_pct(change / prev['equity'] if prev['equity'] else None)})"
        else:
            today = "first day"
        lines.append(f"{NAMES.get(a, a)}: {today} today\n   equity ${last['equity']:,.2f} | total {_money(total)} "
                     f"({_pct(total_pct)})")
    if day is None:
        return None
    head = f"Daily status, close of {date.fromisoformat(day):%a %b %d}"
    spy = _spy(conn, prev_day, day) if prev_day else None
    return "\n".join([head, *lines] + ([f"SPY: {_pct(spy)}"] if spy is not None else []))


def weekly(conn: sqlite3.Connection, upto: str) -> str | None:
    """The week ending at the last close on or before `upto`."""
    agents = ledger.agent_ids(conn)
    rows = {}
    for a in agents:
        last = _closes(conn, a, upto, 1)
        if not last:
            continue
        last = last[0]
        # Start: the last close of the previous calendar week (or the very first close).
        start = conn.execute("SELECT date, seq, equity, kind FROM snapshots WHERE agent_id=? AND kind='daily' AND date<? "
                             "AND strftime('%W', date) != strftime('%W', ?) ORDER BY seq DESC LIMIT 1",
                             (a, last["date"], last["date"])).fetchone()
        if start is None:
            start = conn.execute("SELECT date, seq, equity, kind FROM snapshots WHERE agent_id=? AND kind='post_trade' "
                                 "ORDER BY seq LIMIT 1", (a,)).fetchone()
        week = last["equity"] - _deposits(conn, a, start["seq"], last["seq"]) - start["equity"]
        total, total_pct = _total(conn, a, last)
        rows[a] = {"start": start["date"], "end": last["date"], "week": week, "from_open": start["kind"] == "post_trade",
                   "week_pct": week / start["equity"] if start["equity"] else None,
                   "equity": last["equity"], "total": total, "total_pct": total_pct,
                   "points": scoring.points(conn, a)}
    if not rows:
        return None
    end = max(r["end"] for r in rows.values())
    start = min(r["start"] for r in rows.values())
    lines = [f"Weekly status, {date.fromisoformat(start):%b %d} -> {date.fromisoformat(end):%b %d}"]
    for a, r in sorted(rows.items(), key=lambda kv: -kv[1]["week"]):
        lines.append(f"{NAMES.get(a, a)}: {_money(r['week'])} ({_pct(r['week_pct'])}) this week\n"
                     f"   equity ${r['equity']:,.2f} | total {_money(r['total'])} ({_pct(r['total_pct'])}) | "
                     f"points {r['points']}")
    best_week = max(rows, key=lambda a: rows[a]["week"])
    leader = max(rows, key=lambda a: (rows[a]["points"], rows[a]["total"]))
    if len(rows) > 1:
        lines.append(f"Best this week: {NAMES.get(best_week, best_week)}")
        tied = len({(r["points"], round(r["total"], 2)) for r in rows.values()}) == 1
        lines.append("Leading overall: tie" if tied else
                     f"Leading overall: {NAMES.get(leader, leader)} ({rows[leader]['points']} points, "
                     f"total {_money(rows[leader]['total'])})")
    spy = _spy(conn, start, end, from_open=any(r["from_open"] for r in rows.values() if r["start"] == start))
    if spy is not None:
        lines.append(f"SPY this week: {_pct(spy)}")
    return "\n".join(lines)
