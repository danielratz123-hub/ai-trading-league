"""Read-only data for the dashboard's backtest lab section (arena/lab).

Plain SELECTs on the read-only connection. Until a lab command has created
the lab's tables, the section reports itself as unavailable and stays hidden.

    market state    today's SPY-only label and the last 60 days
    experiments     every challenger: its backtest results, how many tries the
                    idea took, per market state, the holdout, shadow progress,
                    and where it stands (promoted / retired / in shadow /
                    refused at a step)
    strategies      per market state, live and backtest side by side
    capital         each strategy's share of its agent now, and this week's
                    change; the risk dial (backtest only for now)
    adaptive        the Phase 5 study results, if it has been run
The survivorship caveat goes with every backtest number.
"""
from __future__ import annotations

import json
import sqlite3

from ..lab import config as labconfig


def _rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _has(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


_SUMMARY = ("start", "end", "total_return_pct", "annual_return_pct", "max_drawdown_pct", "sharpe",
            "risk_adjusted_score", "period_score", "trades", "win_rate_pct", "spy_return_pct", "vs_spy_pct")


def _summary(results: str | None) -> dict | None:
    if not results:
        return None
    r = json.loads(results)
    out = {k: r.get(k) for k in _SUMMARY}
    out["by_state"] = {k: {x: v.get(x) for x in ("days", "annual_return_pct", "trades", "win_rate_pct")}
                       for k, v in (r.get("by_state") or {}).items()}
    return out


def _experiments(conn) -> list[dict]:
    if not _has(conn, "promotion_log"):
        return []
    ideas = _rows(conn, """SELECT agent_id AS agent, idea_key, MAX(slot) AS slot, MIN(ts) AS first_ts, MAX(id) AS last
                           FROM promotion_log WHERE idea_key IS NOT NULL GROUP BY agent_id, idea_key
                           ORDER BY last DESC LIMIT 30""")
    ev = labconfig.get().evidence
    out = []
    for i in ideas:
        steps = _rows(conn, "SELECT ts, stage, passed, reason FROM promotion_log WHERE agent_id=? AND idea_key=? "
                            "ORDER BY id", (i["agent"], i["idea_key"]))
        attempts = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE idea_key=? AND purpose != 'check'",
                                (i["idea_key"],)).fetchone()[0]
        wf = conn.execute("SELECT results FROM backtest_runs WHERE idea_key=? AND purpose='walk_forward' AND status='ok' "
                          "ORDER BY (requested_by='engine:promotion') DESC, id DESC LIMIT 1", (i["idea_key"],)).fetchone()
        ho = conn.execute("SELECT results FROM backtest_runs WHERE idea_key=? AND purpose='holdout' AND status='ok' "
                          "ORDER BY id LIMIT 1", (i["idea_key"],)).fetchone()
        sh = conn.execute("SELECT * FROM lab_shadows WHERE agent_id=? AND idea_key=? ORDER BY started_at DESC LIMIT 1",
                          (i["agent"], i["idea_key"])).fetchone()
        last = steps[-1] if steps else None
        if any(s["stage"] == "promoted" for s in steps):
            status = "promoted"
        elif any(s["stage"] == "retired" for s in steps) or (sh and sh["status"] == "retired"):
            status = "retired"
        elif sh and sh["status"] == "running":
            status = "shadow"
        elif last and not last["passed"]:
            status = f"refused at {last['stage']}"
        else:
            status = last["stage"] if last else "?"
        shadow = None
        if sh:
            shadow = {"start_day": sh["start_day"], "status": sh["status"], "trades": sh["trades"],
                      "min_trades": ev.shadow_min_trades, "score": sh["score"], "champion_score": sh["champion_score"],
                      "score_margin": ev.shadow_score_margin, "prob_better": sh["prob_better"],
                      "min_prob": ev.shadow_bootstrap_prob, "max_weeks": ev.shadow_max_weeks,
                      "weeks": json.loads(sh["detail"] or "{}").get("weeks"), "last_eval_day": sh["last_eval_day"]}
        out.append({**i, "status": status, "attempts": attempts, "steps": steps, "walk_forward": _summary(wf[0] if wf else None),
                    "holdout": _summary(ho[0] if ho else None), "shadow": shadow})
    return out


