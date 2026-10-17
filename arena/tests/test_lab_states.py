"""Backtest lab, phase 2: SPY-only market-state labels."""
from __future__ import annotations

import json
import math

import pytest

from arena.db import connect
from arena.lab import config as labconfig, histdata, schema, states
from arena.learning.regime import MARKET_STATES, classify_spy
from arena.prices import BAR_COLUMNS
from arena.tests.conftest import weekdays

import pandas as pd

CFG = labconfig.get().market_state
DAYS = weekdays("2016-01-04", "2017-06-30")


def spy_path(n: int) -> list[float]:
    """Calm rise for 260 days, then a volatile fall."""
    out, px = [], 100.0
    for i in range(n):
        if i < 260:
            px *= 1.0008 + (0.002 if i % 2 else -0.002)          # ~3% vol, up
        else:
            px *= 0.995 + (0.03 if i % 2 else -0.03)             # ~48% vol, down
        out.append(px)
    return out


def test_classify_spy_rules():
    assert classify_spy([100.0] * 50, CFG)[0] is None             # no 200-day average yet
    up = [100 * 1.001 ** i for i in range(250)]
    label, inputs = classify_spy(up, CFG)
    assert label == "up_low" and inputs["trend"] == "up" and inputs["realized_vol"] < CFG.vol_low_below
    path = spy_path(400)
    label, inputs = classify_spy(path, CFG)
    assert label == "down_high" and inputs["realized_vol"] >= CFG.vol_high_at
    assert set(MARKET_STATES) == {f"{t}_{v}" for t in ("up", "down") for v in ("low", "normal", "high")}


@pytest.fixture
def dbs():
    h = histdata.connect(":memory:")
    h.executemany("INSERT INTO hist_calendar VALUES (?,?,?)", [(d, "09:30", "16:00") for d in DAYS])
    px = spy_path(len(DAYS))
    df = pd.DataFrame([("SPY", d, p, p, p, p, 1e8) for d, p in zip(DAYS, px)], columns=BAR_COLUMNS)
    histdata._insert(h, df)
    histdata._refresh_symbol(h, "SPY")
    c = connect(":memory:")
    yield c, h
    c.close()
    h.close()


def test_label_all_uses_the_one_function_for_every_day(dbs):
    conn, hconn = dbs
    rep = states.label_all(conn, hconn)
    assert rep["days"] == len(DAYS)
    assert rep["first_label"] == DAYS[CFG.trend_sma_days - 1]     # first day with a 200-day average
    closes = [c for _, c in histdata.closes(hconn, "SPY")]
    stored = states.labels(conn)
    for i in (199, 230, 300, len(DAYS) - 1):
        assert stored[DAYS[i]] == classify_spy(closes[: i + 1], CFG)[0]
    assert stored[DAYS[-1]] == "down_high"
    row = conn.execute("SELECT inputs FROM market_states WHERE day=?", (DAYS[-1],)).fetchone()
    assert math.isclose(json.loads(row[0])["spy_close"], closes[-1], rel_tol=1e-6)


def test_coverage_counts_history_and_live_days(dbs):
    conn, hconn = dbs
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (1, '2017-03-03', '2017-03-06', 'scored')")
    states.label_all(conn, hconn)
    cov = states.coverage(conn, hconn, DAYS[-1])
    assert cov["ok"] and cov["live_days"] > 0 and cov["unlabelled_warmup_days"] == CFG.trend_sma_days - 1
    # A live day without a bar (the nightly job didn't run) is reported.
    conn.execute("DELETE FROM market_states WHERE day=?", (DAYS[-3],))
    cov = states.coverage(conn, hconn, DAYS[-1])
    assert not cov["ok"] and cov["live_missing"] == [DAYS[-3]]


def test_live_trades_get_the_label_from_the_close_before(dbs):
    conn, hconn = dbs
    states.label_all(conn, hconn)
    d = DAYS[-1]
    conn.execute("INSERT INTO trades(seq, agent_id, slot, date, ticker, shares, ref_price, fill_price, value, "
                 "commission, slippage_cost, realized_pnl, reason) VALUES (1,'gpt',1,?,'AAPL',1,1,1,1,0,0,0,'rebalance')",
                 (d,))
    row = conn.execute("SELECT market_state FROM trade_market_states").fetchone()
    assert row[0] == states.labels(conn)[DAYS[-2]]


def test_strategy_by_market_state_view_has_live_and_backtest_rows(dbs):
    conn, _ = dbs
    schema.migrate(conn)
    conn.execute("INSERT INTO backtest_runs(requested_by, idea_key, attempt_no, strategy_name, purpose, status, "
                 "created_at) VALUES ('human','x',1,'momo','walk_forward','running','now')")
    conn.execute("INSERT INTO backtest_trades VALUES (1,'AAPL','long','2017-01-03',100,'2017-01-10',110,1,10,10,"
                 "'rebalance','up_low')")
    conn.execute("UPDATE backtest_runs SET status='ok' WHERE id=1")
    rows = [dict(r) for r in conn.execute("SELECT * FROM strategy_by_market_state")]
    assert rows == [{"source": "backtest:walk_forward", "agent": "human", "strategy": "momo",
                     "market_state": "up_low", "trades": 1, "avg_pct": 10.0, "win_rate_pct": 100.0}]
    # Finished runs are frozen and never deleted (attempt counts can't be cleaned up).
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE backtest_runs SET results='{}' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM backtest_runs")
