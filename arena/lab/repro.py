"""Does the backtester reproduce a live strategy? (Phase 3 check)

For each week a live strategy ran, the coded version (strategies.py) goes
through the engine's own signal path (engine.signals: the frame cut at the
decision date) on the price data the agent saw, and its picks are compared
with the positions the agent actually submitted that week.
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

from . import engine
from .strategies import LIBRARY

# Which coded strategy matches which live (agent, week) slot-1 decision.
CASES = [
    ("gpt", 1, "gpt_six_month_momentum_uncapped"),
    ("gpt", 2, "gpt_six_month_momentum"),
    ("gpt", 3, "gpt_six_month_momentum"),
    ("claude", 1, "claude_momentum_leaders"),
    ("claude", 2, "claude_momentum_leaders"),
    ("claude", 3, "claude_sector_rotation"),
]


def live_decisions(conn: sqlite3.Connection) -> dict[tuple[str, int], dict]:
    """{(agent, week): {"decision_date", "positions"}} for slot 1 of each accepted turn."""
    out = {}
    for agent, week, day, raw in conn.execute(
            "SELECT t.agent_id, t.week_no, w.decision_date, t.decision_json FROM agent_turns t "
            "JOIN weeks w ON w.week_no = t.week_no WHERE t.status = 'accepted'"):
        d = json.loads(raw)
        slot1 = next((s for s in d.get("slots", []) if s.get("slot") == 1), None)
        if slot1:
            out[(agent, week)] = {"decision_date": day, "positions": slot1.get("positions") or {}}
    return out


def compare(frame: pd.DataFrame, decisions: dict[tuple[str, int], dict], universe: set[str]) -> list[dict]:
    rows = []
    for agent, week, name in CASES:
        live = decisions.get((agent, week))
        if live is None:
            continue
        fn, what = LIBRARY[name]
        day = live["decision_date"]
        sig = engine.signals(engine.StrategySpec(name, fn=fn), frame, [day], universe)[day]
        coded, actual = set(sig), {t for t, w in live["positions"].items() if w}
        rows.append({"agent": agent, "week": week, "decision_date": day, "strategy": name, "encodes": what,
                     "coded_picks": sorted(coded), "live_picks": sorted(actual),
                     "same": sorted(coded & actual), "match_pct": round(len(coded & actual) / max(len(actual), 1) * 100, 1),
                     "exact": coded == actual})
    return rows