def _capital(conn) -> list[dict]:
    """Each strategy's share of its agent's equity now, and at this week's start."""
    out = []
    names = {(r["agent_id"], r["slot"]): r["name"] for r in conn.execute(
        "SELECT agent_id, slot, name FROM strategies WHERE status='active'")} if _has(conn, "strategies") else {}
    for (agent,) in conn.execute("SELECT agent_id FROM agents ORDER BY agent_id"):
        now = conn.execute("SELECT date, equity, slots_json FROM snapshots WHERE agent_id=? AND kind='daily' "
                           "ORDER BY seq DESC LIMIT 1", (agent,)).fetchone()
        wk = conn.execute("SELECT date, equity, slots_json FROM snapshots WHERE agent_id=? AND kind='post_trade' "
                          "ORDER BY seq DESC LIMIT 1", (agent,)).fetchone()
        if now is None or not now["equity"]:
            continue
        cur = {int(k): v / now["equity"] for k, v in json.loads(now["slots_json"]).items()}
        start = {int(k): v / wk["equity"] for k, v in json.loads(wk["slots_json"]).items()} if wk and wk["equity"] else {}
        for slot in sorted(set(cur) | set(start)):
            out.append({"agent": agent, "slot": slot,
                        "strategy": "cash reserve" if slot == 0 else names.get((agent, slot), f"slot {slot}"),
                        "share_pct": round(cur.get(slot, 0) * 100, 2),
                        "week_change_pct": round((cur.get(slot, 0) - start.get(slot, 0)) * 100, 2) if start else None,
                        "as_of": now["date"], "week_start": wk["date"] if wk else None})
    return out


def _adaptive(conn) -> list[dict]:
    rows = _rows(conn, """SELECT idea_key, purpose, results, created_at FROM backtest_runs
                          WHERE idea_key LIKE 'adaptive-5bc:%' AND status='ok' ORDER BY id DESC""")
    seen, out = set(), []
    for r in rows:
        k = (r["idea_key"], r["purpose"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"variant": r["idea_key"].split(":", 1)[1], "purpose": r["purpose"], **(_summary(r["results"]) or {})})
    return sorted(out, key=lambda x: (x["variant"], x["purpose"]))


def overview(conn: sqlite3.Connection) -> dict:
    lab = labconfig.get()
    base = {"evidence_rules": labconfig.evidence_enabled(), "history_job": labconfig.history_enabled(),
            "caveat": lab.survivorship_note, "holdout_months": lab.holdout_months}
    if not _has(conn, "market_states"):
        return {"available": False, **base}
    states = _rows(conn, "SELECT day, label, inputs FROM market_states WHERE label IS NOT NULL ORDER BY day DESC LIMIT 60")
    for s in states:
        s["inputs"] = json.loads(s["inputs"] or "{}")
    runs = _rows(conn, """SELECT id, created_at, requested_by, idea_key, attempt_no, strategy_name, purpose, status,
                                 period_start, period_end, results, error FROM backtest_runs
                          WHERE purpose != 'check' AND idea_key NOT LIKE 'adaptive-%' ORDER BY id DESC LIMIT 30""")
    for r in runs:
        r["results"] = _summary(r["results"])
        r["error"] = (r["error"] or "")[:300] or None
    return {
        "available": True, **base,
        "market_state": states[0] if states else None,
        "state_history": [{"day": s["day"], "label": s["label"]} for s in reversed(states)],
        "experiments": _experiments(conn),
        "strategy_by_state": _rows(conn, "SELECT * FROM strategy_by_market_state ORDER BY strategy, source, market_state"),
        "capital": _capital(conn),
        "risk_dial": {"live": False, "level_pct": 100,
                      "note": "The risk dial runs in backtests only (Phase 5); live exposure is not scaled yet."},
        "allocator": {"live": False,
                      "note": "Capital is split by the agents' own allocations; the regime allocator runs in backtests only."},
        "adaptive_study": _adaptive(conn),
        "recent_runs": runs,
    }
