"""The learning layer's part in the existing weekly decision (Saturday).

With LEARNING_ENABLED=1:
  * submit_decision gets a few more fields: per slot `confidence` (0-1,
    required), `rules` (required when starting a new strategy in an empty
    slot), `challenger_positions` (required for a slot with a challenger in
    its shadow test); top level `watchlist` (optional).
  * every accepted decision is journaled: one decisions row per position
    change (buy/sell/short/cover) and one 'hold' row per unchanged strategy.
  * from scoring.start_week (rules_active), the decision must also follow
    each slot's strategy: same strategy (rule changes go through the weekly
    review), position sizes within its position_max_pct, a stop on every
    position within its stop_loss_pct; a new strategy in an empty slot uses
    the week's one rewrite; plus all hard limits.
    Before start_week nothing is enforced: strategies are only tracked.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3

from .. import ledger
from ..ledger import SlotTarget
from ..prices import price_on_or_before
from . import collector, config as lconfig, hardlimits, journal, shadow, strategies as strat
from .review import strategy_rule_errors

_SLOT_FIELDS = {
    "confidence": {"type": "number", "description": "How confident you are in this slot's plan, 0-1."},
    "rules": {"type": "string", "description": "Only for a NEW strategy in an empty slot: its rules in plain "
              "words (entry, exit, sizing). Existing strategies keep their rules; change them in the weekly review."},
    "challenger_positions": {"type": "object", "additionalProperties": {"type": "number"},
                             "description": "Only for a slot whose strategy has a challenger: the weights you would "
                                            "hold under the CHALLENGER's rules (simulated, no money)."},
}


def submit_spec(base: dict) -> dict:
    """submit_decision's schema, extended when the layer is on."""
    if not lconfig.enabled():
        return base
    spec = copy.deepcopy(base)
    item = spec["parameters"]["properties"]["slots"]["items"]
    item["properties"].update(copy.deepcopy(_SLOT_FIELDS))
    item["required"] = list(item["required"]) + ["confidence"]
    spec["parameters"]["properties"]["watchlist"] = {
        "type": "array", "items": {"type": "string"},
        "description": f"Optional: up to {lconfig.get().market_data.watchlist_max} tickers to watch minute by minute "
                       f"(besides what you hold)."}
    return spec


