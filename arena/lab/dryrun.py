"""Phase 4 dry run: one agent proposes a challenger and it goes through
backtest -> holdout -> shadow, on a THROWAWAY COPY of a league database.

    python -m arena.lab evidence-dry-run --from data/arena.db [--agent gpt --slot 1]

* No model is called: a scripted agent plays the weekly review through the
  real runner, tools and engine. It calls run_backtest (a design run, then a
  walk-forward), then submits a code challenger plus a lesson citing its
  walk-forward run.
* The source database is only read (copied); everything happens on the copy.
* Two things are set ON THE COPY so the review can reach the gates (both are
  covered by unit tests on their own and reported in the output): the
  strategy is flagged as having hit its drawdown limit (a rewrite trigger),
  and the cooldown and freeze are relaxed (the live strategies are a few
  days old, so both would refuse a rewrite today).
* The shadow stage starts the next trading day, so there is no new data to
  judge it on yet. To show the evaluation working, the copy's shadow record
  is then replayed from `replay_weeks` ago: this reuses data the gates
  already saw, so it shows the mechanics, not evidence.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..agents.base import AgentAdapter, Step, ToolCall, Usage
from ..prices import ET

CHALLENGER_CODE = '''
ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
        "XLC", "TLT", "IEF", "GLD", "VIXY"}

def target_weights(prices, date):
    """Top 5 six-month momentum stocks above their 200-day average, moves
    above 250% excluded, 15% each (a variant of the GPT champion's rules)."""
    close = prices["close"]
    close = close[[c for c in close.columns if c not in ETFS]]
    if len(close) < 200:
        return {}
    last = close.iloc[-1]
    sma = close.iloc[-200:].mean()
    r126 = close.iloc[-1] / close.iloc[-127] - 1
    ok = last.notna() & sma.notna() & r126.notna() & (last > sma) & (r126 < 2.5)
    top = r126[ok].sort_values(ascending=False).head(5)
    return {t: 0.15 for t in top.index}
'''
IDEA = "momentum-top5-capped"


class ScriptedReviewer(AgentAdapter):
    """Plays a fixed list of tool calls; records the tool results it got."""

    def __init__(self, agent_id: str, script: list):
        super().__init__(agent_id, f"dry-run-{agent_id}")
        self.script, self.received, self.n = list(script), [], 0

    def price_key(self):
        return "dry-run"

    def start(self, system, briefing, tools, web_search, max_web_searches):
        self.tools = [t["name"] for t in tools]

    def step(self) -> Step:
        self.n += 1
        if not self.script:
            return Step([], "done", Usage())
        item = self.script.pop(0)
        if callable(item):
            item = item(self)
        return Step([ToolCall(f"c{self.n}", name, args) for name, args in item], "", Usage(), 0, "tool_use")

    def send_tool_results(self, results):
        self.received.append([(c.name, json.loads(text) if text.startswith("{") else text) for c, text, _ in results])

    def send_user_text(self, text):
        self.received.append(text)


def run(source_db: str, agent: str = "gpt", slot: int = 1, replay_weeks: int = 8, keep: str | None = None) -> dict:
    from ..db import connect
    from ..lab import config as labconfig, evidence, histdata
    from ..learning import config as lconfig, review, strategies as strat

    tmp = tempfile.mkdtemp(prefix="arena_evidence_dry_")
    copy = str(Path(tmp) / "arena_copy.db")
    src = sqlite3.connect(f"file:{Path(source_db).resolve().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    env_before = {k: os.environ.get(k) for k in ("LEARNING_ENABLED", "EVIDENCE_RULES", "HISTORY_DB")}
    os.environ.update(LEARNING_ENABLED="1", EVIDENCE_RULES="1")
    os.environ.setdefault("HISTORY_DB", str(labconfig.history_db_path()))
    lab_get, learn_get = labconfig.get, lconfig.get
    lab_cfg, learn_cfg = labconfig.get(), lconfig.get()
    # Relaxed on the copy only (see the module docstring).
    labconfig.get = lambda: dataclasses.replace(lab_cfg, evidence=dataclasses.replace(
        lab_cfg.evidence, freeze_min_live_trades=0, freeze_min_weeks=0))
    lconfig.get = lambda: dataclasses.replace(learn_cfg, learning=dataclasses.replace(
        learn_cfg.learning, cooldown_weeks_after_change=0, min_trades_for_rewrite=0))
    conn = connect(copy)
    report: dict = {"copy": copy, "source": str(source_db), "agent": agent, "slot": slot,
                    "relaxed_on_copy": ["drawdown flag set (rewrite trigger)", "cooldown 0", "freeze 0",
                                        "min_trades_for_rewrite 0"]}
    try:
        evidence.ensure(conn)
        s = strat.in_slot(conn, agent, slot)
        if s is None:
            raise SystemExit(f"{agent} has no active strategy in slot {slot}")
        champions = {(x.agent_id, x.slot): x.version_id for a in ("claude", "gpt") for x in strat.active(conn, a)}
        live = {"trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                "positions": [tuple(r) for r in conn.execute("SELECT agent_id, slot, ticker, shares FROM positions "
                                                             "ORDER BY 1,2,3")],
                "pending_orders": conn.execute("SELECT COUNT(*) FROM pending_orders WHERE status='pending'").fetchone()[0]}
        strat.flag_emergency(conn, s.strategy_id, "dry run: rewrite trigger")
        now = datetime.now(timezone.utc).isoformat()
        day = datetime.now(ET).date().isoformat()

        def submit(agent_self):
            wf = next((r for step in agent_self.received if isinstance(step, list) for name, r in step
                       if name == "run_backtest" and isinstance(r, dict) and r.get("windows")), None)
            run_ids = [wf["run_id"]] if wf and wf.get("ok") else []
            return [("submit_review", {
                "reasoning": "[dry run] Backtested a top-5 capped momentum variant; proposing it as a challenger.",
                "confidence": 0.6,
                "lessons": [{"key": "capped-momentum-top5", "kind": "improve", "slot": slot,
                             "lesson": "A top-5 capped six-month momentum book may be steadier than top-4.",
                             "backtest_run_ids": run_ids}],
                "rewrite": {"slot": slot, "rules": "Top 5 six-month momentum names above their 200-day average, "
                            "moves above 250% excluded, 15% each.", "hypothesis": "More names, smaller weights: "
                            "lower drawdown for a similar return.", "trigger": "drawdown_limit",
                            "code": CHALLENGER_CODE, "idea_key": IDEA}}),]

        script = [
            [("run_backtest", {"idea_key": IDEA, "code": CHALLENGER_CODE, "purpose": "design",
                               "start": "2018-01-02", "end": "2021-12-31"})],
            [("run_backtest", {"idea_key": IDEA, "code": CHALLENGER_CODE, "purpose": "walk_forward"})],
            submit,
        ]
        bot = ScriptedReviewer(agent, script)
        res = review.run_review(conn, agent, "weekly", day, review.adapter_runner(lambda a: bot, copy), as_of=now)
        report["tools_offered"] = bot.tools
        report["tool_results"] = [
            {k: r.get(k) for k in ("ok", "run_id", "attempts", "trades", "total_return_pct", "risk_adjusted_score",
                                   "spy_return_pct", "budget_left_this_week", "refused", "error")}
            for step in bot.received if isinstance(step, list) for _, r in step if isinstance(r, dict)]
        report["review"] = {"status": res["status"], "applied": res["applied"], "rejected": res["rejected"]}
        hconn = histdata.connect()
        try:
            with conn:
                report["shadow_today"] = evidence.evaluate_shadows(conn, hconn, agent)
            row = conn.execute("SELECT * FROM lab_shadows WHERE agent_id=? AND status='running'", (agent,)).fetchone()
            if row and replay_weeks:
                start = hconn.execute("SELECT day FROM hist_calendar WHERE day <= date(?, ?) ORDER BY day DESC "
                                      "LIMIT 1", (day, f"-{replay_weeks * 7} days")).fetchone()[0]
                conn.execute("UPDATE lab_shadows SET start_day=? WHERE version_id=?", (start, row["version_id"]))
                with conn:
                    report["shadow_replay"] = {"start_day": start, "note": "replay on data the gates already saw: "
                                               "mechanics only", "result": evidence.evaluate_shadows(conn, hconn, agent)}
        finally:
            hconn.close()
        report["promotion_log"] = [dict(r) for r in conn.execute(
            "SELECT stage, passed, reason, run_ids FROM promotion_log ORDER BY id")]
        report["backtest_runs"] = [dict(r) for r in conn.execute(
            "SELECT id, requested_by, idea_key, attempt_no, purpose, status, period_start, period_end FROM backtest_runs "
            "ORDER BY id")]
        report["lessons"] = [dict(r) for r in conn.execute(
            "SELECT l.key, l.status, e.source, e.trades, e.purpose FROM lessons l JOIN lesson_evidence e "
            "ON e.lesson_id=l.id")]
        after = {(x.agent_id, x.slot): x.version_id for a in ("claude", "gpt") for x in strat.active(conn, a)}
        promoted = [dict(r) for r in conn.execute("SELECT version_id FROM promotion_log WHERE stage='promoted'")]
        live_after = {"trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                      "positions": [tuple(r) for r in conn.execute("SELECT agent_id, slot, ticker, shares FROM "
                                                                   "positions ORDER BY 1,2,3")],
                      "pending_orders": conn.execute("SELECT COUNT(*) FROM pending_orders WHERE status='pending'"
                                                     ).fetchone()[0]}
        report["champions_before"] = {f"{a}:{s_}": v for (a, s_), v in champions.items()}
        report["champions_after"] = {f"{a}:{s_}": v for (a, s_), v in after.items()}
        report["checks"] = {
            "champions_unchanged_except_a_promotion": all(
                after.get(k) == v or any(p["version_id"] == after.get(k) for p in promoted) for k, v in champions.items()),
            "no_trades_positions_or_orders_from_the_lab": live_after == live,
            "every_step_logged": len(report["promotion_log"]) > 0 and len(report["backtest_runs"]) > 0,
        }
    finally:
        conn.close()
        if keep:
            shutil.copyfile(copy, keep)      # e.g. to preview the dashboard on the result
        labconfig.get, lconfig.get = lab_get, learn_get
        for k, v in env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    return report
