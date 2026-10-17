"""Daily and weekly reviews, and risk wake-ups (the LLM parts).

Flow of one review (run_review):
  1. build_input(): ONLY the data the spec allows, read "as of" the review's
     start time (anti-lookahead: outcomes evaluated before it, lessons and
     market context from before it)
  2. the prompt (instructions + that JSON) is stored verbatim in
     decision_inputs; the run is logged in review_runs
  3. the agent answers with structured JSON through one tool (submit_review).
     validate_output() checks the structure; invalid -> one retry; invalid
     again -> fall back to "hold" and log the failure
  4. apply_*() enforces every limit in code (param ranges, cooldowns, rewrite
     triggers and caps, regime confirmation for tilts, hard limits), logs each
     refused change in rejected_changes, applies the rest
  5. one decisions row per strategy (holds included), frozen

The agent never touches the database: the engine applies what it asked for.
"""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .. import alerts, ledger
from ..agents.base import Usage
from ..db import log_event, transaction
from ..prices import ET, get_price, price_on_or_before
from ..trading_calendar import last_trading_day_on_or_before
from . import collector, config as lconfig, execution, grader, hardlimits, journal, lessons
from . import strategies as strat
from .regime import current_run, tilt_allowed

KINDS = ("daily", "weekly", "risk")
CHECKPOINT = {"daily": "daily_review", "weekly": "weekly_review", "risk": "risk_exit"}
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
MAX_TEXT = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence():
    """The backtest lab's evidence rules (Phase 4) when EVIDENCE_RULES=1, else None."""
    from ..lab import config as labconfig
    if not labconfig.evidence_enabled():
        return None
    from ..lab import evidence
    return evidence


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


# ==========================================================================
# 1. Input
# ==========================================================================

def _slot_equity_series(conn, agent_id: str, slot: int, start_day: str, end_day: str) -> list[tuple[str, float]]:
    rows = conn.execute("SELECT date, slots_json FROM snapshots WHERE agent_id=? AND kind='daily' AND date>=? "
                        "AND date<=? ORDER BY seq", (agent_id, start_day, end_day)).fetchall()
    out = []
    for r in rows:
        v = json.loads(r["slots_json"]).get(str(slot))
        if v is not None:
            out.append((r["date"], v))
    return out


