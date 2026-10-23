"""Hand calculations, lab parity and cash-flow/closed-episode regressions."""
import json
import math

import pandas as pd
import pytest

from arena import stats
from arena.api.queries import live_stats
from arena.db import connect
from arena.lab.engine import metrics as lab_metrics


def test_hand_calculated_metrics():
    # Daily returns +10%, -10%, +10%: mean=1/30, sample variance=1/75.
    result = stats.metrics([100, 110, 99, 108.9], [10, 20, -5, -15, 0])
    assert result == {
        "total_return_pct": 8.9, "max_drawdown_pct": 10.0,
        "sharpe": round(math.sqrt(21), 3),
        "sortino": round(math.sqrt(84), 3),
        "trades": 5, "win_rate_pct": 40.0, "avg_win": 15, "avg_loss": -10,
    }


def test_metrics_match_lab():
    days = pd.date_range("2024-01-02", periods=3)
    eq = pd.Series([110, 99, 108.9], index=days)
    trades = [{"pnl": p, "entry_state": None, "pnl_pct": p} for p in [10, -5, 0]]
    lab = lab_metrics(eq, trades, 6, 100, ["2024-01-02"], {},
                      [d.strftime("%Y-%m-%d") for d in days], None)
    live = stats.metrics([100, *eq], [10, -5, 0])
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "trades", "win_rate_pct"):
        assert live[key] == lab[key]


@pytest.mark.parametrize("equity", [[], [100], [100, 100, 100], [0, 0]])
def test_empty_short_flat_and_zero_paths(equity):
    result = stats.metrics(equity)
    assert result["sharpe"] == result["sortino"] == 0
    assert result["win_rate_pct"] is result["avg_win"] is result["avg_loss"] is None
    assert result["total_return_pct"] == (0 if equity and equity[0] else None)


def test_one_sided_returns_and_bankruptcy():
    assert stats.metrics([100, 110, 121], [5])["sortino"] == 0
    result = stats.metrics([100, 90, 0], [-5])
    assert result["max_drawdown_pct"] == 100
    assert result["total_return_pct"] == -100
    assert result["sortino"] < 0 and result["avg_win"] is None


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_nonfinite_input(bad):
    with pytest.raises(ValueError):
        stats.metrics([100, bad])
    with pytest.raises(ValueError):
        stats.metrics([100], [bad])


def test_calendar_windows_use_previous_close_and_trade_exit_date():
    # Month starts Monday; the prior Friday is needed for both baselines.
    eq = [("2024-04-01", 120), ("2024-03-28", 100), ("2024-04-02", 132),
          ("2024-04-01", 110), ("2024-04-08", 999)]
    trades = [("2024-03-28", -10), ("2024-04-01", 20), ("2024-04-08", -100)]
    windows = stats.window_stats(eq, trades, as_of="2024-04-02")
    for name in ("weekly", "monthly"):
        assert windows[name]["start"] == "2024-04-01"
        assert windows[name]["total_return_pct"] == 32
        assert windows[name]["trades"] == 1
        assert [p["equity"] for p in windows[name]["equity_curve"]] == [110, 132]
    assert windows["all_time"]["trades"] == 2
    assert windows["all_time"]["total_return_pct"] == 32
    assert stats.window_stats([], as_of="2024-04-02")["weekly"]["total_return_pct"] is None
    assert stats.window_stats([])["all_time"]["end"] is None
    assert stats.window_stats([("2024-04-01", 110)], initial_equity=100)["all_time"]["total_return_pct"] == 10


def test_live_cash_flows_reallocations_and_closed_episodes(tmp_path):
    conn = connect(tmp_path / "stats.db")
    conn.execute("INSERT INTO agents VALUES ('test', NULL, '2024-03-28')")
    for slot in (0, 1, 2):
        conn.execute("INSERT INTO slots(agent_id, slot, name) VALUES ('test', ?, ?)", (slot, f"s{slot}"))
    conn.execute("INSERT INTO deposits(seq, agent_id, date, amount) VALUES (1, 'test', '2024-03-28', 100)")
    conn.execute("INSERT INTO prices VALUES ('SPY','2024-03-28',10,11,10,11,1000), "
                 "('SPY','2024-04-01',11,11,11,11,1000)")

    def snap(seq, day, kind, equity, slots):
        conn.execute("INSERT INTO snapshots(seq,agent_id,date,kind,price_field,equity,cash,long_value,short_value,slots_json) "
                     "VALUES (?,'test',?,?,?,?,0,0,0,?)",
                     (seq, day, kind, 'close' if kind == 'daily' else 'open', equity, json.dumps(slots)))

    snap(2, "2024-03-28", "mark", 100, {"0": 100})
    snap(3, "2024-03-28", "post_trade", 100, {"0": 50, "1": 50})
    snap(5, "2024-03-28", "daily", 110, {"0": 50, "1": 60})
    snap(6, "2024-04-01", "mark", 110, {"0": 50, "1": 60})
    conn.execute("INSERT INTO deposits(seq,agent_id,date,amount) VALUES (7,'test','2024-04-01',100)")
    snap(10, "2024-04-01", "post_trade", 208, {"0": 10, "1": 198})
    snap(15, "2024-04-01", "daily", 208, {"0": 10, "1": 198})
    for day in ("2024-03-28", "2024-04-01"):
        conn.execute("INSERT INTO pending_orders(agent_id,created_date,exec_date,kind,status) "
                     "VALUES ('test',?,?,'rebalance','done')", (day, day))

    def fill(seq, day, delta, pnl, fee=0, slot=1):
        price = 10 if day == "2024-03-28" else 11
        conn.execute("INSERT INTO trades(seq,agent_id,slot,date,ticker,shares,ref_price,fill_price,value,commission,"
                     "slippage_cost,realized_pnl,reason) VALUES (?,'test',?,?,'SPY',?,?,?,?,?,0,?,'rebalance')",
                     (seq, slot, day, delta, price, price, abs(delta) * price, fee, pnl))

    fill(4, "2024-03-28", 10, 0)
    fill(8, "2024-04-01", -5, 10, 1)  # Partial exit isn't a closed episode.
    fill(9, "2024-04-01", -10, -5, 1)  # Flip closes long and opens short.
    fill(11, "2024-04-01", 5, 0)  # Breakeven short, less its opening fee.
    fill(13, "2024-04-01", 10, 0, slot=2)  # Open position is excluded.
    # Later intraday data must not sneak into the daily metrics.
    fill(16, "2024-04-01", -10, 1000, slot=2)
    fill(17, "2024-04-02", 10, 0, slot=2)
    conn.execute("PRAGMA query_only=ON")
    result = live_stats(conn)
    agent = result["agents"][0]
    assert result["as_of"] == "2024-04-01"
    assert agent["windows"]["all_time"]["total_return_pct"] == 8
    first = agent["slots"][0]["windows"]
    assert first["all_time"]["total_return_pct"] == 18.8  # 1.2 * .99 - 1
    assert first["weekly"]["total_return_pct"] == -1
    assert first["weekly"]["trades"] == 2
    assert first["weekly"]["win_rate_pct"] == 50
    assert first["weekly"]["avg_win"] == 3.5
    assert first["weekly"]["avg_loss"] == -0.5
    assert agent["slots"][1]["windows"]["all_time"]["trades"] == 0
    assert agent["slots"][1]["windows"]["all_time"]["total_return_pct"] is None
    conn.close()


