"""Backtest lab, phase 3: the engine, walk-forward, holdout and run log."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from arena.db import connect
from arena.lab import config as labconfig, engine, histdata, runs, states, strategies, walkforward
from arena.prices import BAR_COLUMNS
from arena.tests.conftest import weekdays

TICKERS = ["SPY", "AAPL", "MSFT", "NVDA", "AMD", "XOM", "KO", "PFE", "F"]
DAYS = weekdays("2016-01-04", "2019-12-31")
BPS = labconfig.get().costs.bps_per_side / 1e4


def make_frame(days=DAYS, seed=7, overrides=None) -> pd.DataFrame:
    """Random walks with a drift per ticker; open = previous close +/- noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for k, t in enumerate(TICKERS):
        px = 50.0 + 10 * k
        drift = 0.0003 if t == "SPY" else 0.0004 * (k - 3)
        for d in days:
            o = px * (1 + rng.normal(0, 0.003))
            c = o * (1 + drift + rng.normal(0, 0.012))
            hi, lo = max(o, c) * 1.004, min(o, c) * 0.996
            rows.append((t, d, o, hi, lo, c, 3e6))
            px = c
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    if overrides:
        df = overrides(df)
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="ticker", values=["open", "high", "low", "close", "volume"]).sort_index(axis=1)


FRAME = make_frame()
UNI = set(TICKERS)


def opts(**kw):
    return engine.Options(universe=UNI, **kw)


# ------------------------------------------------------------------ lookahead

def test_a_strategy_reading_tomorrow_raises():
    def peeker(prices, date):
        nxt = prices.index[prices.index.get_loc(pd.Timestamp(date))] + pd.Timedelta(days=1)
        prices["close"].loc[nxt]               # tomorrow is not in the frame it was given
        return {}

    with pytest.raises(engine.LookaheadError):
        engine.run(engine.StrategySpec("peek", fn=peeker), FRAME, "2017-01-02", "2017-03-31", options=opts())


def test_every_call_sees_only_bars_up_to_its_decision_day():
    seen = []

    def spy(prices, date):
        seen.append((date, prices.index.max().strftime("%Y-%m-%d")))
        future = prices["close"]["SPY"].shift(-1).iloc[-1]   # "tomorrow" is not there: NaN
        assert np.isnan(future)
        return {"SPY": 0.2}

    r = engine.run(engine.StrategySpec("s", fn=spy), FRAME, "2017-01-02", "2017-06-30", options=opts())
    assert seen and all(dec == last for dec, last in seen)
    assert [dec for dec, _ in r.schedule] == [d for d, _ in seen]


def test_orders_fill_at_the_next_open_with_costs():
    r = engine.run(engine.StrategySpec("aapl", fn=lambda p, d: {"AAPL": 0.2}, params={"stop_loss_pct": 0.08}),
                   FRAME, "2017-01-02", "2017-01-06", options=opts())
    assert len(r.schedule) == 1
    dec, ex = r.schedule[0]
    assert (dec, ex) == ("2016-12-30", "2017-01-02")       # decided Friday's close, filled Monday's open
    assert pd.Timestamp(ex).dayofweek == 0 or pd.Timestamp(ex).isocalendar()[1] != pd.Timestamp(dec).isocalendar()[1]
    t = r.trades[0]
    assert t["entry_day"] == ex
    assert t["entry_price"] == pytest.approx(FRAME["open"]["AAPL"].loc[ex] * (1 + BPS))


# ------------------------------------------------------------------ SPY check

def test_spy_buy_and_hold_matches_spy_within_costs():
    spec = engine.StrategySpec("spy", fn=strategies.spy_buy_and_hold)
    r = engine.run(spec, FRAME, "2017-01-02", "2019-12-31", options=opts(benchmark=True))
    first_exec = r.schedule[0][1]
    actual = FRAME["close"]["SPY"].iloc[-1] / FRAME["open"]["SPY"].loc[first_exec] - 1
    assert r.metrics["total_return_pct"] == pytest.approx(actual * 100, abs=0.5)
    assert r.metrics["spy_return_pct"] == pytest.approx(actual * 100, abs=1e-3)   # (rounded to 3 decimals)
    assert abs(r.metrics["vs_spy_pct"]) < 0.5           # only entry costs + commission


# ------------------------------------------------------------------ limits

