import pytest

from arena import costs, ledger
from arena.ledger import SlotTarget

from .conftest import DAYS, flat_prices, set_price

D0, D1, D2, D3 = DAYS[0], DAYS[1], DAYS[2], DAYS[3]


def total_costs(conn, agent):
    return conn.execute("SELECT SUM(commission) + SUM(slippage_cost) FROM trades WHERE agent_id=?",
                        (agent,)).fetchone()[0] or 0.0


def test_new_agent_has_starting_cash(conn, agent):
    flat_prices(conn, "SPY", 100)
    v = ledger.value_agent(conn, agent, D0)
    assert v.equity == pytest.approx(1000)
    assert v.cash == pytest.approx(1000)


def test_long_profits_when_price_rises(conn, agent):
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    set_price(conn, "AAPL", D2, 110)
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.5, {"AAPL": 1.0})])
    after_trade = ledger.value_agent(conn, agent, D1).equity
    later = ledger.value_agent(conn, agent, D2).equity
    # ~5 shares * $10 gain
    assert later - after_trade == pytest.approx(5 * 10, rel=0.01)


def test_short_profits_when_price_falls_and_loses_when_it_rises(conn, agent):
    flat_prices(conn, "XOM", 100, DAYS[:2])
    set_price(conn, "XOM", D2, 90)
    set_price(conn, "XOM", D3, 120)
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.1, {"XOM": -1.0})])
    shares = ledger.position_shares(conn, agent, 1, "XOM")
    assert shares < 0
    # Short proceeds land in the slot's cash
    assert ledger.slot_cash(conn, agent)[1] > 100 + 99
    e1 = ledger.value_agent(conn, agent, D1).equity
    assert ledger.value_agent(conn, agent, D2).equity - e1 == pytest.approx(-shares * 10, rel=1e-9)
    assert ledger.value_agent(conn, agent, D3).equity - e1 == pytest.approx(shares * 20, rel=1e-9)


def test_trade_costs_reduce_equity_exactly(conn, agent):
    flat_prices(conn, "MSFT", 200, DAYS[:2])
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 1.0, {"MSFT": 0.5})])
    v = ledger.value_agent(conn, agent, D1, "open")
    assert v.equity == pytest.approx(1000 - total_costs(conn, agent), abs=1e-9)


def test_flip_long_to_short_in_one_rebalance(conn, agent):
    flat_prices(conn, "NVDA", 100, DAYS[:3])
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.2, {"NVDA": 1.0})])
    assert ledger.position_shares(conn, agent, 1, "NVDA") == pytest.approx(2.0, rel=1e-2)   # minus costs
    res = ledger.rebalance(conn, agent, D2, [SlotTarget(1, 0.1, {"NVDA": -1.0})])
    shares = ledger.position_shares(conn, agent, 1, "NVDA")
    assert shares == pytest.approx(-1.0, rel=1e-2)
    flips = [t for t in res.trades if t.ticker == "NVDA"]
    assert len(flips) == 1 and flips[0].shares < -2.9
    row = conn.execute("SELECT avg_price FROM positions WHERE ticker='NVDA'").fetchone()
    assert row["avg_price"] == pytest.approx(costs.fill_price(100, -1))  # new short's entry


def test_cash_freeing_trades_execute_first(conn, agent):
    flat_prices(conn, "AAPL", 100, DAYS[:3])
    flat_prices(conn, "MSFT", 100, DAYS[:3])
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.9, {"AAPL": 0.25 / 0.9})])
    res = ledger.rebalance(conn, agent, D2, [SlotTarget(1, 0.9, {"MSFT": 0.25 / 0.9})])
    assert [t.ticker for t in res.trades] == ["AAPL", "MSFT"]
    assert res.trades[0].shares < 0 < res.trades[1].shares


def test_slots_are_separate_sleeves(conn, agent):
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    flat_prices(conn, "XOM", 50, DAYS[:2])
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.3, {"AAPL": 0.5}),
                                       SlotTarget(2, 0.2, {"XOM": -0.5})])
    v = ledger.value_agent(conn, agent, D1, "open")
    assert v.slot_equity[1] == pytest.approx(300, abs=1)
    assert v.slot_equity[2] == pytest.approx(200, abs=1)
    assert v.slot_equity[0] == pytest.approx(500, abs=1e-9)   # reserve untouched by costs
    # Removing slot 2 closes its short and returns money to the reserve
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.3, {"AAPL": 0.5})])
    assert ledger.position_shares(conn, agent, 2, "XOM") == 0


def test_tiny_trades_are_skipped(conn, agent):
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    res = ledger.rebalance(conn, agent, D1, [SlotTarget(1, 1.0, {"AAPL": 0.0005})])  # $0.50
    assert res.trades == []


def test_deposits_are_not_returns(conn, agent):
    flat_prices(conn, "SPY", 100, DAYS[:3])
    set_price(conn, "SPY", D2, 110)
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 1.0, {"SPY": 1.0})])
    start = ledger.snapshot(conn, agent, D1, "post_trade", "open")
    start_id = ledger.latest_snapshot(conn, agent, "post_trade")["id"]
    ledger.deposit(conn, agent, 1000.0, D2, "bonus")
    ledger.snapshot(conn, agent, D2, "mark", "open")
    end_id = ledger.latest_snapshot(conn, agent, "mark")["id"]
    r = ledger.period_return(conn, agent, start_id, end_id)
    # Price +10% on ~100% invested; the $1,000 deposit must not show up.
    assert r == pytest.approx(0.10 * (start.long_value / start.equity), rel=1e-6)
    assert r < 0.11


def test_split_rescales_shares_and_keeps_value(conn, agent):
    flat_prices(conn, "NVDA", 100, DAYS[:2])
    ledger.rebalance(conn, agent, D1, [SlotTarget(1, 0.2, {"NVDA": 1.0})],)
    ledger.set_risk_orders(conn, agent, 1, {"NVDA": {"stop_loss": 80.0}})
    before = ledger.value_agent(conn, agent, D1).equity
    shares = ledger.position_shares(conn, agent, 1, "NVDA")
    # 10-for-1 split: history becomes 1/10th
    flat_prices(conn, "NVDA", 10, DAYS[:2])
    ledger.apply_price_adjustment(conn, "NVDA", 0.1, D1)
    assert ledger.position_shares(conn, agent, 1, "NVDA") == pytest.approx(shares * 10)
    assert ledger.value_agent(conn, agent, D1).equity == pytest.approx(before)
    stop = conn.execute("SELECT stop_loss FROM risk_orders").fetchone()[0]
    assert stop == pytest.approx(8.0)
