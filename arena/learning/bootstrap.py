"""Bring a running league into the learning layer, once (idempotent).

  1. strategies: every slot the agents have used gets a strategy and a v1
     version (change_kind 'initial': it starts no cooldown), rebuilt week by
     week from the accepted decisions, so a slot that changed its name in
     week 2 has two strategies (the first retired)
  2. decisions: every accepted weekly decision so far is journaled, with
     exactly the prompt the agent got (from turn_messages), and its fills are
     linked to it
  3. market context: the regime for the last 60 trading days (so the
     confirm_days rule works on day one), plus the VIX history
  4. weekly_scores for weeks already scored (mode raw_return: informational)
  5. the grader runs once

Nothing existing is changed: only new tables are written (and the new
trades.decision_id column filled in).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..db import log_event, transaction
from ..ledger import SlotTarget
from . import collector, grader, journal, scores, strategies as strat

STATE_KEY = "learning_bootstrapped"


def done(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM league_state WHERE key=?", (STATE_KEY,)).fetchone() is not None


def _prompt_of(conn, turn_id: int) -> str:
    rows = conn.execute("SELECT seq, role, content FROM turn_messages WHERE turn_id=? AND role IN ('system','user') "
                        "ORDER BY seq LIMIT 3", (turn_id,)).fetchall()
    parts = []
    for r in rows:
        c = r["content"]
        try:
            c = json.loads(c) if c.startswith('"') else c
        except (json.JSONDecodeError, AttributeError):
            pass
        parts.append(c if isinstance(c, str) else json.dumps(c))
    return "\n\n".join(parts) or f"(prompt of turn {turn_id} not stored)"


def backfill_decisions(conn: sqlite3.Connection) -> int:
    weeks = conn.execute("SELECT t.id AS turn_id, t.week_no, t.agent_id, t.decision_json, t.finished_at, "
                         "w.decision_date, w.exec_date, w.status FROM agent_turns t JOIN weeks w ON w.week_no=t.week_no "
                         "WHERE t.status='accepted' AND w.simulate=0 ORDER BY t.week_no, t.agent_id").fetchall()
    prev: dict[str, dict[int, SlotTarget]] = {}
    n = 0
    for w in weeks:
        a = w["agent_id"]
        payload = conn.execute("SELECT payload FROM pending_orders WHERE agent_id=? AND kind='rebalance' AND created_date=? "
                               "ORDER BY id DESC LIMIT 1", (a, w["decision_date"])).fetchone()
        if payload is None:
            continue
        from ..agents.decision import targets_from_json
        targets = {t.slot: t for t in targets_from_json(payload[0])}
        raw = json.loads(w["decision_json"] or "{}")
        conf = {s.get("slot"): s.get("confidence") for s in raw.get("slots", []) if isinstance(s, dict)}
        at = w["finished_at"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        already = conn.execute("SELECT 1 FROM decisions WHERE agent_id=? AND week_no=? AND checkpoint='weekly_rebalance'",
                               (a, w["week_no"])).fetchone()
        sha = journal.store_input(conn, _prompt_of(conn, w["turn_id"]), at)
        before = prev.get(a, {})

        def rec(vid, action, why, symbol=None, weight=None, slot=None):
            nonlocal n
            if already:
                return
            c = conf.get(slot)
            journal.record(conn, agent_id=a, version_id=vid, checkpoint="weekly_rebalance", input_sha=sha,
                           input_kind="weekly_decision", reasoning=why or "(no reason given)", action=action,
                           confidence=c if isinstance(c, (int, float)) and 0 <= c <= 1 else None, symbol=symbol,
                           target_weight=weight, regime=journal.regime_as_of(conn, at), week_no=w["week_no"],
                           source_ref=f"week:{w['week_no']}", decided_at=at)
            n += 1

        for s in strat.active(conn, a):
            t = targets.get(s.slot)
            if t is not None and s.name.strip().lower() == (t.name or "").strip().lower():
                continue
            old = before.get(s.slot)
            for ticker, wgt in sorted((old.weights if old else {}).items()):
                if wgt:
                    rec(s.version_id, "sell" if wgt > 0 else "cover", f"strategy \"{s.name}\" retired", ticker, 0.0, s.slot)
            strat.retire(conn, s.strategy_id, w["week_no"], at)
        for slot, t in sorted(targets.items()):
            s = strat.in_slot(conn, a, slot)
            if s is None:
                s = strat.create(conn, a, slot, t.name or f"slot {slot}", t.reason or t.name or f"slot {slot}", None,
                                 "initial", "existing slot, migrated into the learning layer", w["week_no"], at)
            old = before.get(slot)
            k = 0
            for ticker in sorted(set(t.weights) | set(old.weights if old else {})):
                o, nw = (old.weights.get(ticker, 0.0) if old else 0.0), t.weights.get(ticker, 0.0)
                action = journal.action_for(o, nw) or ("hold" if nw else None)
                if action:
                    k += 1
                    rec(s.version_id, action, t.notes.get(ticker) or t.reason, ticker, round(nw, 6), slot)
            if not k:
                rec(s.version_id, "hold", t.reason or "all cash", slot=slot)
        prev[a] = targets
        if w["status"] in ("executed", "scored"):
            journal.link_trades(conn, a, w["exec_date"], "weekly_rebalance", w["week_no"])
    return n


def backfill_weekly_scores(conn: sqlite3.Connection) -> int:
    n = 0
    for w in conn.execute("SELECT * FROM weeks WHERE status='scored' AND simulate=0 ORDER BY week_no").fetchall():
        if conn.execute("SELECT 1 FROM weekly_scores WHERE week_no=?", (w["week_no"],)).fetchone():
            continue
        nxt = conn.execute("SELECT MIN(exec_date) FROM weeks WHERE week_no>? AND status IN ('executed','scored')",
                           (w["week_no"],)).fetchone()[0]
        if nxt is None:
            continue
        rets, marks = {}, {}
        for r in conn.execute("SELECT * FROM scores WHERE week_no=?", (w["week_no"],)):
            m = conn.execute("SELECT id FROM snapshots WHERE agent_id=? AND kind='mark' AND date=? ORDER BY seq LIMIT 1",
                             (r["agent_id"], nxt)).fetchone()
            if m:
                rets[r["agent_id"]], marks[r["agent_id"]] = r["weekly_return"], m[0]
        if rets:
            scores.compute(conn, w["week_no"], w["exec_date"], marks, rets)
            for r in conn.execute("SELECT agent_id, point FROM scores WHERE week_no=?", (w["week_no"],)):
                conn.execute("UPDATE weekly_scores SET point_won=? WHERE week_no=? AND agent_id=?",
                             (r["point"], w["week_no"], r["agent_id"]))
            n += 1
    return n


def run(conn: sqlite3.Connection, force: bool = False, vix_fetch=collector._DEFAULT) -> dict:
    if done(conn) and not force:
        return {"skipped": "already bootstrapped"}
    out: dict = {}
    with transaction(conn):
        out["decisions"] = backfill_decisions(conn)
        out["weekly_scores"] = backfill_weekly_scores(conn)
    last = conn.execute("SELECT MAX(date) FROM prices WHERE ticker='SPY'").fetchone()[0]
    out["market_context_days"] = collector.backfill_context(conn, last, 60, vix_fetch) if last else 0
    out["graded"] = grader.grade_all(conn)
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO league_state(key, value) VALUES (?, ?)",
                     (STATE_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        log_event(conn, "info", "learning", f"bootstrap: {json.dumps(out, default=str)}")
    return out