def strategy_summary(conn: sqlite3.Connection, s: strat.Strategy, as_of: str, day: str, weekly: bool) -> dict:
    snap = conn.execute("SELECT * FROM portfolio_snapshots WHERE agent_id=? AND slot=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                        (s.agent_id, s.slot, as_of)).fetchone()
    if snap:
        equity, positions = snap["equity"], json.loads(snap["positions"])
    else:
        val = ledger.value_agent(conn, s.agent_id, last_trading_day_on_or_before(conn, day), "close")
        equity = val.slot_equity.get(s.slot, 0.0)
        positions = [{"symbol": p["ticker"], "qty": round(p["shares"], 6), "avg_price": round(p["avg_price"], 4),
                      "price": round(val.prices[p["ticker"]], 4)} for p in ledger.positions(conn, s.agent_id)
                     if p["slot"] == s.slot]
    stops = {r["ticker"]: r["stop_loss"] for r in conn.execute(
        "SELECT ticker, stop_loss FROM risk_orders WHERE agent_id=? AND slot=?", (s.agent_id, s.slot))}
    for p in positions:
        p["stop_loss"] = stops.get(p["symbol"])
        if p.get("avg_price"):
            p["unrealized_pct"] = _r((p["price"] / p["avg_price"] - 1) * 100 * (1 if p["qty"] > 0 else -1), 2)
    # Week to date: since this week's post-trade snapshot.
    wk = conn.execute("SELECT date, slots_json FROM snapshots WHERE agent_id=? AND kind='post_trade' AND date<=? "
                      "ORDER BY seq DESC LIMIT 1", (s.agent_id, day)).fetchone()
    wtd = spy_wtd = None
    if wk:
        e0 = json.loads(wk["slots_json"]).get(str(s.slot))
        if e0:
            wtd = (equity / e0 - 1) * 100
        a, b = get_price(conn, "SPY", wk["date"], "open"), price_on_or_before(conn, "SPY", day)
        if a and b:
            spy_wtd = (b[1] / a - 1) * 100
    created_day = datetime.fromisoformat(s.created_at).astimezone(ET).date().isoformat()
    series = _slot_equity_series(conn, s.agent_id, s.slot, created_day, day)
    peak = max([v for _, v in series] + [equity]) if series else equity
    dd = (equity / peak - 1) * 100 if peak > 0 else None
    cool, cool_why = strat.in_cooldown(conn, s.strategy_id, as_of)
    out = {
        "slot": s.slot, "strategy": s.name, "strategy_id": s.strategy_id, "version": s.version,
        "rules": s.rules_text, "params": s.params,
        "equity": _r(equity, 2), "week_to_date_pct": _r(wtd, 3), "spy_week_to_date_pct": _r(spy_wtd, 3),
        "vs_spy_pct": _r(None if wtd is None or spy_wtd is None else wtd - spy_wtd, 3),
        "drawdown_from_peak_pct": _r(dd, 3), "open_positions": positions,
        "in_cooldown": cool, "cooldown": cool_why,
        "emergency_flagged": s.emergency_flagged_at is not None,
        "trades_since_start": strat.trades_since_start(conn, s),
    }
    if s.challenger:
        out["challenger"] = {"version": s.challenger["version"], "rules": s.challenger["rules_text"],
                             "params": s.challenger["params"], "hypothesis": s.challenger["hypothesis"]}
    ev = _evidence()
    if ev:
        frozen, why, _ = ev.freeze_status(conn, s, as_of)
        out["frozen"], out["freeze"] = frozen, why
        if s.challenger:
            sh = conn.execute("SELECT start_day, trades, score, champion_score, prob_better FROM lab_shadows "
                              "WHERE version_id=?", (s.challenger["version_id"],)).fetchone()
            out["challenger"]["shadow"] = dict(sh) if sh else None
    if weekly:
        c = lconfig.get().learning
        under, detail = strat.underperformed_spy(conn, s, c.underperform_spy_weeks)
        out["rewrite_triggers"] = {"underperformed_spy_weeks_in_a_row": under, "recent_weeks_vs_spy": detail,
                                   "drawdown_limit_hit": s.emergency_flagged_at is not None}
    return out


def build_input(conn: sqlite3.Connection, agent_id: str, kind: str, as_of: str, day: str,
                situation: dict | None = None) -> tuple[dict, list[int]]:
    """The review's whole input, and the ids of the lessons in it."""
    cfg = lconfig.get()
    hist = collector.regime_history(conn, as_of, 60)
    run = current_run(hist)
    last_tilt = conn.execute("SELECT MAX(created_date) FROM pending_orders WHERE agent_id=? AND kind='tilt' "
                             "AND status != 'skipped'", (agent_id,)).fetchone()[0]
    can_tilt, tilt_why = tilt_allowed(hist, cfg.regime.confirm_days, last_tilt)
    ctx_rows = conn.execute("SELECT * FROM market_context WHERE kind='close' AND ts<=? ORDER BY ts DESC LIMIT 5",
                            (as_of,)).fetchall()
    context = [{"day": r["day"], "spy_change_pct": r["spy_change"], "vix": r["vix"], "regime": r["regime"],
                "sector_moves_pct": json.loads(r["sector_moves"] or "{}")} for r in reversed(ctx_rows)]
    headlines = json.loads(ctx_rows[0]["headlines"] or "[]")[:5] if ctx_rows else []
    active = lessons.for_prompt(conn, agent_id, run.label, as_of)
    inp: dict = {
        "agent": agent_id, "kind": kind, "as_of": as_of, "day": day,
        "regime": {"current": run.label, "held_days": run.days_held, "since": run.started,
                   "tilt_allowed": can_tilt, "tilt_status": tilt_why},
        "market_context_last_5_days": context, "headlines": headlines,
        "strategies": [strategy_summary(conn, s, as_of, day, kind == "weekly")
                       for s in strat.active(conn, agent_id) if s.created_at <= as_of],
        "active_lessons": active,
    }
    ev = _evidence()
    if ev:
        ev.ensure(conn)
        for l in active:
            l["evidence"] = ev.lesson_evidence(conn, l["id"])
    if kind in ("daily", "weekly"):
        prev = conn.execute("SELECT MAX(as_of) FROM review_runs WHERE agent_id=? AND kind=? AND status IN ('ok','fallback') "
                            "AND as_of < ? AND dry_run=0", (agent_id, kind, as_of)).fetchone()[0]
        since = prev or (datetime.fromisoformat(as_of) - timedelta(days=3 if kind == "daily" else 8)).isoformat()
        inp["graded_decisions_since_last_review"] = grader.outcomes_as_of(conn, agent_id, as_of, since=since)
        inp["candidate_lessons"] = lessons.candidates(conn, agent_id, as_of)
        if ev:
            # Hypotheses: candidates, and active lessons from before the rules that
            # were never confirmed with backtest + live evidence.
            done = ev.confirmed(conn)
            extra = [l for l in lessons.active_all(conn, agent_id, as_of) if l["id"] not in done]
            hyp = inp.pop("candidate_lessons") + extra
            for l in hyp:
                l["evidence"] = ev.lesson_evidence(conn, l["id"])
            inp["unproven_hypotheses"] = hyp
        inp["limits"] = {"param_ranges": cfg.param_ranges, "max_tilt_pct": cfg.learning.max_tilt_pct,
                         "cooldown_weeks_after_change": cfg.learning.cooldown_weeks_after_change,
                         "lesson_confirmations_to_activate": cfg.learning.lesson_confirmations_to_activate}
    if kind == "weekly":
        inp["shadow_results"] = [dict(r) for r in conn.execute(
            "SELECT r.week_no, s.slot, v.version, r.verdict, r.note FROM shadow_runs r JOIN strategy_versions v "
            "ON v.id=r.version_id JOIN strategies s ON s.id=v.strategy_id WHERE s.agent_id=? AND r.evaluated_at IS NOT NULL "
            "AND r.evaluated_at < ? ORDER BY r.id DESC LIMIT 5", (agent_id, as_of))]
        rep = conn.execute("SELECT week_no, report_text FROM strategy_reports WHERE agent_id=? AND status='accepted' "
                           "AND finished_at < ? ORDER BY week_no DESC LIMIT 1", (agent_id, as_of)).fetchone()
        inp["latest_strategy_report"] = {"week": rep["week_no"], "text": rep["report_text"][:3000]} if rep else None
        ws = conn.execute("SELECT * FROM weekly_scores WHERE agent_id=? ORDER BY week_no DESC LIMIT 1", (agent_id,)).fetchone()
        inp["last_weekly_score"] = None if ws is None else {
            "week": ws["week_no"], "mode": ws["mode"], "return_pct": ws["return_pct"],
            "max_drawdown_pct": ws["max_drawdown_pct"], "score": ws["score"], "point_won": bool(ws["point_won"])}
        inp["limits"].update({"rewrites_per_agent_per_week": cfg.learning.rewrites_per_agent_per_week,
                              "min_trades_for_rewrite": cfg.learning.min_trades_for_rewrite,
                              "underperform_spy_weeks": cfg.learning.underperform_spy_weeks,
                              "rewrites_used_this_week": strat.changes_this_week(conn, agent_id, as_of)})
    if ev and kind == "weekly":
        c = ev.cfg()
        from ..lab import config as labconfig
        inp["backtest"] = {"budget_left_this_week": ev.budget_left(conn, agent_id, as_of),
                           "holdout_months": labconfig.get().holdout_months,
                           "data_note": labconfig.get().survivorship_note}
        inp["evidence_rules"] = {
            "freeze": {"min_live_trades": c.freeze_min_live_trades, "min_weeks": c.freeze_min_weeks},
            "challenger": {"min_oos_trades": c.min_oos_trades, "regime_margin_pct": c.regime_margin_pct,
                           "shadow_min_trades": c.shadow_min_trades, "shadow_score_margin": c.shadow_score_margin,
                           "shadow_bootstrap_prob": c.shadow_bootstrap_prob, "shadow_max_weeks": c.shadow_max_weeks},
            "lessons": {"min_backtest_trades": c.lesson_min_backtest_trades,
                        "min_live_trades": c.lesson_min_live_trades, "hypothesis_max_weeks": c.hypothesis_max_weeks}}
        inp["challenger_status"] = ev.status(conn, agent_id)
    if kind == "risk":
        inp["situation"] = situation or {}
    return inp, [l["id"] for l in active]


# ==========================================================================
# 2. Output format
# ==========================================================================

_LESSON = {"type": "object", "properties": {
    "key": {"type": "string", "description": "stable id, lowercase-with-dashes; reuse it to confirm a lesson"},
    "lesson": {"type": "string"}, "kind": {"type": "string", "enum": list(lessons.KINDS)},
    "regime": {"type": "string", "description": "optional: the regime it applies to"},
    "slot": {"type": "integer", "description": "optional: the strategy it is about"},
    "evidence_decisions": {"type": "array", "items": {"type": "integer"},
                           "description": "ids from graded_decisions_since_last_review that support it"}},
    "required": ["key", "lesson", "kind", "evidence_decisions"]}
_PARAMS = {"type": "object", "properties": {"stop_loss_pct": {"type": "number"}, "position_max_pct": {"type": "number"}}}
_PARAM_CHANGE = {"type": "object", "properties": {"slot": {"type": "integer"}, "params": _PARAMS,
                                                  "change_reason": {"type": "string"}},
                 "required": ["slot", "params", "change_reason"]}
_SLOT_NOTES = {"type": "array", "description": "optional: one line per strategy", "items": {
    "type": "object", "properties": {"slot": {"type": "integer"}, "note": {"type": "string"}}, "required": ["slot", "note"]}}

DAILY_SPEC = {
    "name": "submit_review",
    "description": "Submit your daily review. Call it exactly once. Everything you ask for is checked by the engine; "
                   "changes that break a limit are refused and logged, the rest is applied.",
    "parameters": {"type": "object", "properties": {
        "reasoning": {"type": "string"}, "confidence": {"type": "number", "description": "0-1"},
        "strategy_notes": _SLOT_NOTES,
        "tilt": {"type": "object", "description": "optional: new allocations (fraction of your equity) per slot; only "
                 "when regime.tilt_allowed is true", "properties": {
                     "allocations": {"type": "object", "additionalProperties": {"type": "number"}},
                     "reason": {"type": "string"}}, "required": ["allocations", "reason"]},
        "param_changes": {"type": "array", "items": _PARAM_CHANGE},
        "emergency_rewrites": {"type": "array", "description": "only for strategies with emergency_flagged=true",
                               "items": {"type": "object", "properties": {
                                   "slot": {"type": "integer"}, "rules": {"type": "string"}, "params": _PARAMS,
                                   "change_reason": {"type": "string"},
                                   "positions": {"type": "object", "additionalProperties": {"type": "number"},
                                                 "description": "optional: new weights within the slot, traded at the next open"},
                                   "risk_orders": {"type": "object", "description": "{ticker: {stop_loss: price}} for those positions"}},
                                   "required": ["slot", "rules", "change_reason"]}},
        "lessons": {"type": "array", "items": _LESSON},
    }, "required": ["reasoning", "confidence"]},
}

WEEKLY_SPEC = {
    "name": "submit_review",
    "description": "Submit your weekly review. Call it exactly once. The engine checks everything; refused changes "
                   "are logged. Your weekly portfolio decision follows separately, right after this review.",
    "parameters": {"type": "object", "properties": {
        "reasoning": {"type": "string"}, "confidence": {"type": "number", "description": "0-1"},
        "strategy_notes": _SLOT_NOTES,
        "param_changes": {"type": "array", "items": _PARAM_CHANGE},
        "lessons": {"type": "array", "items": _LESSON},
        "retire_lessons": {"type": "array", "items": {"type": "object", "properties": {
            "lesson_id": {"type": "integer"}, "reason": {"type": "string"},
            "evidence_decisions": {"type": "array", "items": {"type": "integer"}}},
            "required": ["lesson_id", "reason", "evidence_decisions"]}},
        "rewrite": {"type": "object", "description": "optional: at most one new strategy version (becomes a challenger "
                    "tested in shadow next week)", "properties": {
                        "slot": {"type": "integer"}, "rules": {"type": "string"}, "params": _PARAMS,
                        "hypothesis": {"type": "string"},
                        "trigger": {"type": "string", "enum": ["underperform_spy", "drawdown_limit", "lesson"]},
                        "lesson_id": {"type": "integer"}},
                    "required": ["slot", "rules", "hypothesis", "trigger"]},
    }, "required": ["reasoning", "confidence"]},
}

RISK_SPEC = {
    "name": "submit_review",
    "description": "Respond to the risk alert. Call it exactly once. You may only reduce or close positions or raise "
                   "cash (or hold); fills happen now at the latest price. Strategy rules can't be changed here.",
    "parameters": {"type": "object", "properties": {
        "reasoning": {"type": "string"}, "confidence": {"type": "number", "description": "0-1"},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["reduce", "close", "raise_cash", "hold"]},
            "slot": {"type": "integer"}, "ticker": {"type": "string"},
            "fraction": {"type": "number", "description": "reduce / raise_cash: share to sell, 0-1"}},
            "required": ["type", "slot"]}},
    }, "required": ["reasoning", "confidence", "actions"]},
}