def test_hard_limits_cap_filter_and_scale():
    from arena.learning import config as lconfig
    hl = lconfig.get().hard_limits
    w, notes = engine.limit_targets({"AAPL": 0.6, "MSFT": -0.25, "NVDA": -0.25, "PENNY": 0.1, "THIN": 0.1},
                                    {"AAPL": 100, "MSFT": 100, "NVDA": 100, "PENNY": 2.0, "THIN": 50},
                                    {"AAPL": 5e6, "MSFT": 5e6, "NVDA": 5e6, "PENNY": 5e6, "THIN": 10},
                                    {"position_max_pct": 0.2}, {"AAPL", "MSFT", "NVDA", "PENNY", "THIN"})
    assert set(w) == {"AAPL", "MSFT", "NVDA"}
    assert w["AAPL"] == pytest.approx(0.2)
    assert -sum(x for x in w.values() if x < 0) <= hl.max_short_exposure_pct + 1e-9
    assert sum(abs(x) for x in w.values()) <= hl.max_gross_exposure + 1e-9
    assert any("PENNY" in n for n in notes) and any("THIN" in n for n in notes)


def test_no_leverage_and_position_cap_hold_through_a_run():
    spec = engine.StrategySpec("all-in", fn=lambda p, d: {"AAPL": 0.5, "MSFT": 0.5})
    r = engine.run(spec, FRAME, "2017-01-02", "2017-12-29", options=opts())
    assert (r.equity > 0).all()
    assert any("capped" in n for n in r.notes)


def test_stops_exit_at_the_stop_or_the_gap_open():
    def crash(df):
        m = (df["ticker"] == "AAPL") & (df["date"] == "2017-01-11")
        df.loc[m, ["open", "high", "low", "close"]] = df.loc[m, "open"].values[0] * np.array([0.80, 0.81, 0.79, 0.80])
        return df
    f = make_frame(overrides=crash)
    r = engine.run(engine.StrategySpec("aapl", fn=lambda p, d: {"AAPL": 0.2}, params={"stop_loss_pct": 0.05}),
                   f, "2017-01-02", "2017-01-31", options=opts())
    stopped = [t for t in r.trades if t["exit_reason"] == "stop"]
    assert stopped and stopped[0]["exit_day"] == "2017-01-11"
    gap_open = f["open"]["AAPL"].loc["2017-01-11"]
    assert stopped[0]["exit_price"] == pytest.approx(gap_open * (1 - BPS))   # gapped through: the open
    # Stopped out: stays in cash until the next rebalance.
    assert all(t["entry_day"] >= "2017-01-16" for t in r.trades if t is not stopped[0] and t["entry_day"] > "2017-01-11")


def test_results_have_every_required_metric_and_states():
    labels = {d.strftime("%Y-%m-%d"): ("up_low" if i % 2 else "down_high") for i, d in enumerate(FRAME.index)}
    r = engine.run(engine.StrategySpec("momo", fn=strategies.gpt_six_month_momentum), FRAME, "2017-01-02",
                   "2018-12-31", labels, options=opts())
    m = r.metrics
    for k in ("total_return_pct", "annual_return_pct", "max_drawdown_pct", "risk_adjusted_score", "sharpe",
              "trades", "win_rate_pct", "spy_return_pct", "vs_spy_pct", "by_state", "data_note"):
        assert k in m
    assert set(m["by_state"]) == {"up_low", "down_high"}
    assert sum(s["trades"] for s in m["by_state"].values()) == m["trades"]
    assert "Survivorship" in m["data_note"]
    assert all(t["entry_state"] in ("up_low", "down_high") for t in r.trades)


# ------------------------------------------------------------------ same code path as a live code slot

CODE = """
def target_weights(prices, date):
    close = prices["close"]
    r = close.iloc[-1] / close.iloc[-64] - 1
    best = r.drop("SPY").sort_values(ascending=False).index[:2]
    return {t: 0.2 for t in best}
"""


def test_agent_code_runs_in_the_sandbox_and_matches_the_python_version():
    def same(prices, date):
        close = prices["close"]
        r = close.iloc[-1] / close.iloc[-64] - 1
        return {t: 0.2 for t in r.drop("SPY").sort_values(ascending=False).index[:2]}
    a = engine.run(engine.StrategySpec("code", code=CODE), FRAME, "2017-01-02", "2017-04-28", options=opts())
    b = engine.run(engine.StrategySpec("fn", fn=same), FRAME, "2017-01-02", "2017-04-28", options=opts())
    assert a.metrics["total_return_pct"] == pytest.approx(b.metrics["total_return_pct"])
    assert [t["symbol"] for t in a.trades] == [t["symbol"] for t in b.trades]


