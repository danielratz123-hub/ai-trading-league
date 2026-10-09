"""The final decision: its tool schema, parsing, and conversion into
validated ledger targets.

Nothing here trusts the agent: every field is type-checked, code slots are
run in the sandbox, and the combined portfolio goes through
risk.validate_portfolio before anything is accepted.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .. import ledger, risk
from ..config import SETTINGS
from ..ledger import SlotTarget
from ..prices import price_on_or_before
from ..tools.context import ResearchContext
from ..tools.sandbox_tools import run_code_slot

SUBMIT_SPEC = {
    "name": "submit_decision",
    "description": (
        "Submit your FINAL portfolio decision for this week. Call it exactly once when you are done "
        "researching. The engine validates it against the hard limits; if it breaks one you get the "
        "errors back and can fix and resubmit (max 2 retries). Omitted slots are closed and their "
        "money goes to cash. For 'manual' slots, `positions` are weights within the slot "
        "(negative = short, sum of |weights| <= 1). For 'code' slots, omit `positions`: the engine "
        "runs the code you saved with save_strategy_code for that slot; if it crashes, the decision "
        "is rejected so you can fix it."),
    "parameters": {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "description": "Short analysis of current conditions."},
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slot": {"type": "integer", "description": "1-5"},
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["manual", "code"]},
                        "allocation": {"type": "number", "description": "Fraction of total equity, 0-1"},
                        "positions": {"type": "object", "additionalProperties": {"type": "number"},
                                      "description": "manual slots: {ticker: weight within slot}"},
                        "risk_orders": {
                            "type": "object",
                            "description": "{ticker: {stop_loss: price, take_profit: price}}; checked "
                                           "at each close, filled at the next open.",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {"stop_loss": {"type": "number"},
                                               "take_profit": {"type": "number"}}}},
                        "reason": {"type": "string", "description": "Why this slot, in a sentence or two."},
                        "position_reasons": {
                            "type": "object", "additionalProperties": {"type": "string"},
                            "description": "{ticker: one line on why you hold it}. Shown next to each trade."},
                    },
                    "required": ["slot", "name", "type", "allocation", "reason"],
                },
            },
            "notes_to_self": {"type": "string", "description": "Anything to remember next week."},
        },
        "required": ["market_view", "slots", "notes_to_self"],
    },
}


@dataclass
class Checked:
    ok: bool
    message: str                          # text returned to the agent
    targets: list[SlotTarget] = field(default_factory=list)
    slot_runs: list[dict] = field(default_factory=list)   # code-slot outcomes
    errors: list[str] = field(default_factory=list)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse(raw) -> tuple[list[dict], list[str]]:
    """Structural checks only. Returns (normalized slots, errors)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return [], ["The decision must be a JSON object."]
    for key in ("market_view", "notes_to_self"):
        if not isinstance(raw.get(key), str):
            errors.append(f"`{key}` must be a string.")
    slots = raw.get("slots")
    if not isinstance(slots, list):
        return [], errors + ["`slots` must be a list (it may be empty to hold only cash)."]
    out = []
    for i, s in enumerate(slots):
        where = f"slots[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where} must be an object.")
            continue
        slot = s.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool):
            errors.append(f"{where}.slot must be an integer 1-{SETTINGS.limits.max_slots}.")
            continue
        typ = s.get("type")
        if typ not in ("manual", "code"):
            errors.append(f"Slot {slot}: type must be 'manual' or 'code'.")
            continue
        if not _num(s.get("allocation")):
            errors.append(f"Slot {slot}: allocation must be a number.")
            continue
        positions = s.get("positions") or {}
        if typ == "manual":
            if not isinstance(positions, dict) or not all(_num(v) for v in positions.values()):
                errors.append(f"Slot {slot}: positions must map tickers to numeric weights.")
                continue
        risk_orders = s.get("risk_orders") or {}
        if not isinstance(risk_orders, dict) or not all(isinstance(v, dict) for v in risk_orders.values()):
            errors.append(f"Slot {slot}: risk_orders must map tickers to {{stop_loss, take_profit}}.")
            continue
        out.append({
            "slot": slot, "type": typ, "allocation": float(s["allocation"]),
            "name": str(s.get("name", ""))[:80], "reason": str(s.get("reason", "")),
            "positions": {str(k).upper().strip(): float(v) for k, v in positions.items()} if typ == "manual" else {},
            "risk_orders": {str(k).upper().strip(): {kk: v[kk] for kk in ("stop_loss", "take_profit") if v.get(kk) is not None}
                            for k, v in risk_orders.items()},
            "notes": {str(k).upper().strip(): str(v).strip()[:300]
                      for k, v in (s.get("position_reasons") or {}).items()
                      if isinstance(s.get("position_reasons"), dict) and str(v).strip()},
        })
    return out, errors


