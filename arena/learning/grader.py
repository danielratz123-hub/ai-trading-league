"""The grader (no LLM): fills in outcomes for past decisions. Runs before
every review.

All numbers are in PERCENT (1.5 = +1.5%). For a decision on a symbol:

  entry      the fill the decision caused (trades.decision_id), else the open
             of the first trading day after the decision (what it would have got)
  direction  +1 for buy / cover / holding a long, -1 for short / sell /
             holding a short (a sell "wins" if the price then falls)
  pnl_1d     direction x (close 1 trading day after entry / entry - 1)
  pnl_1w     direction x (close 5 trading days after entry / entry - 1)
  max_drawdown  worst adverse move from entry over those 5 days (daily lows
             for longs, highs for shorts), as a positive number
  vs_spy_1w  pnl_1w minus SPY's return over the same window
  pnl_exit / exit_reason  when the position the decision opened is fully
             closed: direction x (exit fill / entry - 1), and the closing
             trade's reason (rebalance, stop_loss, risk_exit, ...)

A decision without a symbol (a hold, or a capital tilt) is graded on its
strategy's equity from the daily snapshots, starting at the close on or
before the decision.

Every time an outcome changes, the new version is appended to
outcomes_history with evaluated_at = now. outcomes_as_of() reads that
history, so a review can only see grades that existed before it started.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import ledger
from ..db import transaction
from ..prices import ET, get_price
from ..trading_calendar import trading_days
from . import strategies as strat

FIELDS = ("pnl_1d_pct", "pnl_1w_pct", "pnl_exit_pct", "max_drawdown_pct", "vs_spy_1w_pct", "exit_reason")
DIRECTION = {"buy": 1, "cover": 1, "hold": 1, "sell": -1, "short": -1}
OPENING = ("buy", "short")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _r(x):
    return None if x is None else round(x, 4)


def entry_day(conn: sqlite3.Connection, decided_at: str) -> str | None:
    """First trading day whose open comes after the decision."""
    t = datetime.fromisoformat(decided_at).astimezone(ET)
    d = t.date().isoformat()
    days = trading_days(conn, d, "2999-12-31")
    if not days:
        return None
    if days[0] == d:
        row = conn.execute("SELECT open_time FROM calendar WHERE date=?", (d,)).fetchone()
        h, m = map(int, row[0].split(":"))
        if (t.hour, t.minute) >= (h, m):
            return days[1] if len(days) > 1 else None
    return days[0]


def _closed_by(conn, d: str, now: str) -> bool:
    """Was trading day d's close at or before `now`? (Only closes the grader
    could have known at `now` are used, so a grade is valid as of its time.)"""
    from .collector import close_ts
    return close_ts(conn, d) <= now


def _window(conn, start: str, n: int, now: str) -> list[str]:
    """start and the next n trading days whose close is known at `now`."""
    return [d for d in trading_days(conn, start, "2999-12-31")[: n + 1] if _closed_by(conn, d, now)]


def _has_close(conn, ticker: str, d: str) -> bool:
    return get_price(conn, ticker, d, "close") is not None


def grade_symbol(conn: sqlite3.Connection, dec, now: str) -> dict | None:
    sign = DIRECTION[dec["action"]]
    if dec["action"] == "hold" and (dec["target_weight"] or 0) < 0:
        sign = -1            # holding a short
    t = dec["symbol"]
    fill = conn.execute("SELECT * FROM trades WHERE decision_id=? ORDER BY seq LIMIT 1", (dec["id"],)).fetchone()
    if fill is not None:
        start, ref, start_seq = fill["date"], fill["fill_price"], fill["seq"]
    else:
        start = entry_day(conn, dec["decided_at"])
        ref = get_price(conn, t, start, "open") if start else None
        start_seq = None
    if not start or not ref:
        return None
    days = [d for d in _window(conn, start, 5, now) if _has_close(conn, t, d)]
    out: dict = {k: None for k in FIELDS}
    if len(days) >= 2:
        out["pnl_1d_pct"] = _r(sign * (get_price(conn, t, days[1], "close") / ref - 1) * 100)
    if len(days) >= 6:
        c = get_price(conn, t, days[5], "close")
        out["pnl_1w_pct"] = _r(sign * (c / ref - 1) * 100)
        spy0, spy1 = get_price(conn, "SPY", start, "open"), get_price(conn, "SPY", days[5], "close")
        if spy0 and spy1:
            out["vs_spy_1w_pct"] = _r(out["pnl_1w_pct"] - (spy1 / spy0 - 1) * 100)
    # Exit of the position this decision opened.
    exit_row = None
    if dec["action"] in OPENING:
        slot = strat.version_row(conn, dec["strategy_version_id"])["slot"]
        held = 0.0
        q = "SELECT * FROM trades WHERE agent_id=? AND slot=? AND ticker=? ORDER BY seq"
        now_day = datetime.fromisoformat(now).astimezone(ET).date().isoformat()
        for tr in conn.execute(q, (dec["agent_id"], slot, t)):
            if tr["date"] > now_day:
                break
            before = held
            held += tr["shares"]
            started = (tr["seq"] >= start_seq) if start_seq is not None else tr["date"] >= start
            if started and abs(before) > ledger.DUST and abs(held) < ledger.DUST:
                exit_row = tr
                break
        if exit_row is not None:
            out["pnl_exit_pct"] = _r(sign * (exit_row["fill_price"] / ref - 1) * 100)
            out["exit_reason"] = exit_row["reason"]
    # Worst adverse move until the exit (or 5 days).
    path = days[:6]
    if exit_row is not None:
        path = [d for d in path if d <= exit_row["date"]] or [start]
    worst = 0.0
    for d in path:
        px = get_price(conn, t, d, "low" if sign > 0 else "high")
        if px:
            worst = min(worst, sign * (px / ref - 1))
    if len(days) >= 2 or exit_row is not None:
        out["max_drawdown_pct"] = _r(-worst * 100)
    return out


def _slot_equity(conn, agent_id: str, slot: int, d: str, kind: str = "daily") -> float | None:
    row = conn.execute("SELECT slots_json FROM snapshots WHERE agent_id=? AND kind=? AND date=? "
                       "ORDER BY seq DESC LIMIT 1", (agent_id, kind, d)).fetchone()
    if row is None:
        return None
    return json.loads(row[0]).get(str(slot))


def grade_strategy(conn: sqlite3.Connection, dec, now: str) -> dict | None:
    """Hold / tilt decisions: the strategy's own equity, from the last close
    at or before the decision (or, for a weekend decision on a strategy that
    only started trading after it, from Monday's post-trade value) to the
    closes after it."""
    sign = DIRECTION[dec["action"]]
    a = dec["agent_id"]
    slot = strat.version_row(conn, dec["strategy_version_id"])["slot"]
    dday = datetime.fromisoformat(dec["decided_at"]).astimezone(ET).date().isoformat()
    row = conn.execute("SELECT date, slots_json FROM snapshots WHERE agent_id=? AND kind='daily' AND date<=? "
                       "ORDER BY seq DESC LIMIT 1", (a, dday)).fetchone()
    base = json.loads(row["slots_json"]).get(str(slot)) if row else None
    base_day = row["date"] if row else None
    if not base:
        e = entry_day(conn, dec["decided_at"])
        base = _slot_equity(conn, a, slot, e, "post_trade") if e else None
        base_day = e
    if not base or base <= 0 or base_day is None:
        return None
    days = [d for d in _window(conn, base_day, 6, now)[1:] if _slot_equity(conn, a, slot, d) is not None][:5]
    out: dict = {k: None for k in FIELDS}
    eq = [_slot_equity(conn, a, slot, d) for d in days]
    if days:
        out["pnl_1d_pct"] = _r(sign * (eq[0] / base - 1) * 100)
        out["max_drawdown_pct"] = _r(-min(0.0, min(sign * (e / base - 1) for e in eq)) * 100)
    if len(days) >= 5:
        out["pnl_1w_pct"] = _r(sign * (eq[4] / base - 1) * 100)
        spy0, spy1 = get_price(conn, "SPY", base_day, "close"), get_price(conn, "SPY", days[4], "close")
        if spy0 and spy1:
            out["vs_spy_1w_pct"] = _r(out["pnl_1w_pct"] - (spy1 / spy0 - 1) * 100)
    return out


def _final(row) -> bool:
    if row is None or row["pnl_1w_pct"] is None:
        return False
    return row["pnl_exit_pct"] is not None or row["action"] not in OPENING


def grade_all(conn: sqlite3.Connection, now: str | None = None) -> dict:
    """Grade every real (non-shadow) decision that isn't final yet."""
    now = now or _now()
    rows = conn.execute(
        "SELECT d.*, o.pnl_1w_pct, o.pnl_exit_pct, o.decision_id AS graded FROM decisions d "
        "LEFT JOIN outcomes o ON o.decision_id = d.id WHERE d.shadow = 0 AND d.decided_at <= ? ORDER BY d.id",
        (now,)).fetchall()
    stats = {"checked": 0, "updated": 0, "final": 0}
    with transaction(conn):
        for dec in rows:
            if _final(dec):
                stats["final"] += 1
                continue
            stats["checked"] += 1
            out = grade_symbol(conn, dec, now) if dec["symbol"] else grade_strategy(conn, dec, now)
            if out is None or all(out[k] is None for k in FIELDS):
                continue
            cur = conn.execute("SELECT * FROM outcomes WHERE decision_id=?", (dec["id"],)).fetchone()
            if cur is not None and all(cur[k] == out[k] for k in FIELDS):
                continue
            vals = [out[k] for k in FIELDS]
            conn.execute(f"INSERT OR REPLACE INTO outcomes(decision_id, {', '.join(FIELDS)}, evaluated_at) "
                         f"VALUES (?,{','.join('?' * len(FIELDS))},?)", (dec["id"], *vals, now))
            conn.execute(f"INSERT INTO outcomes_history(decision_id, {', '.join(FIELDS)}, evaluated_at) "
                         f"VALUES (?,{','.join('?' * len(FIELDS))},?)", (dec["id"], *vals, now))
            stats["updated"] += 1
    return stats


def outcomes_as_of(conn: sqlite3.Connection, agent_id: str, as_of: str,
                   since: str | None = None, limit: int = 40) -> list[dict]:
    """The graded decisions an agent may see in a review starting at as_of:
    for each decision, its latest grade evaluated strictly BEFORE as_of.
    since: only grades evaluated after this time (what's new since the last
    review)."""
    sql = ("SELECT h.*, d.agent_id, d.decided_at, d.checkpoint, d.action, d.symbol, d.target_weight, d.regime, "
           "d.confidence, d.reasoning, s.slot, s.name AS strategy, v.version FROM outcomes_history h "
           "JOIN decisions d ON d.id = h.decision_id JOIN strategy_versions v ON v.id = d.strategy_version_id "
           "JOIN strategies s ON s.id = v.strategy_id "
           "WHERE d.agent_id = ? AND d.shadow = 0 AND h.evaluated_at < ? AND d.decided_at < ? "
           "AND h.id = (SELECT MAX(h2.id) FROM outcomes_history h2 WHERE h2.decision_id = h.decision_id "
           "            AND h2.evaluated_at < ?)")
    params: list = [agent_id, as_of, as_of, as_of]
    if since:
        sql += " AND h.evaluated_at >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY d.decided_at DESC LIMIT ?", (*params, limit)).fetchall()
    return [{"decision_id": r["decision_id"], "decided_at": r["decided_at"], "checkpoint": r["checkpoint"],
             "slot": r["slot"], "strategy": r["strategy"], "version": r["version"], "action": r["action"],
             "symbol": r["symbol"], "regime": r["regime"], "confidence": r["confidence"],
             "reasoning": (r["reasoning"] or "")[:200],
             **{k: r[k] for k in FIELDS}, "evaluated_at": r["evaluated_at"]} for r in rows]


