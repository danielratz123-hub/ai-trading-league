"""Risk-adjusted weekly scores (config/learning.yaml `scoring:`).

    score = weekly_return_pct / max(weekly_max_drawdown_pct, drawdown_floor_pct)

in percent, so +2% with a 4% drawdown scores 0.5, and +2% with a 0.3%
drawdown scores 2.0 (the 1% floor stops a tiny drawdown from blowing the
score up). An agent with a hard-limit violation in its fills that week
(hardlimits.audit_trades) scores 0.

The week is the league's scoring week: from right after the week's trades
(post_trade snapshot at the execution open) to the next execution open
(the mark). Its max drawdown is the worst peak-to-trough fall of the
equity path post_trade -> every daily close -> mark, with deposits removed.

Weeks before scoring.start_week (and everything while LEARNING_ENABLED is
off) keep the original rule: raw return decides the point. Their
weekly_scores rows are still written (mode raw_return), for the dashboard.
"""
from __future__ import annotations

import json
import math
import sqlite3

from . import config as lconfig
from .hardlimits import audit_trades


def mode_for(week_no: int) -> str:
    if not lconfig.enabled():
        return "raw_return"          # (doesn't even read the config file)
    c = lconfig.get().scoring
    if c.mode == "risk_adjusted" and week_no >= c.start_week:
        return "risk_adjusted"
    return "raw_return"


def score(return_pct: float, max_drawdown_pct: float, broke: bool, floor_pct: float | None = None) -> float:
    if broke:
        return 0.0
    floor_pct = lconfig.get().scoring.drawdown_floor_pct if floor_pct is None else floor_pct
    return return_pct / max(max_drawdown_pct, floor_pct)


def max_drawdown_pct(values: list[float]) -> float:
    """Worst peak-to-trough fall of a value path, in percent (>= 0)."""
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return -worst * 100


def equity_path(conn: sqlite3.Connection, agent_id: str, start_snap, end_snap) -> list[float]:
    """post_trade -> daily closes -> mark, deposits in between removed."""
    rows = conn.execute("SELECT seq, equity FROM snapshots WHERE agent_id=? AND seq>? AND seq<? AND kind='daily' "
                        "ORDER BY seq", (agent_id, start_snap["seq"], end_snap["seq"])).fetchall()
    deps = conn.execute("SELECT seq, amount FROM deposits WHERE agent_id=? AND seq>? AND seq<?",
                        (agent_id, start_snap["seq"], end_snap["seq"])).fetchall()

    def adj(seq, eq):
        return eq - sum(d["amount"] for d in deps if d["seq"] < seq)
    return [start_snap["equity"]] + [adj(r["seq"], r["equity"]) for r in rows] + [adj(end_snap["seq"], end_snap["equity"])]


def compute(conn: sqlite3.Connection, week_no: int, exec_date: str, marks: dict[str, int],
            rets: dict[str, float]) -> dict[str, dict]:
    """Write weekly_scores for week_no. Returns {agent: row}."""
    from ..scoring import week_start_snapshot
    mode = mode_for(week_no)
    floor = lconfig.get().scoring.drawdown_floor_pct
    out = {}
    for agent, r in rets.items():
        start = week_start_snapshot(conn, agent, exec_date)
        end = conn.execute("SELECT * FROM snapshots WHERE id=?", (marks[agent],)).fetchone()
        dd = max_drawdown_pct(equity_path(conn, agent, start, end))
        week_mark = conn.execute("SELECT seq FROM snapshots WHERE agent_id=? AND kind='mark' AND date=? "
                                 "ORDER BY seq LIMIT 1", (agent, exec_date)).fetchone()
        violations = audit_trades(conn, agent, week_mark["seq"] if week_mark else start["seq"] - 1, end["seq"])
        broke = bool(violations) and mode == "risk_adjusted"
        ret_pct = r * 100
        sc = score(ret_pct, dd, broke, floor) if mode == "risk_adjusted" else ret_pct
        out[agent] = {"return_pct": ret_pct, "max_drawdown_pct": dd, "broke": broke, "violations": violations,
                      "score": sc, "mode": mode}
        conn.execute(
            "INSERT OR REPLACE INTO weekly_scores(week_no, agent_id, week_start, mode, return_pct, max_drawdown_pct, "
            "broke_hard_limit, violations, score, point_won, bonus_added) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
            (week_no, agent, exec_date, mode, round(ret_pct, 6), round(dd, 6), int(broke),
             json.dumps(violations), round(sc, 6)))
    return out


def winner(rows: dict[str, dict]) -> str | None:
    """Single best score, or None on a tie."""
    if not rows:
        return None
    best = max(r["score"] for r in rows.values())
    top = [a for a, r in rows.items() if math.isclose(r["score"], best, rel_tol=0, abs_tol=1e-12)]
    return top[0] if len(top) == 1 else None


def period_scores(conn: sqlite3.Connection, weeks: list[int]) -> dict[str, float]:
    """Bonus period winner under risk_adjusted scoring: the chained return
    over the period / max(worst weekly drawdown, floor); 0 if any week broke
    a limit."""
    floor = lconfig.get().scoring.drawdown_floor_pct
    out = {}
    agents = [r[0] for r in conn.execute("SELECT DISTINCT agent_id FROM weekly_scores")]
    for a in agents:
        rows = conn.execute(f"SELECT * FROM weekly_scores WHERE agent_id=? AND week_no IN ({','.join('?' * len(weeks))})",
                            (a, *weeks)).fetchall()
        if len(rows) != len(weeks):
            continue
        chained = (math.prod(1 + r["return_pct"] / 100 for r in rows) - 1) * 100
        broke = any(r["broke_hard_limit"] for r in rows)
        out[a] = score(chained, max(r["max_drawdown_pct"] for r in rows), broke, floor)
    return out