SPECS = {"daily": DAILY_SPEC, "weekly": WEEKLY_SPEC, "risk": RISK_SPEC}

# Under the evidence rules (EVIDENCE_RULES=1): the daily review only records
# observations; a lesson may cite backtest runs; a rewrite must come as code.
_LESSON_EV = copy.deepcopy(_LESSON)
_LESSON_EV["properties"]["backtest_run_ids"] = {
    "type": "array", "items": {"type": "integer"},
    "description": "run_ids of your run_backtest results that support it (walk_forward runs count as evidence)"}
_LESSON_EV["properties"]["evidence_decisions"]["description"] = (
    "ids from graded_decisions_since_last_review that support it (live evidence)")
_LESSON_EV["required"] = ["key", "lesson", "kind"]

DAILY_SPEC_EV = {
    "name": "submit_review",
    "description": "Submit your daily review: observations only. Call it exactly once.",
    "parameters": {"type": "object", "properties": {
        "reasoning": {"type": "string"}, "confidence": {"type": "number", "description": "0-1"},
        "strategy_notes": _SLOT_NOTES,
        "lessons": {"type": "array", "items": _LESSON_EV},
    }, "required": ["reasoning", "confidence"]},
}
WEEKLY_SPEC_EV = copy.deepcopy(WEEKLY_SPEC)
WEEKLY_SPEC_EV["parameters"]["properties"]["lessons"] = {"type": "array", "items": _LESSON_EV}
_RW = WEEKLY_SPEC_EV["parameters"]["properties"]["rewrite"]
_RW["description"] = ("optional: at most one challenger for one strategy, AS CODE. The engine backtests it "
                      "(walk-forward, per market state, the locked holdout once), then shadow-trades it until it is "
                      "proven better; only then does it replace the champion.")
_RW["properties"]["code"] = {"type": "string", "description": "Python defining target_weights(prices, date)"}
_RW["properties"]["idea_key"] = {"type": "string", "description": "the idea_key you used with run_backtest"}
_RW["required"] = ["slot", "rules", "hypothesis", "trigger", "code", "idea_key"]


def spec_for(kind: str) -> dict:
    if _evidence() is None:
        return SPECS[kind]
    return {"daily": DAILY_SPEC_EV, "weekly": WEEKLY_SPEC_EV, "risk": RISK_SPEC}[kind]


def tools_for(kind: str) -> list[dict]:
    """The review's tools: submit_review, plus run_backtest in the weekly review under the rules."""
    ev = _evidence()
    return [spec_for(kind)] + ([ev.BACKTEST_SPEC] if ev and kind == "weekly" else [])


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _text(x, maxlen=MAX_TEXT) -> bool:
    return isinstance(x, str) and 0 < len(x.strip()) <= maxlen


def _ints(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in v)


