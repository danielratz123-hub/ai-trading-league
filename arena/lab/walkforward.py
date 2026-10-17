"""Walk-forward testing and the locked holdout.

    |--- design 24m ---|-- test 6m --|
            |--- design 24m ---|-- test 6m --|
                    |--- design 24m ---|-- test 6m --|     ...  |== holdout 12m ==|

* Every window: if the idea has a parameter grid, each candidate runs on the
  design window and the best one (league risk-adjusted score) runs on the
  following test window. Only the test windows count: they are stitched into
  one out-of-sample record (equity chained, trades added up).
* The holdout is the most recent `holdout.months` of data. No design, walk-
  forward or agent run may touch it (HoldoutError); only the one-time
  promotion check (purpose 'holdout', once per idea, runs.py) reads it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from . import config as labconfig, engine, histdata


class HoldoutError(RuntimeError):
    """A design-time run reached into the locked holdout."""


def data_end(hconn: sqlite3.Connection) -> str:
    end = hconn.execute("SELECT MAX(day) FROM hist_bars_daily WHERE symbol='SPY'").fetchone()[0]
    if end is None:
        raise RuntimeError("history.db has no SPY bars; run history-download first")
    return end


def holdout_start(end: str, months: int | None = None, days: list[str] | None = None) -> str:
    """First trading day of the locked window: `months` before the data end."""
    months = labconfig.get().holdout_months if months is None else months
    cut = (pd.Timestamp(end) - pd.DateOffset(months=months)).strftime("%Y-%m-%d")
    if days:
        later = [d for d in days if d > cut]
        if later:
            return later[0]
    return (pd.Timestamp(cut) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def check_period(end: str, purpose: str, locked_from: str) -> None:
    if purpose != "holdout" and end >= locked_from:
        raise HoldoutError(f"{purpose} runs must end before the locked holdout ({locked_from}); asked for {end}")


@dataclass(frozen=True)
class Window:
    design_start: str
    design_end: str
    test_start: str
    test_end: str


def windows(first: str, locked_from: str, design_months: int | None = None, test_months: int | None = None,
            step_months: int | None = None) -> list[Window]:
    wf = labconfig.get().walk_forward
    dm, tm, sm = (design_months or wf.design_months, test_months or wf.test_months, step_months or wf.step_months)
    out = []
    ds = pd.Timestamp(first)
    stop = pd.Timestamp(locked_from) - pd.Timedelta(days=1)
    while True:
        de = ds + pd.DateOffset(months=dm) - pd.Timedelta(days=1)
        ts = de + pd.Timedelta(days=1)
        te = min(ts + pd.DateOffset(months=tm) - pd.Timedelta(days=1), stop)
        if ts > stop or te - ts < pd.Timedelta(days=20):
            break
        out.append(Window(*(x.strftime("%Y-%m-%d") for x in (ds, de, ts, te))))
        ds = ds + pd.DateOffset(months=sm)
    return out


def walk_forward(make: Callable[[dict], engine.StrategySpec], grid: list[dict], frame: pd.DataFrame,
                 labels: dict[str, str | None], first: str, locked_from: str,
                 options: engine.Options | None = None) -> dict:
    """Out-of-sample record of an idea. make(params) builds the strategy."""
    wins = windows(first, locked_from)
    if not wins:
        raise ValueError(f"not enough history for one walk-forward window between {first} and {locked_from}")
    grid = grid or [{}]
    capital = (options.capital if options and options.capital is not None
               else labconfig.get().engine.starting_cash)
    per_window = []
    eq_parts: list[pd.Series] = []
    trades: list[dict] = []
    exec_days: list[str] = []
    fills = 0
    level = capital
    for w in wins:
        check_period(w.test_end, "walk_forward", locked_from)
        best, best_score, design = grid[0], None, None
        if len(grid) > 1:
            for params in grid:
                r = engine.run(make(params), frame, w.design_start, w.design_end, labels, options)
                sc = r.metrics["risk_adjusted_score"]
                if best_score is None or sc > best_score:
                    best, best_score, design = params, sc, r.metrics
        test = engine.run(make(best), frame, w.test_start, w.test_end, labels, options)
        k = level / capital
        eq_parts.append(test.equity * k)
        level = float(test.equity.iloc[-1] * k)
        trades += test.trades
        exec_days += [ex for _, ex in test.schedule]
        fills += test.fills
        per_window.append({"design": [w.design_start, w.design_end], "test": [w.test_start, w.test_end],
                           "first_exec": test.schedule[0][1],
                           "params": best, "design_score": None if design is None else design["risk_adjusted_score"],
                           "result": {k2: test.metrics[k2] for k2 in ("total_return_pct", "max_drawdown_pct",
                                                                   "risk_adjusted_score", "trades", "spy_return_pct")}})
    eq = pd.concat(eq_parts)
    eq = eq[~eq.index.duplicated(keep="last")]
    dates = [d.strftime("%Y-%m-%d") for d in frame.index]
    spy = _spy_chain(frame, per_window)
    oos = engine.metrics(eq, trades, fills, capital, exec_days, labels, dates, spy)
    return {"out_of_sample": oos, "windows": per_window, "grid_size": len(grid), "trades": trades}


def _spy_chain(frame: pd.DataFrame, per_window: list[dict]) -> float | None:
    """SPY over the same test windows, chained (open of the first rebalance -> last close)."""
    if "SPY" not in frame["close"].columns:
        return None
    g = 1.0
    for w in per_window:
        o = frame["open"]["SPY"].loc[w["first_exec"]:w["test"][1]]
        c = frame["close"]["SPY"].loc[w["test"][0]:w["test"][1]]
        if o.empty:
            continue
        g *= float(c.iloc[-1] / o.iloc[0])
    return g - 1


def load(hconn: sqlite3.Connection, end: str, symbols: list[str] | None = None) -> pd.DataFrame:
    """All history up to `end` (the engine and the strategies cut it further)."""
    return histdata.bars(hconn, symbols, end=end)
