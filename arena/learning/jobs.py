"""The learning layer's scheduled jobs (each runs as `python -m arena.main <job>`):

    risk-check     trading days, hourly 10:00-15:30 ET     riskcheck.run_check
    daily-review   trading days, 16:30 ET, after `daily`    collector.after_close -> grader -> review per agent
    weekly-review  Saturday 12:00 ET, before `decide`       grader -> shadow verdicts -> review per agent
                                                            -> promote confirmed lessons
The 1-minute collector runs inside the scheduler (scheduler.py), not as a job.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .. import ledger
from ..db import transaction
from ..league import LeagueError, is_paused
from ..trading_calendar import is_trading_day, last_trading_day_on_or_before
from . import bootstrap, collector, grader, lessons, review, riskcheck, shadow
from .review import RunnerResult


def fake_runner(agent_id: str, kind: str, prompt: str, inp: dict) -> RunnerResult:
    """--fake-agents: a valid 'hold' answer without calling any model (no plan usage)."""
    out: dict = {"reasoning": f"[fake agent for a dry run] {kind} review: no changes.", "confidence": 0.5}
    if kind == "risk":
        slots = sorted({f["slot"] for f in inp.get("situation", {}).get("fired", []) if f.get("slot")}) or \
            [s["slot"] for s in inp["strategies"]][:1]
        out["actions"] = [{"type": "hold", "slot": s} for s in slots] or [{"type": "hold", "slot": 1}]
    return RunnerResult(out, attempts=1)


def after_grading(conn: sqlite3.Connection) -> None:
    """A review sees only outcomes evaluated strictly BEFORE it starts; make
    sure the clock has moved past the grading that just ran (coarse clocks
    can return the same instant twice)."""
    last = conn.execute("SELECT MAX(evaluated_at) FROM outcomes_history").fetchone()[0]
    deadline = time.monotonic() + 1.0
    while last and datetime.now(timezone.utc).isoformat() <= last and time.monotonic() < deadline:
        time.sleep(0.002)


def ensure_bootstrap(conn: sqlite3.Connection) -> dict | None:
    return None if bootstrap.done(conn) else bootstrap.run(conn)


def close_checkpoint(conn: sqlite3.Connection, d: str, **fetchers) -> dict:
    """After the close (no LLM): snapshots + market context, outcomes, strategy equity."""
    if conn.execute("SELECT 1 FROM snapshots WHERE kind='daily' AND date=? LIMIT 1", (d,)).fetchone() is None:
        raise LeagueError(f"The daily job hasn't processed {d} yet (prices and snapshots); run `daily` first.")
    out = {"collector": collector.after_close(conn, d, **fetchers)}
    out["grader"] = grader.grade_all(conn)
    ts = collector.close_ts(conn, d)
    with transaction(conn):
        for a in ledger.agent_ids(conn):
            grader.write_portfolio_snapshots(conn, a, ts, ledger.value_agent(conn, a, d, "close"))
    out["pruned_minute_bars"] = collector.prune_minute_bars(conn)
    return out


def daily_review(conn: sqlite3.Connection, d: str, runner: review.Runner, dry_run: bool = False,
                 **fetchers) -> dict:
    if not is_trading_day(conn, d):
        return {"skipped": f"{d} is not a trading day"}
    out = {"bootstrap": ensure_bootstrap(conn), "close": close_checkpoint(conn, d, **fetchers), "reviews": []}
    if is_paused(conn):
        out["reviews"] = "skipped: the league is paused"
        return out
    after_grading(conn)
    for a in ledger.agent_ids(conn):
        if is_paused(conn, a):
            out["reviews"].append({"agent": a, "status": "skipped: agent paused"})
            continue
        out["reviews"].append(review.run_review(conn, a, "daily", d, runner, dry_run=dry_run))
    return out


def weekly_review(conn: sqlite3.Connection, day: str, runner: review.Runner, dry_run: bool = False) -> dict:
    """day: the review's calendar day (a Saturday); the week ended at the last
    trading day on or before it."""
    out: dict = {"bootstrap": ensure_bootstrap(conn), "grader": grader.grade_all(conn), "agents": {}}
    week_end = last_trading_day_on_or_before(conn, day)
    if is_paused(conn):
        out["agents"] = "skipped: the league is paused"
        return out
    after_grading(conn)
    for a in ledger.agent_ids(conn):
        if is_paused(conn, a):
            out["agents"][a] = {"skipped": "agent paused"}
            continue
        with transaction(conn):
            verdicts = shadow.evaluate(conn, a, week_end, day)
        res = review.run_review(conn, a, "weekly", day, runner, dry_run=dry_run)
        with transaction(conn):
            promoted = lessons.promote_ready(conn, a)
        out["agents"][a] = {"shadow": verdicts, "review": res, "lessons_promoted": promoted}
    return out


def risk_check(conn: sqlite3.Connection, d: str, runner: review.Runner | None, dry_run: bool = False,
               after_fills=None, now: datetime | None = None) -> dict:
    if not is_trading_day(conn, d):
        return {"skipped": f"{d} is not a trading day"}
    ensure_bootstrap(conn)
    return riskcheck.run_check(conn, d, now or datetime.now(timezone.utc), runner, dry_run, after_fills)