def validate_output(kind: str, raw, inp: dict) -> list[str]:
    """Structure only (types, sizes, slots that exist). Limits are applied
    later, so a limit breach is a logged rejection, not a retry."""
    e: list[str] = []
    if not isinstance(raw, dict):
        return ["The review must be a JSON object."]
    rules_on = _evidence() is not None
    if not _text(raw.get("reasoning"), 4000):
        e.append("reasoning: a non-empty string (max 4000 characters) is required.")
    if not _num(raw.get("confidence")) or not 0 <= raw["confidence"] <= 1:
        e.append("confidence: a number from 0 to 1 is required.")
    slots = {s["slot"] for s in inp.get("strategies", [])}

    def slot_ok(v, where):
        if not isinstance(v, int) or isinstance(v, bool):
            e.append(f"{where}.slot must be an integer.")
            return False
        if v not in slots:
            e.append(f"{where}.slot {v} has no active strategy (yours: {sorted(slots)}).")
            return False
        return True

    def lst(name, cap):
        v = raw.get(name)
        if v is None:
            return []
        if not isinstance(v, list) or len(v) > cap:
            e.append(f"{name} must be a list of at most {cap} items.")
            return []
        return v

    for i, n in enumerate(lst("strategy_notes", 5)):
        if not isinstance(n, dict) or not _text(n.get("note"), 600):
            e.append(f"strategy_notes[{i}] needs a note (max 600 characters).")
        else:
            slot_ok(n.get("slot"), f"strategy_notes[{i}]")
    if kind == "risk":
        acts = raw.get("actions")
        if not isinstance(acts, list) or not acts or len(acts) > 20:
            e.append("actions must be a list of 1-20 items (use [{\"type\": \"hold\", \"slot\": N}] to do nothing).")
            return e
        for i, a in enumerate(acts):
            if not isinstance(a, dict) or a.get("type") not in ("reduce", "close", "raise_cash", "hold"):
                e.append(f"actions[{i}].type must be reduce, close, raise_cash or hold.")
                continue
            slot_ok(a.get("slot"), f"actions[{i}]")
            if a["type"] in ("reduce", "close") and not isinstance(a.get("ticker"), str):
                e.append(f"actions[{i}]: {a['type']} needs a ticker.")
            if a["type"] in ("reduce", "raise_cash") and (not _num(a.get("fraction")) or not 0 < a["fraction"] <= 1):
                e.append(f"actions[{i}]: fraction must be a number in (0, 1].")
        return e
    for i, p in enumerate(lst("param_changes", 5)):
        if not isinstance(p, dict) or not isinstance(p.get("params"), dict) or not _text(p.get("change_reason"), 600):
            e.append(f"param_changes[{i}] needs slot, params (object) and change_reason.")
        else:
            slot_ok(p.get("slot"), f"param_changes[{i}]")
    for i, l in enumerate(lst("lessons", 10)):
        if not isinstance(l, dict):
            e.append(f"lessons[{i}] must be an object.")
            continue
        if not KEY.match(str(l.get("key", ""))):
            e.append(f"lessons[{i}].key must look like 'breakouts-fail-choppy' (lowercase, dashes, 2-60 chars).")
        if not _text(l.get("lesson"), 600):
            e.append(f"lessons[{i}].lesson is required (max 600 characters).")
        if l.get("kind") not in lessons.KINDS:
            e.append(f"lessons[{i}].kind must be one of {', '.join(lessons.KINDS)}.")
        ev = l.get("evidence_decisions")
        if rules_on:
            runs_ = l.get("backtest_run_ids")
            if (ev is not None and not _ints(ev)) or (runs_ is not None and not _ints(runs_)) or not (ev or runs_):
                e.append(f"lessons[{i}] must cite evidence: evidence_decisions (graded decision ids) and/or "
                         f"backtest_run_ids (your run_backtest run ids).")
        elif not isinstance(ev, list) or not ev or not all(isinstance(x, int) and not isinstance(x, bool) for x in ev):
            e.append(f"lessons[{i}].evidence_decisions must be a non-empty list of decision ids.")
        if l.get("regime") is not None and l["regime"] not in lconfig.get().regime.labels:
            e.append(f"lessons[{i}].regime must be one of {', '.join(lconfig.get().regime.labels)}.")
        if l.get("slot") is not None:
            slot_ok(l.get("slot"), f"lessons[{i}]")
    if kind == "daily" and rules_on:
        for k in ("tilt", "param_changes", "emergency_rewrites"):
            if raw.get(k):
                e.append(f"{k}: the daily review only records observations (strategy_notes, lessons); changes "
                         f"go through the weekly review. The hourly risk check still cuts risk.")
    if kind == "daily" and not rules_on:
        t = raw.get("tilt")
        if t is not None:
            if (not isinstance(t, dict) or not isinstance(t.get("allocations"), dict) or not _text(t.get("reason"), 600)
                    or not all(_num(v) and v >= 0 for v in t["allocations"].values())):
                e.append("tilt needs allocations {slot: fraction >= 0} and a reason.")
            else:
                for k in t["allocations"]:
                    if not str(k).isdigit() or int(k) not in slots:
                        e.append(f"tilt.allocations: slot {k} has no active strategy.")
        for i, r in enumerate(lst("emergency_rewrites", 5)):
            if not isinstance(r, dict) or not _text(r.get("rules"), 4000) or not _text(r.get("change_reason"), 600):
                e.append(f"emergency_rewrites[{i}] needs slot, rules and change_reason.")
                continue
            slot_ok(r.get("slot"), f"emergency_rewrites[{i}]")
            if r.get("positions") is not None and (not isinstance(r["positions"], dict)
                                                   or not all(_num(v) for v in r["positions"].values())):
                e.append(f"emergency_rewrites[{i}].positions must map tickers to numeric weights.")
    if kind == "weekly":
        for i, r in enumerate(lst("retire_lessons", 10)):
            if (not isinstance(r, dict) or not isinstance(r.get("lesson_id"), int) or not _text(r.get("reason"), 600)
                    or not isinstance(r.get("evidence_decisions"), list) or not r["evidence_decisions"]):
                e.append(f"retire_lessons[{i}] needs lesson_id, reason and evidence_decisions.")
        rw = raw.get("rewrite")
        if rw is not None:
            if (not isinstance(rw, dict) or not _text(rw.get("rules"), 4000) or not _text(rw.get("hypothesis"), 2000)
                    or rw.get("trigger") not in ("underperform_spy", "drawdown_limit", "lesson")):
                e.append("rewrite needs slot, rules, hypothesis and trigger (underperform_spy | drawdown_limit | lesson).")
            elif rules_on and (not _text(rw.get("code"), 20000) or "target_weights" not in rw["code"]
                               or not KEY.match(str(rw.get("idea_key", "")))):
                e.append("rewrite needs `code` defining target_weights(prices, date) and an `idea_key` "
                         "(lowercase-with-dashes): challengers are backtested before they trade in shadow.")
            else:
                slot_ok(rw.get("slot"), "rewrite")
    return e


# ==========================================================================
# 3. Prompt
# ==========================================================================

