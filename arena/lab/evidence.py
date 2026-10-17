"""Phase 4: evidence rules. Agents test ideas on history first and change
strategies only on enough evidence. Live trading confirms; it doesn't discover.

On only with EVIDENCE_RULES=1 (and LEARNING_ENABLED=1); every threshold is
in config/lab.yaml `evidence:` (human-edited). With it off, the learning
layer works exactly as before.

    freeze          a champion can't change (params or rules) before it has
                    min_live_trades live trades AND min_weeks weeks as champion.
                    The hourly risk check may always reduce risk.
    run_backtest    the agents' tool, in the WEEKLY review only: design or
                    walk-forward runs of their own code, never the holdout,
                    at most backtest_budget runs per agent per week. Every
                    run is logged (runs.py) with its attempt number.
    lessons         start as hypotheses; confirmed only with a cited walk-
                    forward run (>= min_backtest_trades out-of-sample trades)
                    AND cited graded live trades (>= min_live_trades); not
                    confirmed within hypothesis_max_weeks: retired. Only
                    confirmed lessons reach prompts as rules.
    challengers     a rewrite must come as code. The engine (not the agent)
                    then runs, in order, logging every step in promotion_log:
                      1. walk-forward: >= min_oos_trades out-of-sample trades
                         and a better risk-adjusted score than the champion
                      2. per market state: not worse than the champion by more
                         than regime_margin_pct (annual return)
                      3. the locked holdout, once per idea: score >= champion's;
                         a failed idea is retired for good
                      4. shadow trading (no capital) on new data, evaluated
                         after every day's data (so after every closed trade),
                         until PROVEN: >= shadow min_trades closed trades, mean
                         weekly score >= champion's + score_margin, and a paired
                         weekly bootstrap P(better) >= bootstrap_prob. Then it
                         is promoted at once. Not proven in max_weeks: retired.
                         A new rewrite of the slot replaces the challenger, so
                         its record starts again from zero.
    champion baseline  the champion's own code when it has some; a manual
                    (rules-in-words) champion can't be backtested, so its
                    stand-in for steps 1-3 is SPY buy-and-hold, and in the
                    shadow stage its real live record.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..prices import ET
from ..learning import lessons as llessons, strategies as strat
from ..learning.scores import max_drawdown_pct, score as league_score
from . import config as labconfig, engine, histdata, runs, schema, walkforward
from .strategies import spy_buy_and_hold

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")


def enabled() -> bool:
    return labconfig.evidence_enabled()


def cfg():
    return labconfig.get().evidence


def ensure(conn: sqlite3.Connection) -> None:
    schema.migrate(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


def log(conn, agent_id: str, stage: str, passed: bool, reason: str, *, slot=None, version_id=None, idea_key=None,
        detail: dict | None = None, run_ids: list | None = None, at: str | None = None) -> None:
    conn.execute("INSERT INTO promotion_log(ts, agent_id, slot, version_id, idea_key, stage, passed, reason, detail, "
                 "run_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (at or _now(), agent_id, slot, version_id, idea_key, stage, int(passed), reason[:1000],
                  json.dumps(detail or {}, default=str), json.dumps(run_ids or [])))


# ==========================================================================
# Freeze
# ==========================================================================

def champion_since(conn: sqlite3.Connection, s: strat.Strategy) -> str:
    """When the current champion version started trading: its creation, or
    its promotion from challenger."""
    ensure(conn)
    row = conn.execute("SELECT MAX(ts) FROM promotion_log WHERE version_id=? AND stage='promoted'",
                       (s.version_id,)).fetchone()
    v = strat.version_row(conn, s.version_id)
    return max(x for x in (row[0], v["created_at"]) if x)


def freeze_status(conn: sqlite3.Connection, s: strat.Strategy, now: str) -> tuple[bool, str, dict]:
    """(frozen, why, numbers). Frozen until BOTH min_live_trades and min_weeks."""
    c = cfg()
    since = champion_since(conn, s)
    trades = conn.execute("SELECT COUNT(*) FROM trades WHERE agent_id=? AND slot=? AND date>=?",
                          (s.agent_id, s.slot, _day(since))).fetchone()[0]
    weeks = (datetime.fromisoformat(now) - datetime.fromisoformat(since)).days / 7
    detail = {"champion_since": since, "live_trades": trades, "weeks": round(weeks, 2),
              "needs_trades": c.freeze_min_live_trades, "needs_weeks": c.freeze_min_weeks}
    missing = []
    if trades < c.freeze_min_live_trades:
        missing.append(f"{trades}/{c.freeze_min_live_trades} live trades")
    if weeks < c.freeze_min_weeks:
        missing.append(f"{weeks:.1f}/{c.freeze_min_weeks} weeks")
    if missing:
        return True, f"champion v{s.version} is frozen since {_day(since)}: " + ", ".join(missing), detail
    return False, f"champion v{s.version}: {trades} live trades over {weeks:.1f} weeks", detail


# ==========================================================================
# The agents' backtest tool (weekly review only)
# ==========================================================================

BACKTEST_SPEC = {
    "name": "run_backtest",
    "description": (
        "Backtest a strategy idea on daily history since 2016 (weekly review only). Your code defines "
        "target_weights(prices, date) -> {ticker: weight}, exactly like a code slot: `prices` holds bars up to "
        "`date` only; orders fill at the next open with costs; the league's hard limits and a stop on every "
        "position apply. purpose 'design' = any period before the locked holdout; 'walk_forward' = the "
        "out-of-sample record (design 24 months -> test the next 6, rolling). The most recent 12 months are a "
        "locked holdout you can't use. Every run is logged and counted per idea_key (reuse the key for the "
        "same idea); you have a weekly budget. Survivorship bias: the universe is today's S&P 500, so results "
        "are optimistic."),
    "parameters": {"type": "object", "properties": {
        "idea_key": {"type": "string", "description": "stable id for the idea, lowercase-with-dashes"},
        "code": {"type": "string", "description": "Python defining target_weights(prices, date)"},
        "params": {"type": "object", "properties": {"stop_loss_pct": {"type": "number"},
                                                    "position_max_pct": {"type": "number"}}},
        "purpose": {"type": "string", "enum": ["design", "walk_forward"]},
        "start": {"type": "string", "description": "design only: first day YYYY-MM-DD"},
        "end": {"type": "string", "description": "design only: last day, before the holdout"}},
        "required": ["idea_key", "code", "purpose"]},
}


def _week_bounds(now: str) -> tuple[str, str]:
    d = datetime.fromisoformat(now).astimezone(ET)
    start = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=7)).astimezone(timezone.utc).isoformat()


def budget_used(conn: sqlite3.Connection, agent_id: str, now: str) -> int:
    ensure(conn)
    a, b = _week_bounds(now)
    return conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE requested_by=? AND created_at>=? AND created_at<?",
                        (f"agent:{agent_id}", a, b)).fetchone()[0]


def budget_left(conn: sqlite3.Connection, agent_id: str, now: str) -> int:
    return max(cfg().backtest_budget - budget_used(conn, agent_id, now), 0)


_SUMMARY = ("start", "end", "total_return_pct", "annual_return_pct", "max_drawdown_pct", "sharpe",
            "risk_adjusted_score", "period_score", "trades", "win_rate_pct", "spy_return_pct", "vs_spy_pct",
            "by_state", "attempts", "run_id", "holdout_from", "data_note")


def summary(res: dict) -> dict:
    out = {k: res[k] for k in _SUMMARY if k in res}
    if "windows" in res:
        out["windows"] = [{"test": w["test"], **w["result"]} for w in res["windows"]]
    if res.get("notes"):
        out["engine_notes"] = res["notes"][:5]
    return out


def agent_backtest(conn: sqlite3.Connection, hconn: sqlite3.Connection, agent_id: str, args: dict,
                   now: str | None = None) -> dict:
    """The run_backtest tool. Never raises: refusals come back as {"ok": False}."""
    now = now or _now()
    ensure(conn)
    key, code, purpose = args.get("idea_key"), args.get("code"), args.get("purpose")
    if not isinstance(key, str) or not KEY.match(key):
        return {"ok": False, "refused": "idea_key must look like 'momentum-top4' (lowercase, dashes)"}
    if not isinstance(code, str) or "target_weights" not in code or len(code) > 20000:
        return {"ok": False, "refused": "code must define target_weights(prices, date) (max 20,000 characters)"}
    if purpose not in ("design", "walk_forward"):
        return {"ok": False, "refused": "purpose must be design or walk_forward (the holdout is locked)"}
    params = args.get("params") or {}
    errs = strat.check_params(params) if isinstance(params, dict) else ["params must be an object"]
    if errs:
        return {"ok": False, "refused": "; ".join(errs)}
    left = budget_left(conn, agent_id, now)
    if left <= 0:
        return {"ok": False, "refused": f"weekly backtest budget used up ({cfg().backtest_budget} runs); "
                                        "it resets at the start of next week"}
    retired = conn.execute("SELECT reason FROM retired_ideas WHERE idea_key=?", (key,)).fetchone()
    if retired:
        return {"ok": False, "refused": f"idea {key!r} was retired: {retired[0]}"}
    spec = engine.StrategySpec(key, code=code, params=params)
    start = args.get("start") if purpose == "design" else None
    end = args.get("end") if purpose == "design" else None
    try:
        res = runs.execute(conn, hconn, spec=spec, purpose=purpose, idea_key=key, requested_by=f"agent:{agent_id}",
                           start=start, end=end)
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)[:500]}
    out = summary(res) if res.get("ok") else {k: res[k] for k in ("refused", "error", "run_id", "attempts") if k in res}
    out["ok"] = bool(res.get("ok"))
    out["budget_left_this_week"] = budget_left(conn, agent_id, now)
    return out


# ==========================================================================
# Lessons
# ==========================================================================

def _states_of(conn, ids: list[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in conn.execute(f"SELECT decided_at, checkpoint FROM decisions WHERE id IN ({','.join('?' * len(ids))})",
                          ids):
        day = d[0][:10]
        op = "<" if d[1] == "risk_exit" else "<="
        row = conn.execute(f"SELECT label FROM market_states WHERE day {op} ? ORDER BY day DESC LIMIT 1",
                           (day,)).fetchone()
        lab = row[0] if row and row[0] else "unlabelled"
        out[lab] = out.get(lab, 0) + 1
    return out


def record_lesson_evidence(conn: sqlite3.Connection, agent_id: str, lesson_id: int, live_ids: list[int],
                           run_ids: list[int], at: str) -> list[str]:
    """Summarize what a lesson cites; returns notes on refused citations."""
    ensure(conn)
    notes = []
    trade_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM decisions WHERE id IN ({','.join('?' * len(live_ids))}) AND action != 'hold' AND agent_id=?",
        (*live_ids, agent_id))] if live_ids else []
    if trade_ids:
        lo, hi = conn.execute(f"SELECT MIN(decided_at), MAX(decided_at) FROM decisions WHERE id IN "
                              f"({','.join('?' * len(trade_ids))})", trade_ids).fetchone()
        conn.execute("INSERT INTO lesson_evidence(lesson_id, source, ref, trades, period_start, period_end, regime, "
                     "purpose, added_at) VALUES (?,?,?,?,?,?,?,NULL,?)",
                     (lesson_id, "live", json.dumps(sorted(trade_ids)), len(trade_ids), lo[:10], hi[:10],
                      json.dumps(_states_of(conn, trade_ids)), at))
    for rid in run_ids:
        r = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (rid,)).fetchone()
        if r is None or r["status"] != "ok" or r["requested_by"] not in (f"agent:{agent_id}", "engine:promotion") \
                or r["purpose"] not in ("design", "walk_forward"):
            notes.append(f"backtest run {rid}: not one of your finished design/walk-forward runs")
            continue
        res = json.loads(r["results"])
        states = {k: v["trades"] for k, v in res.get("by_state", {}).items() if v.get("trades")}
        conn.execute("INSERT INTO lesson_evidence(lesson_id, source, ref, trades, period_start, period_end, regime, "
                     "purpose, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (lesson_id, "backtest", str(rid), int(res.get("trades") or 0), res.get("start"), res.get("end"),
                      json.dumps(states), r["purpose"], at))
    return notes


def lesson_evidence(conn: sqlite3.Connection, lesson_id: int) -> dict:
    """What the prompt shows next to a lesson."""
    rows = conn.execute("SELECT * FROM lesson_evidence WHERE lesson_id=? ORDER BY id", (lesson_id,)).fetchall()
    live_ids: set[int] = set()
    for r in rows:
        if r["source"] == "live":
            live_ids |= set(json.loads(r["ref"]))
    live = [r for r in rows if r["source"] == "live"]
    bt = [r for r in rows if r["source"] == "backtest"]
    best = max((r for r in bt if r["purpose"] == "walk_forward"), key=lambda r: r["trades"], default=None)
    return {
        "live": {"trades": len(live_ids), "period": [min((r["period_start"] for r in live), default=None),
                                                     max((r["period_end"] for r in live), default=None)],
                 "states": _merge([json.loads(r["regime"] or "{}") for r in live])} if live else None,
        "backtest": {"runs": [int(r["ref"]) for r in bt],
                     "best_walk_forward": None if best is None else {
                         "run_id": int(best["ref"]), "trades": best["trades"],
                         "period": [best["period_start"], best["period_end"]],
                         "states": json.loads(best["regime"] or "{}")}} if bt else None,
    }


def _merge(ds: list[dict]) -> dict:
    out: dict = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def confirmed(conn: sqlite3.Connection) -> set[int]:
    ensure(conn)
    return {r[0] for r in conn.execute("SELECT lesson_id FROM lesson_confirmations")}


def promote_lessons(conn: sqlite3.Connection, agent_id: str, at: str | None = None) -> dict:
    """Hypothesis -> confirmed with enough backtest AND live evidence; stale
    hypotheses retired. Replaces lessons.promote_ready under the rules."""
    ensure(conn)
    at = at or _now()
    c = cfg()
    done, retired = [], []
    have = confirmed(conn)
    rows = conn.execute("SELECT * FROM lessons WHERE agent_id=? AND status IN ('candidate', 'active')",
                        (agent_id,)).fetchall()
    for l in rows:
        if l["id"] in have:
            continue
        ev = lesson_evidence(conn, l["id"])
        live_n = (ev["live"] or {}).get("trades", 0)
        bt = (ev["backtest"] or {}).get("best_walk_forward")
        bt_n = bt["trades"] if bt else 0
        if live_n >= c.lesson_min_live_trades and bt_n >= c.lesson_min_backtest_trades:
            conn.execute("INSERT INTO lesson_confirmations(lesson_id, confirmed_at, detail) VALUES (?,?,?)",
                         (l["id"], at, json.dumps(ev)))
            conn.execute("UPDATE lessons SET status='active', updated_at=? WHERE id=?", (at, l["id"]))
            done.append(l["id"])
        elif datetime.fromisoformat(at) - datetime.fromisoformat(l["created_at"]) > timedelta(weeks=c.hypothesis_max_weeks):
            llessons.retire(conn, agent_id, l["id"], f"hypothesis not confirmed within {c.hypothesis_max_weeks} weeks "
                            f"(live trades {live_n}/{c.lesson_min_live_trades}, walk-forward trades "
                            f"{bt_n}/{c.lesson_min_backtest_trades})", at=at)
            retired.append(l["id"])
    return {"confirmed": done, "retired": retired}


# ==========================================================================
# Challengers: the gates
# ==========================================================================

def version_code(conn: sqlite3.Connection, version_id: int) -> dict | None:
    ensure(conn)
    r = conn.execute("SELECT * FROM strategy_version_code WHERE version_id=?", (version_id,)).fetchone()
    return None if r is None else dict(r)


def _baseline(conn, s: strat.Strategy) -> tuple[engine.StrategySpec, engine.Options | None, str]:
    """What the challenger is measured against in the backtest gates."""
    vc = version_code(conn, s.version_id)
    if vc:
        return engine.StrategySpec(f"champion-v{s.version}", code=vc["code"], params=s.params), None, "champion code"
    return (engine.StrategySpec("spy_buy_and_hold", fn=spy_buy_and_hold), engine.Options(benchmark=True),
            "SPY buy-and-hold (the champion is rules in words, which can't be backtested)")


def _state_gaps(ch: dict, base: dict, margin: float, min_days: int) -> list[str]:
    out = []
    for st, b in base.get("by_state", {}).items():
        c = ch.get("by_state", {}).get(st)
        if st == "unlabelled" or c is None or min(c["days"], b["days"]) < min_days:
            continue
        if c["annual_return_pct"] is not None and b["annual_return_pct"] is not None \
                and c["annual_return_pct"] < b["annual_return_pct"] - margin:
            out.append(f"{st}: {c['annual_return_pct']:+.1f}%/yr vs champion {b['annual_return_pct']:+.1f}%/yr")
    return out


def retire_idea(conn, agent_id: str, idea_key: str, sha: str | None, reason: str, **kw) -> None:
    conn.execute("INSERT OR REPLACE INTO retired_ideas(idea_key, code_sha256, reason, ts) VALUES (?,?,?,?)",
                 (idea_key, sha, reason[:500], kw.get("at") or _now()))
    log(conn, agent_id, "retired", False, reason, idea_key=idea_key, **kw)


def propose(conn: sqlite3.Connection, hconn: sqlite3.Connection, agent_id: str, s: strat.Strategy, rw: dict,
            as_of: str, week_no: int | None) -> tuple[bool, str, int | None]:
    """A weekly-review rewrite under the rules: freeze, then gates 1-3, then
    the shadow stage starts. Returns (accepted, message, version id)."""
    ensure(conn)
    c = cfg()
    code = rw.get("code")
    sha = hashlib.sha256((code or "").encode()).hexdigest()
    key = rw.get("idea_key") or f"{agent_id}-s{s.slot}-{sha[:10]}"
    kw = {"slot": s.slot, "at": as_of}
    log(conn, agent_id, "proposed", True, (rw.get("hypothesis") or "")[:500], idea_key=key,
        detail={"rules": rw.get("rules", "")[:1000], "params": rw.get("params")}, **kw)
    if not isinstance(code, str) or "target_weights" not in code:
        log(conn, agent_id, "proposed", False, "a challenger must come as code (target_weights) so it can be "
            "backtested", idea_key=key, **kw)
        return False, "a challenger must come as code defining target_weights(prices, date)", None
    if not KEY.match(key):
        return False, "idea_key must look like 'momentum-top4'", None
    gone = conn.execute("SELECT reason FROM retired_ideas WHERE idea_key=? OR code_sha256=?", (key, sha)).fetchone()
    if gone:
        log(conn, agent_id, "proposed", False, f"idea retired earlier: {gone[0]}", idea_key=key, **kw)
        return False, f"this idea was retired: {gone[0]}", None
    frozen, why, fd = freeze_status(conn, s, as_of)
    log(conn, agent_id, "freeze", not frozen, why, idea_key=key, detail=fd, **kw)
    if frozen:
        return False, why, None
    params = {**s.params, **(rw.get("params") or {})}
    spec = engine.StrategySpec(key, code=code, params=params)
    base_spec, base_opt, base_what = _baseline(conn, s)

    def fail(stage, reason, detail, run_ids, retire=False):
        log(conn, agent_id, stage, False, reason, idea_key=key, detail=detail, run_ids=run_ids, **kw)
        vid = strat.add_version(conn, s.strategy_id, rw["rules"], params, "weekly_rewrite", f"rejected at {stage}: "
                                f"{reason}", "dropped", week_no, hypothesis=rw.get("hypothesis"), at=as_of)
        _store_code(conn, vid, key, code, as_of)
        if retire:
            retire_idea(conn, agent_id, key, sha, f"{stage}: {reason}", slot=s.slot, version_id=vid, at=as_of)
        return False, f"{stage}: {reason}", vid

    # 1. Walk-forward, out of sample.
    wf = runs.execute(conn, hconn, spec=spec, purpose="walk_forward", idea_key=key, requested_by="engine:promotion")
    bwf = runs.execute(conn, hconn, spec=base_spec, purpose="check", walk=True, idea_key=f"baseline:{key}",
                       requested_by="engine:promotion", options=base_opt)
    ids = [wf.get("run_id"), bwf.get("run_id")]
    if not wf.get("ok") or not bwf.get("ok"):
        return fail("walk_forward", f"backtest failed: {wf.get('error') or wf.get('refused') or bwf.get('error')}",
                    {}, ids)
    d1 = {"trades": wf["trades"], "score": wf["risk_adjusted_score"], "baseline": base_what,
          "baseline_score": bwf["risk_adjusted_score"], "attempts": wf["attempts"]}
    if wf["trades"] < c.min_oos_trades:
        return fail("walk_forward", f"{wf['trades']} out-of-sample trades; needs {c.min_oos_trades}", d1, ids)
    # Both scores: the mean WEEKLY score alone can be positive for a strategy
    # that loses most of its money (a good week scores +3, a terrible one ~-1),
    # so the whole-period score (total return / max drawdown) must win too.
    d1.update(period_score=wf["period_score"], baseline_period_score=bwf["period_score"])
    if not wf["risk_adjusted_score"] > bwf["risk_adjusted_score"]:
        return fail("walk_forward", f"score {wf['risk_adjusted_score']:.3f} is not above the {base_what} score "
                    f"{bwf['risk_adjusted_score']:.3f}", d1, ids)
    if not wf["period_score"] > bwf["period_score"]:
        return fail("walk_forward", f"whole-period score (return / max drawdown) {wf['period_score']:.3f} is not "
                    f"above the {base_what}'s {bwf['period_score']:.3f}", d1, ids)
    log(conn, agent_id, "walk_forward", True, f"{wf['trades']} OOS trades, score {wf['risk_adjusted_score']:.3f} > "
        f"{bwf['risk_adjusted_score']:.3f} ({base_what}); attempt {wf['attempts']} of this idea", idea_key=key,
        detail=d1, run_ids=ids, **kw)
    # 2. Per market state.
    gaps = _state_gaps(wf, bwf, c.regime_margin_pct, c.min_state_days)
    if gaps:
        return fail("regimes", "worse than the champion by more than "
                    f"{c.regime_margin_pct:g}%/yr in: " + "; ".join(gaps), {"gaps": gaps}, ids)
    log(conn, agent_id, "regimes", True, "no market state worse than the champion by more than "
        f"{c.regime_margin_pct:g}%/yr", idea_key=key, run_ids=ids, **kw)
    # 3. The locked holdout, once.
    ho = runs.execute(conn, hconn, spec=spec, purpose="holdout", idea_key=key, requested_by="engine:promotion")
    if not ho.get("ok"):
        return fail("holdout", ho.get("refused") or ho.get("error") or "holdout run failed", {}, [ho.get("run_id")],
                    retire=bool(ho.get("refused")))
    bho = runs.execute(conn, hconn, spec=base_spec, purpose="check", idea_key=f"baseline:{key}",
                       requested_by="engine:promotion", start=ho["holdout_from"], options=base_opt)
    ids = [ho["run_id"], bho.get("run_id")]
    if not bho.get("ok"):
        return fail("holdout", f"baseline holdout run failed: {bho.get('error')}", {}, ids)
    d3 = {"score": ho["risk_adjusted_score"], "baseline_score": bho["risk_adjusted_score"], "trades": ho["trades"],
          "return_pct": ho["total_return_pct"], "baseline_return_pct": bho["total_return_pct"]}
    d3.update(period_score=ho["period_score"], baseline_period_score=bho["period_score"])
    if ho["risk_adjusted_score"] < bho["risk_adjusted_score"] - c.holdout_margin \
            or ho["period_score"] < bho["period_score"] - c.holdout_margin:
        return fail("holdout", f"holdout score {ho['risk_adjusted_score']:.3f} (whole period {ho['period_score']:.3f}) "
                    f"vs {base_what} {bho['risk_adjusted_score']:.3f} ({bho['period_score']:.3f}); the idea is "
                    f"retired", d3, ids, retire=True)
    log(conn, agent_id, "holdout", True, f"holdout score {ho['risk_adjusted_score']:.3f} >= "
        f"{bho['risk_adjusted_score']:.3f}", idea_key=key, detail=d3, run_ids=ids, **kw)
    # 4. Shadow stage starts (a new challenger replaces any old one: its record restarts at zero).
    vid = strat.add_version(conn, s.strategy_id, rw["rules"], params, "weekly_rewrite",
                            f"passed walk-forward, regimes and holdout ({key})", "challenger", week_no,
                            hypothesis=rw.get("hypothesis"), at=as_of)
    _store_code(conn, vid, key, code, as_of)
    conn.execute("UPDATE lab_shadows SET status='retired', finished_at=? WHERE agent_id=? AND slot=? AND status='running'",
                 (as_of, agent_id, s.slot))
    start = _first_shadow_day(hconn, _day(as_of))
    conn.execute("INSERT INTO lab_shadows(version_id, agent_id, slot, champion_version_id, idea_key, start_day, status, "
                 "started_at) VALUES (?,?,?,?,?,?,'running',?)", (vid, agent_id, s.slot, s.version_id, key, start, as_of))
    log(conn, agent_id, "shadow_started", True, f"shadow trading from {start} until proven (max "
        f"{c.shadow_max_weeks} weeks)", version_id=vid, idea_key=key, **kw)
    return True, f"challenger v{strat.version_row(conn, vid)['version']} passed the backtest gates; shadow trading " \
                 f"from {start}", vid


def _store_code(conn, vid: int, key: str, code: str, at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO strategy_version_code(version_id, idea_key, code, code_sha256, created_at) "
                 "VALUES (?,?,?,?,?)", (vid, key, code, hashlib.sha256(code.encode()).hexdigest(), at))


def _first_shadow_day(hconn, day: str) -> str:
    """The shadow record counts from the first decision AFTER the proposal."""
    nxt = hconn.execute("SELECT MIN(day) FROM hist_calendar WHERE day > ?", (day,)).fetchone()[0]
    return nxt or day


# ==========================================================================
# Shadow stage
# ==========================================================================

def weekly_scores(eq: pd.Series, floor: float | None = None) -> dict[str, float]:
    """{ISO week: league score} of a daily equity series; each week starts
    from the previous week's last value."""
    from ..learning import config as lconfig
    floor = lconfig.get().scoring.drawdown_floor_pct if floor is None else floor
    out = {}
    prev = None
    for wk, part in eq.groupby([f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" for d in eq.index]):
        vals = list(part.values)
        path = ([prev] if prev is not None else []) + vals
        if len(path) >= 2:
            r = (path[-1] / path[0] - 1) * 100
            out[wk] = league_score(r, max_drawdown_pct(path), False, floor)
        prev = vals[-1]
    return out


def bootstrap_prob(a: list[float], b: list[float], samples: int, seed: int = 7) -> float:
    """P(mean(a) > mean(b)) by resampling paired weeks with replacement."""
    if not a:
        return 0.0
    d = np.array(a) - np.array(b)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(samples, len(d)))
    return float((d[idx].mean(axis=1) > 0).mean())


def _champion_series(conn, hconn, row, frame, start, end) -> tuple[pd.Series | None, str]:
    """The champion over the shadow window: its live slot equity, or (no
    live record, e.g. a dry run on history) its code / SPY through the engine."""
    snaps = conn.execute("SELECT date, slots_json FROM snapshots WHERE agent_id=? AND kind='daily' AND date>=? "
                         "AND date<=? ORDER BY seq", (row["agent_id"], start, end)).fetchall()
    pts = {}
    for d, js in snaps:
        v = json.loads(js).get(str(row["slot"]))
        if v:
            pts[d] = v
    # The live record counts only if it covers the whole window (it starts
    # within a week of the shadow start).
    if len(pts) >= 5 and (pd.Timestamp(min(pts)) - pd.Timestamp(start)).days <= 7:
        return pd.Series(pts, index=pd.to_datetime(list(pts))).sort_index(), "live slot equity"
    s = strat.by_id(conn, conn.execute("SELECT strategy_id FROM strategy_versions WHERE id=?",
                                       (row["champion_version_id"],)).fetchone()[0])
    spec, opt, what = _baseline(conn, s)
    try:
        r = engine.run(spec, frame, start, end, {}, opt)
    except ValueError:
        return None, what
    return r.equity, what


def evaluate_shadows(conn: sqlite3.Connection, hconn: sqlite3.Connection, agent_id: str | None = None,
                     through: str | None = None, at: str | None = None) -> list[dict]:
    """Update every running shadow on the data so far; promote the proven,
    retire the timed-out. Safe to run every night."""
    ensure(conn)
    c = cfg()
    at = at or _now()
    through = through or walkforward.data_end(hconn)
    rows = conn.execute("SELECT * FROM lab_shadows WHERE status='running'" + (" AND agent_id=?" if agent_id else ""),
                        (agent_id,) if agent_id else ()).fetchall()
    out = []
    frame = None
    for row in rows:
        role = conn.execute("SELECT role FROM strategy_versions WHERE id=?", (row["version_id"],)).fetchone()[0]
        if role != "challenger":
            conn.execute("UPDATE lab_shadows SET status='retired', finished_at=? WHERE version_id=?", (at, row["version_id"]))
            log(conn, row["agent_id"], "retired", False, "the challenger was replaced or its strategy changed; its "
                "shadow record ends here", slot=row["slot"], version_id=row["version_id"], idea_key=row["idea_key"], at=at)
            out.append({"version_id": row["version_id"], "status": "retired", "why": "replaced"})
            continue
        if frame is None:
            frame = histdata.bars(hconn, end=through)
        vc = version_code(conn, row["version_id"])
        v = strat.version_row(conn, row["version_id"])
        spec = engine.StrategySpec(row["idea_key"], code=vc["code"], params=json.loads(v["params"]))
        weeks = (pd.Timestamp(through) - pd.Timestamp(row["start_day"])).days / 7
        res: dict = {"weeks": round(weeks, 2)}
        try:
            r = engine.run(spec, frame, row["start_day"], through, _labels(conn, through))
        except ValueError:
            r = None                                  # no rebalance yet
        if r is not None:
            closed = [t for t in r.trades if t["exit_reason"] != "end"]
            ch_w = weekly_scores(r.equity)
            champ, what = _champion_series(conn, hconn, row, frame, row["start_day"], through)
            cw = weekly_scores(champ) if champ is not None else {}
            common = sorted(k for k in set(ch_w) & set(cw) if np.isfinite(ch_w[k]) and np.isfinite(cw[k]))
            a, b = [ch_w[k] for k in common], [cw[k] for k in common]
            res.update({"trades": len(closed), "weeks_compared": len(common), "champion": what,
                        "score": round(float(np.mean(a)), 4) if a else None,
                        "champion_score": round(float(np.mean(b)), 4) if b else None,
                        "prob_better": round(bootstrap_prob(a, b, c.shadow_bootstrap_samples), 4) if a else None,
                        "return_pct": r.metrics["total_return_pct"]})
        proven = (res.get("trades", 0) >= c.shadow_min_trades and res.get("score") is not None
                  and res.get("champion_score") is not None
                  and res["score"] >= res["champion_score"] + c.shadow_score_margin
                  and res["prob_better"] >= c.shadow_bootstrap_prob)
        status = "running"
        if proven:
            status = "promoted"
            promote(conn, row, res, at)
        elif weeks >= c.shadow_max_weeks:
            status = "retired"
            strat.drop(conn, row["version_id"])
            retire_idea(conn, row["agent_id"], row["idea_key"], vc["code_sha256"],
                        f"not proven in {c.shadow_max_weeks} weeks of shadow trading: {json.dumps(res)}",
                        slot=row["slot"], version_id=row["version_id"], at=at)
        conn.execute("UPDATE lab_shadows SET status=?, last_eval_day=?, trades=?, score=?, champion_score=?, "
                     "prob_better=?, detail=?, finished_at=? WHERE version_id=?",
                     (status, through, res.get("trades", 0), res.get("score"), res.get("champion_score"),
                      res.get("prob_better"), json.dumps(res), None if status == "running" else at, row["version_id"]))
        if status == "running":
            log(conn, row["agent_id"], "shadow", False, f"not proven yet: {res.get('trades', 0)}/{c.shadow_min_trades} "
                f"trades, score {res.get('score')} vs {res.get('champion_score')}, P(better) {res.get('prob_better')}",
                slot=row["slot"], version_id=row["version_id"], idea_key=row["idea_key"], detail=res, at=at)
        out.append({"version_id": row["version_id"], "status": status, **res})
    return out


def _labels(conn, end: str) -> dict:
    from .states import labels
    return labels(conn, end=end)


def promote(conn: sqlite3.Connection, row, res: dict, at: str) -> None:
    """The proven challenger becomes the champion; its code is saved for the
    slot, which from now on is a code slot."""
    vc = version_code(conn, row["version_id"])
    strat.promote(conn, row["version_id"])
    conn.execute("INSERT INTO strategy_code(agent_id, slot, saved_date, code, test_weights) VALUES (?,?,?,?,?)",
                 (row["agent_id"], row["slot"], _day(at), vc["code"], "{}"))
    log(conn, row["agent_id"], "promoted", True, f"proven in shadow: {res.get('trades')} trades, score "
        f"{res.get('score')} vs {res.get('champion_score')}, P(better) {res.get('prob_better')}",
        slot=row["slot"], version_id=row["version_id"], idea_key=row["idea_key"], detail=res, at=at)


def status(conn: sqlite3.Connection, agent_id: str) -> dict:
    """For prompts and the dashboard: running shadows and the latest steps."""
    ensure(conn)
    sh = [dict(r) for r in conn.execute("SELECT version_id, slot, idea_key, start_day, status, trades, score, "
                                        "champion_score, prob_better FROM lab_shadows WHERE agent_id=? "
                                        "ORDER BY started_at DESC LIMIT 5", (agent_id,))]
    steps = [dict(r) for r in conn.execute("SELECT ts, slot, idea_key, stage, passed, reason FROM promotion_log "
                                           "WHERE agent_id=? ORDER BY id DESC LIMIT 10", (agent_id,))]
    return {"shadows": sh, "recent_steps": steps}