def test_live_empty_league(tmp_path):
    conn = connect(tmp_path / "empty.db")
    assert live_stats(conn) == {"as_of": None, "equity_curve_unit": "growth_index", "agents": []}
    conn.execute("INSERT INTO agents VALUES ('test', NULL, '2024-01-01')")
    assert live_stats(conn)["agents"][0]["windows"]["all_time"]["total_return_pct"] is None
    conn.close()


def test_weekly_and_monthly_windows_differ_across_year_boundary():
    eq = [("2023-12-29", 100), ("2024-01-01", 110), ("2024-01-05", 121),
          ("2024-01-08", 133.1)]
    windows = stats.window_stats(eq)
    assert windows["weekly"]["total_return_pct"] == 10
    assert windows["monthly"]["total_return_pct"] == 33.1


def test_closed_short_includes_borrow_commissions_and_split(tmp_path):
    conn = connect(tmp_path / "short.db")
    conn.execute("INSERT INTO agents VALUES ('test', NULL, '2024-01-01')")
    conn.execute("INSERT INTO slots(agent_id, slot, name) VALUES ('test', 1, 'short')")
    for seq, shares, pnl, fee in [(1, -10, 0, 2), (4, 10, 10, 1), (5, 10, -5, 1)]:
        conn.execute("INSERT INTO trades(seq,agent_id,slot,date,ticker,shares,ref_price,fill_price,value,commission,"
                     "slippage_cost,realized_pnl,reason) VALUES (?,'test',1,'2024-01-01','SPY',?,10,10,100,?,0,?,'rebalance')",
                     (seq, shares, fee, pnl))
    conn.execute("INSERT INTO corporate_actions(seq,date,ticker,factor) VALUES (2,'2024-01-01','SPY',0.5)")
    conn.execute("INSERT INTO borrow_fees(seq,agent_id,slot,date,ticker,short_value,days,amount) "
                 "VALUES (3,'test',1,'2024-01-01','SPY',100,1,3)")
    conn.execute("INSERT INTO snapshots(seq,agent_id,date,kind,price_field,equity,cash,long_value,short_value,slots_json) "
                 "VALUES (6,'test','2024-01-01','daily','close',100,100,0,0,'{}')")
    result = live_stats(conn)["agents"][0]
    # 10 - 5 realized, less 2+1+1 commissions and 3 borrow = -2.
    assert result["windows"]["all_time"]["trades"] == 1
    assert result["windows"]["all_time"]["avg_loss"] == -2
    assert result["slots"][0]["windows"]["all_time"]["avg_loss"] == -2
    conn.close()


def test_slot_transfer_without_post_trade_snapshot_is_not_profit(tmp_path):
    conn = connect(tmp_path / "tilt.db")
    conn.execute("INSERT INTO agents VALUES ('test', NULL, '2024-01-01')")
    for slot in (1, 2):
        conn.execute("INSERT INTO slots(agent_id, slot) VALUES ('test', ?)", (slot,))
    conn.execute("INSERT INTO deposits(seq,agent_id,date,amount) VALUES (1,'test','2024-01-01',200)")
    for seq, day, slots in [(2, "2024-01-01", {"1": 100, "2": 100}),
                            (3, "2024-01-02", {"1": 150, "2": 50}),
                            (4, "2024-01-03", {"1": 200, "2": 0})]:
        conn.execute("INSERT INTO snapshots(seq,agent_id,date,kind,price_field,equity,cash,long_value,short_value,slots_json) "
                     "VALUES (?,'test',?,'daily','close',200,200,0,0,?)", (seq, day, json.dumps(slots)))
    for slot in live_stats(conn)["agents"][0]["slots"]:
        assert slot["windows"]["all_time"]["total_return_pct"] == 0
        assert slot["windows"]["all_time"]["max_drawdown_pct"] == 0
    conn.close()