def _rejected(errors: list[str], targets=None, runs=None) -> Checked:
    return Checked(False, "Your decision was REJECTED:\n" + "\n".join(f"  - {e}" for e in errors),
                   targets or [], runs or [], errors)


def check(ctx: ResearchContext, raw: dict) -> Checked:
    """Turn a submitted decision into validated targets, or explain why not."""
    slots, errors = parse(raw)
    if errors:
        return _rejected(errors)

    conn = ctx.conn
    targets, runs, code_errors = [], [], []
    for s in slots:
        if s["type"] == "manual":
            targets.append(SlotTarget(s["slot"], s["allocation"], s["positions"], s["name"], "manual",
                                      risk_orders=s["risk_orders"], reason=s["reason"], notes=s["notes"]))
            continue
        weights, err = run_code_slot(ctx, s["slot"])
        runs.append({"slot": s["slot"], "ok": err is None, "weights": weights, "error": err})
        if err is None:
            targets.append(SlotTarget(s["slot"], s["allocation"], weights, s["name"], "code",
                                      risk_orders=s["risk_orders"], reason=s["reason"], notes=s["notes"]))
        else:
            # A crashing code slot is a validation error: the agent must fix it.
            code_errors.append(f"Slot {s['slot']}: your strategy code failed at the decision date. Fix it "
                               f"with save_strategy_code (or make the slot manual). Error:\n{err[-800:]}")
    if code_errors:
        return _rejected(code_errors, targets, runs)

    # Latest close per ticker with a risk order, to sanity-check stop levels.
    last_prices = {}
    for t in targets:
        for ticker in t.risk_orders:
            p = price_on_or_before(conn, ticker, ctx.decision_date)
            if p:
                last_prices[ticker] = p[1]
    res = risk.validate_portfolio(targets, last_prices,
                                  reduce_only=held_removed_weights(conn, ctx.agent_id, ctx.decision_date))
    if not res.ok:
        return Checked(False, res.message(), targets, runs, res.errors)

    e = res.exposures
    return Checked(True, f"Decision ACCEPTED. Exposure: gross {e['gross']:.1%}, long {e['long']:.1%}, "
                         f"short {e['short']:.1%}. Orders execute at the next trading day's open.",
                   targets, runs)


def held_removed_weights(conn, agent_id: str, d: str) -> dict[tuple[int, str], float]:
    """{(slot, ticker): weight in the slot} for held tickers that have left
    the S&P 500 (see universe.REMOVED), valued at d's close."""
    from .. import universe
    held = [p for p in ledger.positions(conn, agent_id) if p["ticker"] in universe.REMOVED and p["shares"]]
    if not held:
        return {}
    val = ledger.value_agent(conn, agent_id, d, "close")
    out = {}
    for p in held:
        eq = val.slot_equity.get(p["slot"], 0.0)
        price = val.prices.get(p["ticker"])
        if eq > 0 and price:
            out[(p["slot"], p["ticker"])] = p["shares"] * price / eq
    return out


def targets_to_json(targets: list[SlotTarget]) -> str:
    return json.dumps([t.__dict__ for t in targets], sort_keys=True)


def targets_from_json(text: str) -> list[SlotTarget]:
    return [SlotTarget(**d) for d in json.loads(text)]