INTRO = {
    "daily": """You are "{agent}" in the AI Trading League. This is your DAILY REVIEW after today's close. It is not a trading turn: you can't research or pick new stocks here.

You may (all optional, every limit is enforced by the engine and refused changes are logged):
1. tilt: move capital between your strategies, ONLY if regime.tilt_allowed is true (a regime change has held for the confirmation period). At most limits.max_tilt_pct of your equity may move. Trades at the next open.
2. param_changes: adjust a strategy's params within limits.param_ranges. Not for strategies in cooldown. Each change makes a new strategy version; give a change_reason.
3. emergency_rewrites: rewrite a strategy's rules, ONLY for strategies with emergency_flagged = true (drawdown limit hit).
4. lessons: add candidate lessons, or confirm one by reusing its key from candidate_lessons/active_lessons. Each needs evidence_decisions: ids from graded_decisions_since_last_review. A lesson becomes active after limits.lesson_confirmations_to_activate confirmations on different days.
Holding (changing nothing) is a valid decision. Give reasoning and a confidence (0-1).""",
    "weekly": """You are "{agent}" in the AI Trading League. This is your WEEKLY REVIEW. Your weekly portfolio decision follows right after it, so don't pick positions here.

You may (all optional, every limit is enforced by the engine and refused changes are logged):
1. lessons / retire_lessons: confirm or add candidate lessons (with evidence_decisions), and retire lessons that recent evidence contradicts (with evidence).
2. param_changes: within limits.param_ranges, not for strategies in cooldown.
3. rewrite: at most ONE new version of one strategy, with a written hypothesis. Allowed only if a trigger holds (underperform_spy: trailed SPY limits.underperform_spy_weeks weeks in a row; drawdown_limit: emergency_flagged; lesson: an active lesson supports it, give lesson_id), the strategy has >= limits.min_trades_for_rewrite trades, isn't in cooldown, and you haven't used this week's rewrite. The new version starts as a CHALLENGER: next week you give its positions too, they run in shadow (no money), and it replaces the current version only if it beats it on both return and drawdown.
Give reasoning and a confidence (0-1).""",
    "risk": """You are "{agent}" in the AI Trading League. RISK ALERT: the engine's hourly risk check fired for your portfolio (see situation).

Allowed actions only: reduce (sell a fraction of a position), close (a whole position), raise_cash (sell a fraction of every long in a strategy), or hold. Fills happen NOW at the latest market price. You can't buy, open positions or change strategy rules here. Give reasoning and a confidence (0-1).""",
}


INTRO_EV = {
    "daily": """You are "{agent}" in the AI Trading League. This is your DAILY REVIEW after today's close. Under the evidence rules it only RECORDS OBSERVATIONS: no backtests, no strategy, param or capital changes (those go through the weekly review; the hourly risk check still cuts risk).

You may add strategy_notes and lessons. Every lesson starts as an UNPROVEN HYPOTHESIS and must cite evidence: evidence_decisions (ids from graded_decisions_since_last_review). Reuse a key to add evidence to an existing hypothesis. Only hypotheses later supported by BOTH a walk-forward backtest and enough live trades become confirmed rules (see unproven_hypotheses for what each has so far).
Give reasoning and a confidence (0-1).""",
    "weekly": """You are "{agent}" in the AI Trading League. This is your WEEKLY REVIEW. Your weekly portfolio decision follows right after it, so don't pick positions here. Live trading confirms ideas; history is where you test them.

Tools: run_backtest (test ideas on daily history since 2016; budget in backtest.budget_left_this_week; the most recent months are a locked holdout) and submit_review (call once at the end).

You may (all optional, every rule is enforced by the engine and refused changes are logged):
1. lessons / retire_lessons: lessons are hypotheses until confirmed. Cite evidence: evidence_decisions (graded live decisions) and/or backtest_run_ids (your run_backtest runs). A hypothesis becomes a confirmed rule only with a walk-forward run of >= evidence_rules.lessons.min_backtest_trades trades AND >= min_live_trades live trades; unconfirmed after hypothesis_max_weeks it is retired. Only confirmed lessons (active_lessons) are rules.
2. param_changes: within limits.param_ranges, only for strategies that are not frozen (see each strategy's `freeze`).
3. rewrite: at most ONE challenger, written AS CODE (target_weights(prices, date)) with the idea_key you backtested it under, for a strategy that is not frozen and has a trigger. The engine then runs, in order: a walk-forward with >= evidence_rules.challenger.min_oos_trades out-of-sample trades and a better score than your champion; no market state worse than the champion by more than regime_margin_pct; the locked holdout ONCE (fail = the idea is retired for good); then shadow trading (no money) until proven. Many tries per idea are visible next to the results.
Give reasoning and a confidence (0-1).""",
}


def render_prompt(kind: str, inp: dict) -> str:
    intro = INTRO_EV.get(kind) if _evidence() is not None else None
    return ((intro or INTRO[kind]).format(agent=inp["agent"]) + "\n\nINPUT (JSON):\n" + json.dumps(inp, indent=1, default=str)
            + "\n\nCall the submit_review tool (MCP server \"arena\") exactly once with your answer.")


# ==========================================================================
# 4. Running the agent
# ==========================================================================

@dataclass
class RunnerResult:
    output: dict | None              # the validated output, or None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None         # why there is no output (crash, timeout, ...)


class Runner(Protocol):
    def __call__(self, agent_id: str, kind: str, prompt: str, inp: dict) -> RunnerResult: ...


MAX_ATTEMPTS = 2   # the first answer + one retry; then fall back to hold


def adapter_runner(factory, db_path: str | None = None) -> Runner:
    """AGENT_BACKEND=api: a pay-per-token adapter, the submit tool only (plus
    run_backtest in the weekly review under the evidence rules)."""
    def run(agent_id: str, kind: str, prompt: str, inp: dict) -> RunnerResult:
        adapter = factory(agent_id)
        res = RunnerResult(None)
        tools = tools_for(kind)
        adapter.start("You are a careful portfolio manager. Answer only through the submit_review tool.",
                      prompt, tools, False, 0)
        nudges = 0
        tool_steps = 0
        while res.attempts < MAX_ATTEMPTS:
            try:
                step = adapter.step()
            except Exception as e:  # noqa: BLE001
                res.error = f"API error ({type(e).__name__}): {str(e)[:300]}"
                return res
            res.usage.add(step.usage)
            bt = [c for c in step.tool_calls if c.name == "run_backtest"]
            if bt and len(tools) > 1 and tool_steps < 40:
                tool_steps += 1
                adapter.send_tool_results([(c, json.dumps(run_backtest_tool(db_path, agent_id, c.args or {}),
                                                         default=str), False) for c in bt])
                continue
            calls = [c for c in step.tool_calls if c.name == "submit_review"]
            if not calls:
                if nudges >= 2:
                    res.error = "The model stopped without calling submit_review."
                    return res
                nudges += 1
                adapter.send_user_text("Call submit_review now with your answer.")
                continue
            c = calls[0]
            res.attempts += 1
            errs = [f"could not parse your arguments: {c.parse_error}"] if c.parse_error else \
                validate_output(kind, c.args or {}, inp)
            if not errs:
                res.output = c.args
                return res
            res.errors = errs
            adapter.send_tool_results([(c, "REJECTED:\n- " + "\n- ".join(errs) + "\nFix it and call submit_review "
                                        "again (last try).", True)])
        return res
    return run