def write_portfolio_snapshots(conn: sqlite3.Connection, agent_id: str, ts: str, val: ledger.Valuation) -> int:
    """Equity per strategy (slot) at a checkpoint."""
    strategy_of = {s.slot: s.strategy_id for s in strat.active(conn, agent_id)}
    cash = ledger.slot_cash(conn, agent_id)
    by_slot: dict[int, list] = {}
    for p in ledger.positions(conn, agent_id):
        by_slot.setdefault(p["slot"], []).append(p)
    n = 0
    for slot in sorted(set(cash) | set(by_slot)):
        pos = by_slot.get(slot, [])
        pv = sum(p["shares"] * val.prices[p["ticker"]] for p in pos)
        conn.execute(
            "INSERT OR REPLACE INTO portfolio_snapshots(agent_id, slot, strategy_id, ts, cash, positions_value, equity, "
            "positions) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, slot, strategy_of.get(slot), ts, round(cash.get(slot, 0.0), 6), round(pv, 6),
             round(cash.get(slot, 0.0) + pv, 6),
             json.dumps([{"symbol": p["ticker"], "qty": round(p["shares"], 6), "avg_price": round(p["avg_price"], 4),
                          "price": round(val.prices[p["ticker"]], 4)} for p in pos])))
        n += 1
    return n