def week_of(conn: sqlite3.Connection, decision_date: str) -> int | None:
    row = conn.execute("SELECT week_no FROM weeks WHERE decision_date=?", (decision_date,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT COALESCE(MAX(week_no), 0) + 1 FROM weeks").fetchone()
    return row[0]


def _num01(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1


def held_weights(conn: sqlite3.Connection, agent_id: str, d: str) -> dict[tuple[int, str], float]:
    val = ledger.value_agent(conn, agent_id, d, "close")
    out = {}
    for p in ledger.positions(conn, agent_id):
        eq = val.slot_equity.get(p["slot"], 0.0)
        if eq > 0:
            out[(p["slot"], p["ticker"])] = p["shares"] * val.prices[p["ticker"]] / eq
    return out


def check(conn: sqlite3.Connection, agent_id: str, decision_date: str, raw: dict,
          targets: list[SlotTarget]) -> list[str]:
    """Extra validation of a weekly decision (may attach engine stops to code
    slots' targets). Empty list = fine."""
    if not lconfig.enabled():
        return []
    errors: list[str] = []
    slots_raw = {s.get("slot"): s for s in raw.get("slots") or [] if isinstance(s, dict)}
    for n, s in slots_raw.items():
        if not _num01(s.get("confidence")):
            errors.append(f"Slot {n}: confidence (a number from 0 to 1) is required.")
    wl = raw.get("watchlist")
    if wl is not None and (not isinstance(wl, list) or not all(isinstance(t, str) for t in wl)):
        errors.append("watchlist must be a list of tickers.")
    week_no = week_of(conn, decision_date)
    if not hardlimits.active(conn, week_no):
        return errors
    now = journal.now_utc()
    new_count = 0
    last = {}
    for t in targets:
        for ticker in t.weights:
            p = price_on_or_before(conn, ticker, decision_date)
            if p:
                last[ticker] = p[1]
    for t in targets:
        s = strat.in_slot(conn, agent_id, t.slot)
        rs = slots_raw.get(t.slot, {})
        if s is not None and s.name.strip().lower() != (t.name or "").strip().lower():
            errors.append(f"Slot {t.slot} runs the strategy \"{s.name}\" (v{s.version}). A different strategy can't "
                          f"replace it in the weekly decision: keep the name and follow its rules, propose a rewrite "
                          f"in the weekly review, or leave the slot out to retire the strategy.")
            continue
        params = s.params if s else {**lconfig.default_params()}
        if s is None:
            new_count += 1
            if not isinstance(rs.get("rules"), str) or len(rs["rules"].strip()) < 20:
                errors.append(f"Slot {t.slot}: a new strategy needs `rules` (its rules in plain words, 20+ characters).")
            ok, why = strat.new_strategy_allowed(conn, agent_id, t.slot, now)
            if not ok or new_count > 1:
                errors.append(f"Slot {t.slot}: can't start a new strategy: "
                              f"{why if not ok else 'only one new strategy or rewrite per week'}.")
        if t.type == "code":
            hardlimits.attach_default_stops(conn, t, decision_date, params["stop_loss_pct"])
        errors += strategy_rule_errors(t, params, last)
        if s is not None and s.challenger is not None:
            cp = rs.get("challenger_positions")
            if not isinstance(cp, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                   for v in cp.values()):
                errors.append(f"Slot {t.slot}: its strategy has a challenger (v{s.challenger['version']}) in a shadow "
                              f"test; give challenger_positions (weights under the challenger's rules).")
            else:
                ch = SlotTarget(t.slot, t.allocation, {k.upper().strip(): float(v) for k, v in cp.items()}, t.name,
                                "manual")
                errs = [e for e in hardlimits.validate_slots(conn, [ch], decision_date) if "stop_loss" not in e]
                errs += [e for e in strategy_rule_errors(ch, {"position_max_pct": s.challenger["params"].get(
                    "position_max_pct")}, {})]
                errors += [f"challenger_positions: {e}" for e in errs]
    errors += hardlimits.validate_slots(conn, targets, decision_date, held_weights(conn, agent_id, decision_date))
    return errors


def after_accepted(conn: sqlite3.Connection, agent_id: str, week_no: int, decision_date: str, exec_date: str,
                   raw: dict, targets: list[SlotTarget], prompt: str, decided_at: str | None = None) -> list[int]:
    """Journal an accepted weekly decision and keep strategies in sync with
    the slots. Runs inside the caller's transaction. Returns decision ids."""
    if not lconfig.enabled():
        return []
    decided_at = decided_at or journal.now_utc()
    active_rules = hardlimits.active(conn, week_no)
    sha = journal.store_input(conn, prompt, decided_at)
    regime = journal.regime_as_of(conn, decided_at)
    slots_raw = {s.get("slot"): s for s in raw.get("slots") or [] if isinstance(s, dict)}
    by_slot = {t.slot: t for t in targets}
    held = held_weights(conn, agent_id, decision_date)
    ids: list[int] = []

    def rec(version_id, action, reasoning, conf, symbol=None, weight=None):
        ids.append(journal.record(conn, agent_id=agent_id, version_id=version_id, checkpoint="weekly_rebalance",
                                  input_sha=sha, input_kind="weekly_decision", reasoning=reasoning, action=action,
                                  confidence=conf, symbol=symbol, target_weight=weight, regime=regime,
                                  week_no=week_no, source_ref=f"week:{week_no}", decided_at=decided_at))

    # Strategies leaving: record the exits under the old version, then retire.
    for s in strat.active(conn, agent_id):
        t = by_slot.get(s.slot)
        if t is not None and s.name.strip().lower() == (t.name or "").strip().lower():
            continue
        for (slot, ticker), w in sorted(held.items()):
            if slot == s.slot and abs(w) > 1e-9:
                rec(s.version_id, "sell" if w > 0 else "cover", f"strategy \"{s.name}\" retired", None, ticker, 0.0)
        strat.retire(conn, s.strategy_id, week_no, decided_at)
    # Strategies staying or arriving.
    for t in sorted(targets, key=lambda x: x.slot):
        rs = slots_raw.get(t.slot, {})
        conf = float(rs["confidence"]) if _num01(rs.get("confidence")) else None
        s = strat.in_slot(conn, agent_id, t.slot)
        if s is None:
            kind = "new_strategy" if active_rules else "initial"
            rules = rs.get("rules") if isinstance(rs.get("rules"), str) and rs["rules"].strip() else (t.reason or t.name)
            s = strat.create(conn, agent_id, t.slot, t.name or f"slot {t.slot}", rules, None, kind,
                             "started in the weekly decision", week_no, decided_at)
        n = 0
        for ticker in sorted(set(t.weights) | {k for (sl, k) in held if sl == t.slot}):
            old, new = held.get((t.slot, ticker), 0.0), t.weights.get(ticker, 0.0)
            action = journal.action_for(old, new) or ("hold" if new else None)
            if action:
                n += 1
                why = t.notes.get(ticker) or (t.exits.get(ticker) if not new else None) or t.reason
                rec(s.version_id, action, why or "(no reason given)", conf, ticker, round(new, 6))
        if not n:
            rec(s.version_id, "hold", t.reason or "all cash", conf)
        cp = rs.get("challenger_positions")
        if s.challenger is not None and isinstance(cp, dict):
            shadow.start(conn, agent_id, t.slot, week_no, exec_date,
                         {k.upper().strip(): float(v) for k, v in cp.items()}, sha, t.reason, conf, regime, decided_at)
    if isinstance(raw.get("watchlist"), list):
        collector.set_watchlist(conn, agent_id, raw["watchlist"], week_no)
    return ids


def briefing_section(conn: sqlite3.Connection, agent_id: str, week_no: int, decision_date: str) -> list[str]:
    """What the weekly briefing adds when the layer is on."""
    if not lconfig.enabled():
        return []
    cfg = lconfig.get()
    from . import lessons
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    hist = collector.regime_history(conn, now, 30)
    regime = hist[-1][1] if hist else None
    active_rules = hardlimits.active(conn, week_no)
    hl = cfg.hard_limits
    lines = ["", "## Your strategies (learning layer)",
             f"Market regime: {regime or 'unknown'}."]
    if active_rules:
        lines += [
            "From this week the NEW RULES apply (enforced by the engine):",
            "- Each slot runs its strategy below. Keep the slot's name; pick positions freely within its rules. "
            "Weights <= the strategy's position_max_pct, and every position needs a stop_loss no further than its "
            "stop_loss_pct from the last close (code slots get an engine stop at that distance).",
            f"- Hard limits per strategy: no position above {hl.max_position_pct_of_strategy:.0%} of the strategy, "
            f"gross <= {hl.max_gross_exposure:.0%}, shorts <= {hl.max_short_exposure_pct:.0%}, price >= "
            f"${hl.min_price:.2f}, 20-day average volume >= {hl.min_avg_daily_volume:,.0f} shares, at most "
            f"{hl.max_trades_per_strategy_per_day} trades per strategy per day outside the weekly rebalance.",
            "- To change a strategy's rules: propose a rewrite in the weekly review (it is tested in shadow first). "
            f"Leaving a slot out retires its strategy; a new strategy in an empty slot needs `rules` and uses the "
            f"week's one rewrite ({cfg.learning.rewrites_per_agent_per_week}/week).",
            f"- Scoring from week {cfg.scoring.start_week}: weekly return % / max(weekly max drawdown %, "
            f"{cfg.scoring.drawdown_floor_pct:g}%); the best score wins the point; a broken hard limit scores 0."]
    else:
        lines.append(f"(Tracking only until week {cfg.scoring.start_week}: your slots are recorded as strategies, "
                     f"nothing extra is enforced yet.)")
    lines.append("Give each slot a `confidence` (0-1).")
    for s in strat.active(conn, agent_id):
        cool, why = strat.in_cooldown(conn, s.strategy_id, now)
        lines.append(f"- Slot {s.slot} \"{s.name}\" v{s.version}: params {json.dumps(s.params)}"
                     f"{'; COOLDOWN ' + why if cool else ''}{'; EMERGENCY-FLAGGED' if s.emergency_flagged_at else ''}\n"
                     f"  rules: {s.rules_text[:800]}")
        if s.challenger:
            lines.append(f"  CHALLENGER v{s.challenger['version']} in shadow test: give challenger_positions for this "
                         f"slot. Its params {json.dumps(s.challenger['params'])}; rules: {s.challenger['rules_text'][:800]}")
    act = lessons.for_prompt(conn, agent_id, regime, now)
    if act:
        lines += ["", "Active lessons (confirmed several times):"] + [f"- [{l['key']}] {l['lesson']}" for l in act]
    return lines