def cli_runner(factory, db_path: str) -> Runner:
    """AGENT_BACKEND=cli: Claude Code / Codex with our MCP server in review mode
    (one tool, submit_review; structure validated by the server)."""
    import shutil
    import sys
    import tempfile
    from pathlib import Path

    from ..agents.cli import CliRun, McpLaunch
    from ..config import PROJECT_ROOT, SETTINGS

    def run(agent_id: str, kind: str, prompt: str, inp: dict) -> RunnerResult:
        agent = factory(agent_id)
        workdir = Path(tempfile.mkdtemp(prefix=f"arena_review_{agent_id}_"))
        result_path, input_path = workdir / "result.json", workdir / "input.json"
        input_path.write_text(json.dumps(inp, default=str), encoding="utf-8")
        mcp = McpLaunch(sys.executable, ["-m", "arena.mcp_server", "--db", str(db_path), "--agent", agent_id,
                                         "--result", str(result_path), "--review", kind,
                                         "--review-input", str(input_path)],
                        {"PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"})
        res = RunnerResult(None)
        runs: list[CliRun] = []
        try:
            # Backtests take time: a weekly review with run_backtest gets up to an hour.
            timeout = SETTINGS.report_timeout_sec if len(tools_for(kind)) == 1 else max(SETTINGS.report_timeout_sec, 3600)
            runs.append(agent.run(prompt, workdir, mcp, web_search=False, timeout=timeout))
            state = _read(result_path)
            if not state.get("accepted") and state.get("attempts", 0) < MAX_ATTEMPTS and runs[-1].session_id \
                    and "timed out" not in (runs[-1].error or ""):
                runs.append(agent.run("Call submit_review now with your answer.", workdir, mcp, web_search=False,
                                      resume_session=runs[-1].session_id, timeout=SETTINGS.report_timeout_sec))
                state = _read(result_path)
        except Exception as e:  # noqa: BLE001
            state = _read(result_path)
            res.error = f"Review turn crashed: {type(e).__name__}: {e}"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        for r in runs:
            res.usage.add(r.usage)
        res.attempts = state.get("attempts", 0)
        res.errors = state.get("last_errors", [])
        if state.get("accepted"):
            res.output = state["output"]
        elif not res.error:
            res.error = (runs[-1].error if runs and runs[-1].error else
                         "The agent didn't submit a valid review." if res.attempts else
                         "The agent didn't call submit_review.")
        return res
    return run


def run_backtest_tool(db_path: str | None, agent_id: str, args: dict) -> dict:
    """The run_backtest tool, for both runners (the CLI one through the MCP server)."""
    from ..config import SETTINGS
    from ..db import connect
    from ..lab import config as labconfig, evidence, histdata
    db = db_path or str(SETTINGS.db_path)
    conn, hconn = connect(db), histdata.connect(labconfig.history_db_for(db))
    try:
        return evidence.agent_backtest(conn, hconn, agent_id, args)
    except Exception as e:  # noqa: BLE001 - a tool error goes back to the agent
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}
    finally:
        conn.close()
        hconn.close()


