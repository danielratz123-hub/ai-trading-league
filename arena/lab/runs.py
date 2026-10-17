"""The backtest_runs log: every run, and how many tries each idea took.

Every run goes through execute() below, which writes the row BEFORE it
runs (status 'running') and freezes it after (the table's trigger), so a
failed or embarrassing attempt can't be dropped from the count. The results
always carry `attempts` (this idea's run count so far), so "best of 40
tries" is visible next to the numbers.

Purposes:
    design        any period before the holdout (exploring an idea)
    walk_forward  the out-of-sample record (walkforward.walk_forward)
    holdout       the one-time locked-holdout check (once per idea, ever)
    check         engine self-checks (e.g. SPY buy-and-hold); may use the
                  holdout period, never counts as an idea's attempt
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from . import config as labconfig, engine, schema, walkforward
from ..learning import config as lconfig

PURPOSES = ("design", "walk_forward", "holdout", "check")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(conn: sqlite3.Connection) -> None:
    """Commit on a normal connection. The league's connections (db.connect)
    autocommit and nest work in savepoints: committing there would end the
    caller's transaction (e.g. a weekly review applying a rewrite) midway."""
    if conn.isolation_level is not None:
        conn.commit()


def attempts(conn: sqlite3.Connection, idea_key: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE idea_key=? AND purpose != 'check'",
                        (idea_key,)).fetchone()[0]


def holdout_used(conn: sqlite3.Connection, idea_key: str) -> dict | None:
    row = conn.execute("SELECT id, status, results FROM backtest_runs WHERE idea_key=? AND purpose='holdout' "
                       "AND status IN ('running', 'ok') ORDER BY id LIMIT 1", (idea_key,)).fetchone()
    return None if row is None else {"run_id": row[0], "status": row[1]}


def _settings() -> dict:
    lab = labconfig.get()
    return {"costs": asdict(lab.costs), "engine": asdict(lab.engine), "walk_forward": asdict(lab.walk_forward),
            "holdout_months": lab.holdout_months, "hard_limits": asdict(lconfig.get().hard_limits)}


def _begin(conn, *, requested_by: str, idea_key: str, spec: engine.StrategySpec, purpose: str,
           start: str | None, end: str | None, version_id: int | None, params: dict, status: str = "running",
           error: str | None = None) -> tuple[int, int]:
    n = attempts(conn, idea_key) + (0 if purpose == "check" else 1)
    cur = conn.execute(
        "INSERT INTO backtest_runs(requested_by, idea_key, attempt_no, strategy_name, strategy_version_id, code_sha256, "
        "params, purpose, period_start, period_end, status, error, settings, data_note, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (requested_by, idea_key, n, spec.name, version_id, spec.sha256(), json.dumps(params, sort_keys=True), purpose,
         start, end, status, error, json.dumps(_settings()), labconfig.get().survivorship_note, _now(),
         None if status == "running" else _now()))
    _commit(conn)
    return cur.lastrowid, n


def _finish(conn, run_id: int, status: str, results: dict | None = None, error: str | None = None,
            trades: list[dict] | None = None) -> None:
    if trades:
        conn.executemany(
            "INSERT INTO backtest_trades(run_id, symbol, side, entry_day, entry_price, exit_day, exit_price, shares, "
            "pnl, pnl_pct, exit_reason, entry_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, t["symbol"], t["side"], t["entry_day"], t["entry_price"], t["exit_day"], t["exit_price"],
              t["shares"], t["pnl"], t["pnl_pct"], t["exit_reason"], t["entry_state"]) for t in trades])
    conn.execute("UPDATE backtest_runs SET status=?, results=?, error=?, finished_at=? WHERE id=?",
                 (status, None if results is None else json.dumps(results, default=str), error, _now(), run_id))
    _commit(conn)


def execute(conn: sqlite3.Connection, hconn: sqlite3.Connection, *, spec: engine.StrategySpec | None = None,
            make: Callable[[dict], engine.StrategySpec] | None = None, grid: list[dict] | None = None,
            purpose: str, idea_key: str, requested_by: str, start: str | None = None, end: str | None = None,
            version_id: int | None = None, options: engine.Options | None = None,
            frame: pd.DataFrame | None = None, walk: bool = False) -> dict:
    """Run and log one backtest. Returns the results (with attempts and the
    survivorship note) or {"ok": False, "error"/"refused": ...}.
    walk=True with purpose 'check': a walk-forward used as a baseline (e.g.
    the champion or SPY next to a challenger); it never counts as an attempt."""
    walk = walk or purpose == "walk_forward"
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    schema.migrate(conn)
    if make is None:
        if spec is None:
            raise ValueError("give spec or make")
        make = lambda p, s=spec: s if not p else engine.StrategySpec(s.name, s.fn, s.code, {**s.params, **p})  # noqa: E731
    base = make((grid or [{}])[0])
    if options is not None and options.benchmark and purpose != "check":
        raise ValueError("benchmark mode (no stops, no position cap) is for engine checks only")
    from .states import labels as state_labels
    d_end = walkforward.data_end(hconn)
    days = [d for d, in hconn.execute("SELECT day FROM hist_calendar WHERE day <= ? ORDER BY day", (d_end,))]
    locked = walkforward.holdout_start(d_end, days=days)
    first = hconn.execute("SELECT MIN(day) FROM hist_bars_daily WHERE symbol='SPY'").fetchone()[0]
    if purpose == "holdout":
        start, end = locked, d_end
        used = holdout_used(conn, idea_key)
        if used:
            rid, n = _begin(conn, requested_by=requested_by, idea_key=idea_key, spec=base, purpose="holdout",
                            start=start, end=end, version_id=version_id, params=base.params, status="refused",
                            error=f"the holdout was already used by run {used['run_id']}")
            return {"ok": False, "refused": f"holdout already used for this idea (run {used['run_id']})",
                    "run_id": rid, "attempts": n}
    else:
        start = start or first
        end = end or (d_end if purpose == "check" and not walk else
                      (pd.Timestamp(locked) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        if purpose != "check":
            try:
                walkforward.check_period(end, purpose, locked)
            except walkforward.HoldoutError as e:
                rid, n = _begin(conn, requested_by=requested_by, idea_key=idea_key, spec=base, purpose=purpose,
                                start=start, end=end, version_id=version_id, params=base.params, status="refused",
                                error=str(e))
                return {"ok": False, "refused": str(e), "run_id": rid, "attempts": n}
    rid, n = _begin(conn, requested_by=requested_by, idea_key=idea_key, spec=base, purpose=purpose, start=start,
                    end=end, version_id=version_id, params={"grid": grid} if grid else base.params)
    try:
        frame = frame if frame is not None else walkforward.load(hconn, end)
        frame = frame.loc[:end]
        labels = state_labels(conn, end=end)
        if walk:
            out = walkforward.walk_forward(make, grid or [{}], frame, labels, start, locked, options)
            res, trades = out["out_of_sample"], out["trades"]
            res = {**res, "windows": out["windows"], "grid_size": out["grid_size"]}
        else:
            r = engine.run(base, frame, start, end, labels, options)
            res, trades = r.metrics, r.trades
            res = {**res, "notes": r.notes[:20]}
    except Exception as e:  # noqa: BLE001 - every failure is logged as an attempt
        _finish(conn, rid, "failed", error=f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "run_id": rid, "attempts": n}
    res = {**res, "ok": True, "run_id": rid, "purpose": purpose, "idea_key": idea_key, "attempts": n,
           "holdout_from": locked}
    _finish(conn, rid, "ok", res, trades=trades)
    return res
