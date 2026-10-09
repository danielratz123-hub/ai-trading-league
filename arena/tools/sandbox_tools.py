"""Tools that run agent-written code (always via the sandbox)."""
from __future__ import annotations

import json
import sqlite3

from .. import backtest as bt
from ..config import SETTINGS
from ..sandbox.runner import SandboxLimits, SandboxResult, run_code
from .context import ResearchContext
from .data_tools import ToolError

# Backtests call target_weights once per week, so they get more time.
BACKTEST_LIMITS = SandboxLimits(cpu_seconds=90, wall_seconds=150)


def run_python(ctx: ResearchContext, code: str) -> dict:
    if not code or not code.strip():
        raise ToolError("code is empty.")
    r = run_code("analysis", code, ctx.data_file(), ctx.universe, ctx.decision_date,
                 limits=ctx.sandbox_limits)
    return {"ok": r.ok, "stdout": r.stdout, "error": r.error}


def backtest(ctx: ResearchContext, start: str, end: str | None = None,
             code: str | None = None, portfolio: dict | None = None) -> dict:
    """Backtest either strategy `code` (defining target_weights) or a fixed
    `portfolio` of weights, rebalanced weekly, on history before the decision date."""
    if bool(code) == bool(portfolio):
        raise ToolError("Pass exactly one of `code` or `portfolio`.")
    notes = []
    if end is None or end > ctx.decision_date:
        if end:
            notes.append(f"end clipped to the decision date {ctx.decision_date}.")
        end = ctx.decision_date
    if start >= end:
        raise ToolError("start must be before end.")
    frame = ctx.frame()
    schedule = bt.weekly_schedule(frame.index, start, end)
    # The last exec date would be after the decision date, so it never
    # appears; every scheduled trade uses bars that already happened.
    if len(schedule) < 4:
        raise ToolError("Backtest needs at least 4 weeks of history in the range.")

    if portfolio:
        try:
            w = {str(k).upper(): float(v) for k, v in portfolio.items()}
        except (TypeError, ValueError):
            raise ToolError("portfolio must map tickers to numeric weights.") from None
        weights = {exec_d: w for _, exec_d in schedule}
    else:
        r = run_code("strategy_series", code, ctx.data_file(), ctx.universe, ctx.decision_date,
                     dates=[dec for dec, _ in schedule], limits=BACKTEST_LIMITS)
        if not r.ok:
            return {"ok": False, "error": r.error, "stdout": r.stdout}
        weights = {exec_d: r.series[dec] for dec, exec_d in schedule}

    try:
        res = bt.simulate(frame["open"], frame["close"], weights, start, end, SETTINGS.starting_cash)
    except (KeyError, ValueError) as e:
        raise ToolError(f"Backtest failed: {e}") from None
    return {"ok": True, "notes": notes, **res.to_dict()}


def _validate_on_decision_date(ctx: ResearchContext, code: str) -> SandboxResult:
    return run_code("strategy", code, ctx.data_file(), ctx.universe, ctx.decision_date,
                    limits=ctx.sandbox_limits)


def save_strategy_code(ctx: ResearchContext, slot: int, code: str) -> dict:
    """Validate code by running it at the decision date, then store it."""
    if not isinstance(slot, int) or not 1 <= slot <= SETTINGS.limits.max_slots:
        raise ToolError(f"slot must be 1..{SETTINGS.limits.max_slots}.")
    r = _validate_on_decision_date(ctx, code)
    if not r.ok:
        return {"saved": False, "error": r.error, "stdout": r.stdout}
    ctx.conn.execute(
        "INSERT INTO strategy_code(agent_id, slot, saved_date, code, test_weights) VALUES (?,?,?,?,?)",
        (ctx.agent_id, slot, ctx.decision_date, code, json.dumps(r.weights)))
    return {"saved": True, "slot": slot, "weights_today": r.weights,
            "note": "Use type 'code' for this slot in submit_decision to activate it."}


def latest_code(conn: sqlite3.Connection, agent_id: str, slot: int) -> str | None:
    row = conn.execute("SELECT code FROM strategy_code WHERE agent_id=? AND slot=? "
                       "ORDER BY id DESC LIMIT 1", (agent_id, slot)).fetchone()
    return None if row is None else row["code"]


def run_code_slot(ctx: ResearchContext, slot: int) -> tuple[dict[str, float] | None, str | None]:
    """Decision-time run of a code slot. Returns (weights, None) or
    (None, error). On error the league keeps the slot's previous positions
    and puts the error in the next briefing."""
    code = latest_code(ctx.conn, ctx.agent_id, slot)
    if code is None:
        return None, f"slot {slot} is type 'code' but no code was saved for it."
    r = _validate_on_decision_date(ctx, code)
    if not r.ok:
        return None, r.error
    return r.weights, None


def summarize(result: dict) -> str:
    """Short text for the tool_calls log."""
    s = json.dumps(result, default=str)
    return s if len(s) <= 300 else s[:300] + "..."

