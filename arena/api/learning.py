"""Read-only data for the dashboard's Learning section (arena/learning).

Every query is plain SELECT on the read-only connection. If the database
was made before the learning layer (its tables don't exist yet because the
engine hasn't started on the new code), the section reports itself as
unavailable instead of failing.
"""
from __future__ import annotations

import json
import sqlite3

from ..learning import config as lconfig


def _rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='decisions'").fetchone() is not None


def overview(conn: sqlite3.Connection) -> dict:
    enabled = lconfig.enabled()
    if not _available(conn):
        return {"available": False, "enabled": enabled}
    cfg = lconfig.get()
    regime = conn.execute("SELECT day, regime, vix, spy_change, regime_inputs FROM market_context WHERE kind='close' "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
    versions = _rows(conn, """
        SELECT s.agent_id AS agent, s.slot, s.name AS strategy, s.status AS strategy_status, v.version, v.role,
               v.change_kind, v.change_reason, v.hypothesis, v.params, v.rules_text, v.created_at
        FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id
        ORDER BY v.created_at DESC LIMIT 60""")
    for v in versions:
        v["params"] = json.loads(v["params"] or "{}")
        v["rules_text"] = (v["rules_text"] or "")[:600]
    lessons = _rows(conn, """
        SELECT l.agent_id AS agent, l.id, l.key, l.lesson, l.kind, l.regime, l.status, l.times_confirmed, l.source,
               l.retired_reason, l.updated_at, s.slot, s.name AS strategy
        FROM lessons l LEFT JOIN strategies s ON s.id = l.strategy_id
        WHERE l.status IN ('active', 'candidate') ORDER BY l.status, l.times_confirmed DESC, l.updated_at DESC LIMIT 80""")
    shadow = _rows(conn, """
        SELECT s.agent_id AS agent, s.slot, s.name AS strategy, v.version, v.hypothesis, r.week_no, r.start_date,
               r.end_date, r.return_pct, r.max_drawdown_pct, r.champion_return_pct, r.champion_max_drawdown_pct,
               r.verdict, r.note
        FROM shadow_runs r JOIN strategy_versions v ON v.id = r.version_id JOIN strategies s ON s.id = v.strategy_id
        ORDER BY r.id DESC LIMIT 30""")
    rejected = _rows(conn, "SELECT ts, agent_id AS agent, source, slot, kind, reason, detail FROM rejected_changes "
                           "ORDER BY id DESC LIMIT 60")
    for r in rejected:
        r["detail"] = (r["detail"] or "")[:400]
    reports = _rows(conn, """
        SELECT r.* FROM reports r WHERE r.week_no = (SELECT MAX(r2.week_no) FROM reports r2 WHERE r2.agent_id = r.agent_id)
        ORDER BY r.agent_id""")
    return {
        "available": True, "enabled": enabled, "start_week": cfg.scoring.start_week,
        "scoring": {"mode": cfg.scoring.mode, "floor_pct": cfg.scoring.drawdown_floor_pct},
        "regime": dict(regime) if regime else None,
        "weekly_scores": _rows(conn, "SELECT week_no, agent_id AS agent, week_start, mode, return_pct, max_drawdown_pct, "
                                     "broke_hard_limit, score, point_won, bonus_added FROM weekly_scores "
                                     "ORDER BY week_no DESC, agent_id LIMIT 40"),
        "strategy_by_regime": _rows(conn, "SELECT * FROM strategy_by_regime ORDER BY agent, strategy, regime"),
        "reports": reports, "lessons": lessons, "versions": versions, "shadow": shadow, "rejected": rejected,
        "reviews": _rows(conn, "SELECT kind, agent_id AS agent, day, status, attempts, finished_at FROM review_runs "
                               "WHERE dry_run=0 ORDER BY id DESC LIMIT 20"),
        "risk_checks": _rows(conn, "SELECT ts, day, positions, fired, woke FROM risk_checks ORDER BY id DESC LIMIT 10"),
    }
