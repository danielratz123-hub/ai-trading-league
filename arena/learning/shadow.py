"""Champion vs challenger.

A rewrite proposed in the weekly review becomes a *challenger* version of
the strategy. In the next weekly decision the agent gives, for that slot,
both the live positions (champion) and the positions it would hold under the
challenger's rules. The challenger's positions are simulated on paper for
one week (shadow_test_weeks), with no capital:

    entry at the execution day's open, valued at each close, until the last
    close before the next weekly review (the Friday close), minus entry
    slippage on its gross exposure.

The champion is measured over the same window from the slot's own equity
(post_trade snapshot at the open -> daily closes). At the next weekly review
the challenger is promoted only if it beat the champion on BOTH return and
max drawdown; otherwise it is dropped. Either way the result is recorded as
a lesson (source = engine).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..config import SETTINGS
from ..prices import get_price
from ..trading_calendar import trading_days
from . import journal, lessons, strategies as strat
from .scores import max_drawdown_pct


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start(conn: sqlite3.Connection, agent_id: str, slot: int, week_no: int, exec_date: str,
          weights: dict[str, float], input_sha: str, reasoning: str, confidence: float | None,
          regime: str | None, decided_at: str) -> int | None:
    """Register the challenger's shadow positions for the coming week."""
    s = strat.in_slot(conn, agent_id, slot)
    if s is None or s.challenger is None:
        return None
    run = conn.execute(
        "INSERT OR REPLACE INTO shadow_runs(version_id, champion_version_id, week_no, weights, start_date) "
        "VALUES (?,?,?,?,?)", (s.challenger["version_id"], s.version_id, week_no,
                               json.dumps({k: v for k, v in weights.items() if v}), exec_date)).lastrowid
    for t, w in sorted(weights.items()):
        if w:
            journal.record(conn, agent_id=agent_id, version_id=s.challenger["version_id"],
                           checkpoint="weekly_rebalance", input_sha=input_sha, input_kind="weekly_decision",
                           reasoning=f"[shadow] {reasoning}", action="buy" if w > 0 else "short",
                           confidence=confidence, symbol=t, target_weight=w, regime=regime, week_no=week_no,
                           shadow=True, source_ref=f"shadow:{run}", decided_at=decided_at)
    return run


def _slot_eq(conn, agent_id: str, slot: int, d: str, kind: str) -> float | None:
    row = conn.execute("SELECT slots_json FROM snapshots WHERE agent_id=? AND kind=? AND date=? ORDER BY seq DESC LIMIT 1",
                       (agent_id, kind, d)).fetchone()
    return None if row is None else json.loads(row[0]).get(str(slot))


def measure(conn: sqlite3.Connection, run, agent_id: str, slot: int, end_day: str) -> dict | None:
    """Both sides' return and max drawdown (percent) from the start open to end_day's close."""
    days = trading_days(conn, run["start_date"], end_day)
    if not days:
        return None
    weights = json.loads(run["weights"])
    # Challenger: weights x price path, entry at the open.
    gross = sum(abs(w) for w in weights.values())
    entry_cost = gross * SETTINGS.costs.slippage_bps / 1e4
    path = [1.0 - entry_cost]
    opens = {t: get_price(conn, t, run["start_date"], "open") for t in weights}
    if any(v is None for v in opens.values()):
        return None
    for d in days:
        v = 1.0 - entry_cost
        for t, w in weights.items():
            c = get_price(conn, t, d, "close")
            if c is None:
                return None
            v += w * (c / opens[t] - 1)
        path.append(v)
    # Champion: the slot's equity.
    e0 = _slot_eq(conn, agent_id, slot, run["start_date"], "post_trade")
    champ = [_slot_eq(conn, agent_id, slot, d, "daily") for d in days]
    if not e0 or any(e is None for e in champ):
        return None
    return {"return_pct": (path[-1] - 1) * 100, "max_drawdown_pct": max_drawdown_pct(path),
            "champion_return_pct": (champ[-1] / e0 - 1) * 100,
            "champion_max_drawdown_pct": max_drawdown_pct([e0] + champ), "end_date": end_day}


def evaluate(conn: sqlite3.Connection, agent_id: str, end_day: str, day: str) -> list[dict]:
    """Decide every pending shadow run of the agent whose week has ended.
    Promotes or drops the challenger and records a lesson. Returns results."""
    out = []
    rows = conn.execute(
        "SELECT r.*, s.slot, s.id AS strategy_id, v.version, v.hypothesis FROM shadow_runs r "
        "JOIN strategy_versions v ON v.id = r.version_id JOIN strategies s ON s.id = v.strategy_id "
        "WHERE s.agent_id=? AND r.verdict='pending' AND r.start_date IS NOT NULL AND r.start_date < ?",
        (agent_id, end_day)).fetchall()
    for run in rows:
        role = conn.execute("SELECT role FROM strategy_versions WHERE id=?", (run["version_id"],)).fetchone()[0]
        res = measure(conn, run, agent_id, run["slot"], end_day) if json.loads(run["weights"]) else None
        if role != "challenger":
            verdict, note = "dropped", "the strategy changed or was retired before the test ended"
        elif res is None:
            verdict, note = "dropped", ("no shadow positions were given" if not json.loads(run["weights"])
                                        else "not enough price data to compare")
        else:
            better_ret = res["return_pct"] > res["champion_return_pct"]
            better_dd = res["max_drawdown_pct"] < res["champion_max_drawdown_pct"]
            verdict = "promoted" if better_ret and better_dd else "dropped"
            note = (f"challenger {res['return_pct']:+.2f}% (max dd {res['max_drawdown_pct']:.2f}%) vs champion "
                    f"{res['champion_return_pct']:+.2f}% (max dd {res['champion_max_drawdown_pct']:.2f}%)")
        res = res or {}
        conn.execute("UPDATE shadow_runs SET end_date=?, return_pct=?, max_drawdown_pct=?, champion_return_pct=?, "
                     "champion_max_drawdown_pct=?, verdict=?, note=?, evaluated_at=? WHERE id=?",
                     (res.get("end_date", end_day), res.get("return_pct"), res.get("max_drawdown_pct"),
                      res.get("champion_return_pct"), res.get("champion_max_drawdown_pct"), verdict, note,
                      _now(), run["id"]))
        if verdict == "promoted":
            strat.promote(conn, run["version_id"])
        elif role == "challenger":
            strat.drop(conn, run["version_id"])
        evidence = [r[0] for r in conn.execute("SELECT id FROM decisions WHERE source_ref=?", (f"shadow:{run['id']}",))]
        lesson_text = (f"Slot {run['slot']} v{run['version']} challenger {verdict}: {note}. "
                       f"Hypothesis was: {(run['hypothesis'] or '-')[:200]}")
        lessons.upsert(conn, agent_id=agent_id, key=f"challenger-s{run['slot']}-v{run['version']}-{verdict}",
                       lesson=lesson_text, kind="keep" if verdict == "promoted" else "stop", evidence=evidence,
                       day=day, strategy_id=run["strategy_id"], source="engine")
        out.append({"slot": run["slot"], "version": run["version"], "verdict": verdict, "note": note})
    return out