def _read(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def default_runner(db_path: str) -> Runner:
    from ..agents import make_agent
    from ..config import SETTINGS
    return cli_runner(make_agent, db_path) if SETTINGS.agent_backend == "cli" else adapter_runner(make_agent, db_path)


# ==========================================================================
# 5. Applying (every limit enforced here)
# ==========================================================================

@dataclass
class Ctx:
    conn: sqlite3.Connection
    agent_id: str
    kind: str
    run_id: int
    as_of: str
    day: str
    inp: dict
    input_sha: str
    lesson_ids: list[int]
    regime: str | None
    dry_run: bool
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return CHECKPOINT[self.kind] if self.kind != "risk" else "risk_check"

    def reject(self, slot, kind: str, detail: dict, reason: str) -> None:
        hardlimits.reject(self.conn, self.agent_id, self.source, slot, kind, detail, reason, self.run_id)
        self.rejected.append(f"{kind}{'' if slot is None else ' slot ' + str(slot)}: {reason}")


def _visible_decisions(ctx: Ctx) -> set[int]:
    """Decision ids the agent may cite: its own, graded before the review."""
    return {r[0] for r in ctx.conn.execute(
        "SELECT DISTINCT h.decision_id FROM outcomes_history h JOIN decisions d ON d.id=h.decision_id "
        "WHERE d.agent_id=? AND h.evaluated_at < ?", (ctx.agent_id, ctx.as_of))}


def apply_lessons(ctx: Ctx, raw: dict) -> None:
    visible = _visible_decisions(ctx)
    rules = _evidence()
    for l in raw.get("lessons") or []:
        ev = [i for i in (l.get("evidence_decisions") or []) if i in visible]
        run_ids = [r for r in (l.get("backtest_run_ids") or []) if isinstance(r, int)] if rules else []
        if not ev and not run_ids:
            ctx.reject(l.get("slot"), "lesson", l, "no valid evidence: cite ids of your decisions that were already graded"
                       + (" or your backtest run ids" if rules else ""))
            continue
        s = strat.in_slot(ctx.conn, ctx.agent_id, l["slot"]) if l.get("slot") is not None else None
        lid, what = lessons.upsert(ctx.conn, agent_id=ctx.agent_id, key=l["key"], lesson=l["lesson"], kind=l["kind"],
                                   evidence=ev, day=ctx.day, regime=l.get("regime"),
                                   strategy_id=s.strategy_id if s else None, at=ctx.as_of)
        if rules:
            for note in rules.record_lesson_evidence(ctx.conn, ctx.agent_id, lid, ev, run_ids, ctx.as_of):
                ctx.reject(l.get("slot"), "lesson_evidence", {"key": l["key"]}, note)
        ctx.applied.append(f"lesson {l['key']} (#{lid}): {what}" + (" (hypothesis)" if rules else ""))


def apply_params(ctx: Ctx, raw: dict) -> set[int]:
    changed = set()
    for p in raw.get("param_changes") or []:
        s = strat.in_slot(ctx.conn, ctx.agent_id, p["slot"])
        errs = strat.check_params(p["params"])
        cool, why = strat.in_cooldown(ctx.conn, s.strategy_id, ctx.as_of)
        rules = _evidence()
        if rules and not errs:
            frozen, fwhy, _ = rules.freeze_status(ctx.conn, s, ctx.as_of)
            if frozen:
                ctx.reject(p["slot"], "param", p, fwhy)
                continue
        if errs:
            ctx.reject(p["slot"], "param", p, "; ".join(errs))
        elif cool:
            ctx.reject(p["slot"], "param", p, f"strategy in cooldown: {why}")
        elif p["slot"] in changed:
            ctx.reject(p["slot"], "param", p, "one param change per strategy per review")
        else:
            params = {**s.params, **p["params"]}
            if params == s.params:
                ctx.reject(p["slot"], "param", p, "no change to the current params")
                continue
            vid = strat.add_version(ctx.conn, s.strategy_id, s.rules_text, params, "param_tune",
                                    p["change_reason"], "champion", _week(ctx.conn), at=ctx.as_of)
            changed.add(p["slot"])
            ctx.applied.append(f"slot {p['slot']}: params {s.params} -> {params} (version id {vid})")
    return changed


def _week(conn) -> int | None:
    return hardlimits.current_week(conn)


def apply_tilt(ctx: Ctx, raw: dict) -> dict[int, tuple[float, float]]:
    """Queue a capital tilt; returns {slot: (old allocation, new allocation)}."""
    t = raw.get("tilt")
    if not t:
        return {}
    cfg = lconfig.get()
    reg = ctx.inp["regime"]
    if not reg["tilt_allowed"]:
        ctx.reject(None, "tilt", t, f"tilting is not allowed now: {reg['tilt_status']}")
        return {}
    val = ledger.value_agent(ctx.conn, ctx.agent_id, ctx.day, "close")
    if val.equity <= 0:
        ctx.reject(None, "tilt", t, "no equity")
        return {}
    cur = {s["slot"]: val.slot_equity.get(s["slot"], 0.0) / val.equity for s in ctx.inp["strategies"]}
    new = dict(cur)
    new.update({int(k): float(v) for k, v in t["allocations"].items()})
    moved = sum(abs(new[k] - cur[k]) for k in new) / 2
    if sum(new.values()) > 1 + 1e-6:
        ctx.reject(None, "tilt", t, f"allocations add up to {sum(new.values()):.2%} (> 100%)")
        return {}
    if moved > cfg.learning.max_tilt_pct + 1e-6:
        ctx.reject(None, "tilt", t, f"moves {moved:.1%} of equity; the maximum per tilt is {cfg.learning.max_tilt_pct:.0%}")
        return {}
    if moved < 0.005:
        ctx.reject(None, "tilt", t, "no meaningful change")
        return {}
    execution.queue(ctx.conn, ctx.agent_id, ctx.day, "tilt", {"allocations": {str(k): v for k, v in new.items()}},
                    f"daily review {ctx.run_id}: {t['reason']}")
    ctx.applied.append(f"tilt queued for the next open ({moved:.1%} of equity): "
                       + ", ".join(f"slot {k} {cur[k]:.1%}->{new[k]:.1%}" for k in sorted(new) if abs(new[k] - cur[k]) > 1e-4))
    return {k: (cur[k], new[k]) for k in new if abs(new[k] - cur[k]) > 1e-4}


def apply_emergency(ctx: Ctx, raw: dict) -> set[int]:
    from ..agents.decision import held_removed_weights
    from ..risk import validate_portfolio
    done = set()
    for r in raw.get("emergency_rewrites") or []:
        s = strat.in_slot(ctx.conn, ctx.agent_id, r["slot"])
        if s.emergency_flagged_at is None:
            ctx.reject(r["slot"], "emergency_rewrite", r, "this strategy is not flagged by the drawdown limit")
            continue
        params = {**s.params, **(r.get("params") or {})}
        errs = strat.check_params(params)
        if errs:
            ctx.reject(r["slot"], "emergency_rewrite", r, "; ".join(errs))
            continue
        payload = None
        if r.get("positions") is not None:
            val = ledger.value_agent(ctx.conn, ctx.agent_id, ctx.day, "close")
            targets = execution.current_targets(ctx.conn, ctx.agent_id, val)
            alloc = val.slot_equity.get(r["slot"], 0.0) / val.equity if val.equity > 0 else 0.0
            new = ledger.SlotTarget(r["slot"], alloc, {k.upper().strip(): float(v) for k, v in r["positions"].items()},
                                    s.name, "manual", risk_orders={k.upper().strip(): v for k, v in (r.get("risk_orders") or {}).items()
                                                                  if isinstance(v, dict)},
                                    reason=r["change_reason"])
            plan = [t for t in targets if t.slot != r["slot"]] + [new]
            last = {t: p[1] for t in new.weights if (p := price_on_or_before(ctx.conn, t, ctx.day))}
            res = validate_portfolio(plan, last, reduce_only=held_removed_weights(ctx.conn, ctx.agent_id, ctx.day))
            errs = list(res.errors)
            errs += [e for e in strategy_rule_errors(new, params, last)]
            if hardlimits.active(ctx.conn):
                held = {(t.slot, k): w for t in targets for k, w in t.weights.items()}
                errs += hardlimits.validate_slots(ctx.conn, [new], ctx.day, held)
            if errs:
                ctx.reject(r["slot"], "emergency_rewrite", r, "; ".join(errs)[:900])
                continue
            payload = {"slot": r["slot"], "allocation": alloc, "positions": new.weights, "risk_orders": new.risk_orders,
                       "name": s.name, "reason": r["change_reason"]}
        vid = strat.add_version(ctx.conn, s.strategy_id, r["rules"], params, "emergency_rewrite", r["change_reason"],
                                "champion", _week(ctx.conn), at=ctx.as_of)
        strat.clear_emergency(ctx.conn, s.strategy_id)
        if payload:
            execution.queue(ctx.conn, ctx.agent_id, ctx.day, "emergency", payload, f"emergency rewrite v{vid}")
        done.add(r["slot"])
        ctx.applied.append(f"slot {r['slot']}: emergency rewrite (version id {vid})"
                           + (" + new positions at the next open" if payload else ""))
    return done


def strategy_rule_errors(t: ledger.SlotTarget, params: dict, last: dict[str, float]) -> list[str]:
    """A plan for a slot must follow its strategy's params: position size
    <= position_max_pct, and a stop no further than stop_loss_pct away."""
    errs = []
    pmax, sl = params.get("position_max_pct"), params.get("stop_loss_pct")
    for ticker, w in t.weights.items():
        if not w:
            continue
        if pmax is not None and abs(w) > pmax + 1e-6:
            errs.append(f"Slot {t.slot} {ticker}: weight {abs(w):.0%} is above the strategy's position_max_pct {pmax:.0%}.")
        stop = (t.risk_orders.get(ticker) or {}).get("stop_loss")
        px = last.get(ticker)
        if sl is not None and stop and px:
            dist = (px - stop) / px if w > 0 else (stop - px) / px
            if dist > sl + 1e-6:
                errs.append(f"Slot {t.slot} {ticker}: stop {stop} is {dist:.1%} from the last close {px:.2f}; the "
                            f"strategy's stop_loss_pct is {sl:.0%}.")
    return errs


def apply_weekly(ctx: Ctx, raw: dict) -> None:
    visible = _visible_decisions(ctx)
    for r in raw.get("retire_lessons") or []:
        ev = [i for i in r["evidence_decisions"] if i in visible]
        if not ev:
            ctx.reject(None, "retire_lesson", r, "no valid evidence")
        elif lessons.retire(ctx.conn, ctx.agent_id, r["lesson_id"], f"{r['reason']} (evidence {ev})", at=ctx.as_of):
            ctx.applied.append(f"lesson #{r['lesson_id']} retired")
        else:
            ctx.reject(None, "retire_lesson", r, "not one of your lessons, or already retired")
    rw = raw.get("rewrite")
    if rw:
        s = strat.in_slot(ctx.conn, ctx.agent_id, rw["slot"])
        params = {**s.params, **(rw.get("params") or {})}
        errs = strat.check_params(params)
        ok, why = strat.rewrite_allowed(ctx.conn, s, rw["trigger"], rw.get("lesson_id"), ctx.as_of)
        if errs:
            ctx.reject(rw["slot"], "rewrite", rw, "; ".join(errs))
        elif not ok:
            ctx.reject(rw["slot"], "rewrite", rw, why)
        elif _evidence() is not None:
            hconn = _history_conn(ctx)
            try:
                accepted, msg, vid = _evidence().propose(ctx.conn, hconn, ctx.agent_id, s, rw, ctx.as_of, _week(ctx.conn))
            finally:
                hconn.close()
            if accepted:
                ctx.applied.append(f"slot {rw['slot']}: {msg}")
            else:
                ctx.reject(rw["slot"], "rewrite", {k: v for k, v in rw.items() if k != "code"}, msg)
        else:
            vid = strat.add_version(ctx.conn, s.strategy_id, rw["rules"], params, "weekly_rewrite",
                                    f"trigger {rw['trigger']}: {why}", "challenger", _week(ctx.conn),
                                    hypothesis=rw["hypothesis"], at=ctx.as_of)
            ctx.applied.append(f"slot {rw['slot']}: challenger v{strat.version_row(ctx.conn, vid)['version']} "
                               f"created (shadow test next week)")


def _history_conn(ctx: Ctx):
    from ..lab import config as labconfig, histdata
    db = ctx.conn.execute("PRAGMA database_list").fetchone()[2]
    return histdata.connect(labconfig.history_db_for(db) if db else None)


def _record_rows(ctx: Ctx, raw: dict | None, fallback_error: str | None,
                 tilt: dict[int, tuple[float, float]], changed: set[int]) -> list[int]:
    """One decisions row per strategy (holds included). A tilt is recorded as
    buy / sell of the strategy itself (symbol empty, target_weight = its new
    share of the agent's equity)."""
    notes = {n["slot"]: n["note"] for n in (raw or {}).get("strategy_notes") or []}
    ids = []
    for s in ctx.inp["strategies"]:
        st = strat.in_slot(ctx.conn, ctx.agent_id, s["slot"])
        if st is None:
            continue
        if raw is None:
            reasoning, action, conf = f"FALLBACK to hold: {fallback_error}", "hold", None
        else:
            reasoning = (notes.get(s["slot"]) + " | " if s["slot"] in notes else "") + raw["reasoning"]
            conf = float(raw["confidence"])
            action = "hold"
            if s["slot"] in tilt:
                old, new = tilt[s["slot"]]
                action = "buy" if new > old else "sell"      # more / less capital for the strategy
        ids.append(journal.record(
            ctx.conn, agent_id=ctx.agent_id, version_id=st.version_id, checkpoint=CHECKPOINT[ctx.kind],
            input_sha=ctx.input_sha, input_kind=f"{ctx.kind}_review", reasoning=reasoning, action=action,
            confidence=conf, target_weight=tilt[s["slot"]][1] if s["slot"] in tilt else None, regime=ctx.regime, lessons_used=ctx.lesson_ids,
            week_no=_week(ctx.conn), fallback=raw is None, source_ref=f"review:{ctx.run_id}", decided_at=ctx.as_of))
    return ids


# ==========================================================================
# 6. The whole review
# ==========================================================================

def run_review(conn: sqlite3.Connection, agent_id: str, kind: str, day: str, runner: Runner,
               as_of: str | None = None, dry_run: bool = False, situation: dict | None = None,
               on_risk_actions: Callable | None = None) -> dict:
    """Run one review for one agent. Returns a summary dict."""
    assert kind in KINDS
    from ..logs import bind
    with bind(agent=agent_id, model=_model_of(agent_id), review=kind):
        return _run_review(conn, agent_id, kind, day, runner, as_of, dry_run, situation, on_risk_actions)


def _model_of(agent_id: str) -> str | None:
    from ..config import SETTINGS
    return SETTINGS.models.get(agent_id) or "(cli default)"


def _run_review(conn, agent_id, kind, day, runner, as_of, dry_run, situation, on_risk_actions) -> dict:
    import logging
    log = logging.getLogger("arena.review")
    as_of = as_of or _now()
    inp, lesson_ids = build_input(conn, agent_id, kind, as_of, day, situation)
    prompt = render_prompt(kind, inp)
    with transaction(conn):
        sha = journal.store_input(conn, prompt, as_of)
        run_id = conn.execute("INSERT INTO review_runs(kind, agent_id, as_of, day, status, input_sha256, dry_run, "
                              "started_at) VALUES (?,?,?,?,'running',?,?,?)",
                              (kind, agent_id, as_of, day, sha, int(dry_run), _now())).lastrowid
    log.info("review %s started (review_run %s, %d strategies)", kind, run_id, len(inp["strategies"]))
    res = runner(agent_id, kind, prompt, inp)
    if res.output is not None:
        # Defense in depth: the runner validated already; never apply unvalidated output.
        errs = validate_output(kind, res.output, inp)
        if errs:
            res.output, res.errors, res.error = None, errs, "output failed validation"
    ctx = Ctx(conn, agent_id, kind, run_id, as_of, day, inp, sha, lesson_ids, inp["regime"]["current"], dry_run)
    fallback_error = None
    if res.output is None:
        fallback_error = (res.error or "invalid output") + (": " + "; ".join(res.errors)[:600] if res.errors else "")
    with transaction(conn):
        tilt, changed = {}, set()
        if res.output is not None and kind == "daily" and _evidence() is not None:
            apply_lessons(ctx, res.output)            # observations only
        elif res.output is not None and kind != "risk":
            apply_lessons(ctx, res.output)
            changed = apply_params(ctx, res.output)
            if kind == "daily":
                tilt = apply_tilt(ctx, res.output)
                changed |= apply_emergency(ctx, res.output)
            if kind == "weekly":
                apply_weekly(ctx, res.output)
        if kind == "risk":
            ids = (on_risk_actions(ctx, res.output, fallback_error) if on_risk_actions else [])
        else:
            ids = _record_rows(ctx, res.output, fallback_error, tilt, changed)
        status = "ok" if res.output is not None else "fallback"
        conn.execute("UPDATE review_runs SET status=?, attempts=?, output_json=?, applied_json=?, errors=?, input_tokens=?, "
                     "cached_tokens=?, cache_write_tokens=?, output_tokens=?, finished_at=? WHERE id=?",
                     (status, res.attempts, json.dumps(res.output) if res.output is not None else None,
                      json.dumps({"applied": ctx.applied, "rejected": ctx.rejected, "decisions": ids}),
                      json.dumps(res.errors + ([res.error] if res.error else [])), res.usage.input, res.usage.cached,
                      res.usage.cache_write, res.usage.output, _now(), run_id))
        log_event(conn, "info" if status == "ok" else "error", f"review:{kind}:{agent_id}",
                  f"{day}: {status}; applied {len(ctx.applied)}, rejected {len(ctx.rejected)}"
                  + (f"; {fallback_error[:300]}" if fallback_error else ""))
    log.info("review %s finished: %s, %d applied, %d rejected, %d attempt(s)%s", kind, status, len(ctx.applied),
                len(ctx.rejected), res.attempts, f"; {fallback_error[:200]}" if fallback_error else "")
    if fallback_error and not dry_run:
        alerts.send(f"{kind.capitalize()} review FELL BACK TO HOLD ({agent_id}, {day}): {fallback_error[:400]}",
                    level="warning")
    return {"agent": agent_id, "kind": kind, "day": day, "run_id": run_id, "status": status, "applied": ctx.applied,
            "rejected": ctx.rejected, "decisions": ids, "attempts": res.attempts,
            "error": fallback_error, "output": res.output,
            "tokens": {"input": res.usage.input, "cached": res.usage.cached, "output": res.usage.output}}