def test_reproduces_a_mechanical_live_strategy():
    """The engine's signal path gives exactly the rule's picks on each day."""
    for day in ("2017-06-30", "2018-03-29", "2019-11-29"):
        sig = engine.signals(engine.StrategySpec("g", fn=strategies.gpt_six_month_momentum), FRAME, [day], UNI)[day]
        close = FRAME["close"].loc[:day].drop(columns=["SPY"])
        r126 = close.iloc[-1] / close.iloc[-127] - 1
        ok = (close.iloc[-1] > close.iloc[-200:].mean()) & (r126 < 2.5)
        assert set(sig) == set(r126[ok].sort_values(ascending=False).index[:4])


# ------------------------------------------------------------------ walk-forward, holdout, run log

@pytest.fixture
def dbs():
    h = histdata.connect(":memory:")
    h.executemany("INSERT INTO hist_calendar VALUES (?,?,?)", [(d, "09:30", "16:00") for d in DAYS])
    long = FRAME.stack(level=1, future_stack=True).reset_index()
    long.columns = ["date", "ticker", *long.columns[2:]]
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")
    histdata._insert(h, long[BAR_COLUMNS])
    for t in TICKERS:
        histdata._refresh_symbol(h, t)
    c = connect(":memory:")
    states.label_all(c, h)
    yield c, h
    c.close()
    h.close()


def test_windows_never_reach_the_holdout():
    locked = walkforward.holdout_start("2019-12-31", days=DAYS)
    assert locked == "2019-01-01"           # (the test calendar has no holidays)
    ws = walkforward.windows("2016-01-04", locked, 12, 3, 3)
    assert ws and all(w.test_end < locked for w in ws)
    assert all(w.test_start > w.design_end for w in ws)
    with pytest.raises(walkforward.HoldoutError):
        walkforward.check_period("2019-02-01", "design", locked)


def test_runs_are_logged_counted_and_the_holdout_is_used_once(dbs):
    conn, hconn = dbs
    spec = engine.StrategySpec("momo", fn=strategies.gpt_six_month_momentum)
    o = opts()
    r1 = runs.execute(conn, hconn, spec=spec, purpose="design", idea_key="momo", requested_by="human",
                      start="2017-01-02", end="2018-06-29", options=o)
    assert r1["ok"] and r1["attempts"] == 1 and "Survivorship" in r1["data_note"]
    # Design runs can't touch the locked last 12 months.
    r2 = runs.execute(conn, hconn, spec=spec, purpose="design", idea_key="momo", requested_by="human",
                      start="2017-01-02", end="2019-06-28", options=o)
    assert not r2["ok"] and "holdout" in r2["refused"] and r2["attempts"] == 2
    wf = runs.execute(conn, hconn, make=lambda p: engine.StrategySpec("momo", fn=strategies.gpt_six_month_momentum,
                                                                      params=p),
                      grid=[{"stop_loss_pct": 0.05}, {"stop_loss_pct": 0.08}], purpose="walk_forward",
                      idea_key="momo", requested_by="human", options=o)
    assert wf["ok"] and wf["attempts"] == 3 and wf["windows"] and wf["grid_size"] == 2
    assert all(w["test"][1] < wf["holdout_from"] for w in wf["windows"])
    h1 = runs.execute(conn, hconn, spec=spec, purpose="holdout", idea_key="momo", requested_by="human", options=o)
    assert h1["ok"] and h1["start"] >= h1["holdout_from"]
    h2 = runs.execute(conn, hconn, spec=spec, purpose="holdout", idea_key="momo", requested_by="human", options=o)
    assert not h2["ok"] and "already used" in h2["refused"]
    rows = conn.execute("SELECT attempt_no, purpose, status FROM backtest_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "design", "ok"), (2, "design", "refused"), (3, "walk_forward", "ok"),
                                        (4, "holdout", "ok"), (5, "holdout", "refused")]
    stored = json.loads(conn.execute("SELECT results FROM backtest_runs WHERE id=1").fetchone()[0])
    assert stored["attempts"] == 1 and stored["by_state"]
    n = conn.execute("SELECT COUNT(*) FROM backtest_trades WHERE run_id=1").fetchone()[0]
    assert n == stored["trades"] > 0
    assert conn.execute("SELECT COUNT(*) FROM strategy_by_market_state WHERE source LIKE 'backtest:%'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM backtest_runs WHERE id=2")


def test_benchmark_mode_is_only_for_checks(dbs):
    conn, hconn = dbs
    spec = engine.StrategySpec("spy", fn=strategies.spy_buy_and_hold)
    with pytest.raises(ValueError):
        runs.execute(conn, hconn, spec=spec, purpose="design", idea_key="spy", requested_by="human",
                     options=opts(benchmark=True))
    r = runs.execute(conn, hconn, spec=spec, purpose="check", idea_key="spy-check", requested_by="engine",
                     options=opts(benchmark=True))
    assert r["ok"] and r["end"] == DAYS[-1]
