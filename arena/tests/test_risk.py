import math

import pytest

from arena import ledger, risk
from arena.ledger import SlotTarget

from .conftest import DAYS, flat_prices, set_price

# ---------------------------------------------------------------- validation


def errors_for(slots, prices=None):
    res = risk.validate_portfolio(slots, prices)
    assert not res.ok
    return " | ".join(res.errors)


def test_valid_portfolio_passes():
    res = risk.validate_portfolio([
        SlotTarget(1, 0.5, {"AAPL": 0.4, "MSFT": 0.4, "SPY": 0.2}),
        SlotTarget(2, 0.3, {"XOM": -0.3, "TLT": 0.5}),
    ])
    assert res.ok, res.errors
    assert res.exposures["short"] == pytest.approx(0.09)


def test_rejects_too_much_in_one_ticker_across_slots():
    msg = errors_for([SlotTarget(1, 0.5, {"NVDA": 0.3}), SlotTarget(2, 0.5, {"NVDA": 0.3})])
    assert "NVDA: net long 30.0%" in msg


def test_rejects_single_short_over_15pct():
    assert "net short 20.0%" in errors_for([SlotTarget(1, 0.2, {"TSLA": -1.0})])


def test_rejects_total_short_over_50pct():
    w = {t: -0.25 for t in ["AAPL", "MSFT", "NVDA", "AMZN"]}
    msg = errors_for([SlotTarget(1, 0.55, w)])
    assert "Total short exposure 55.0%" in msg


def test_rejects_leverage_and_bad_structure():
    assert "Total allocation" in errors_for([SlotTarget(1, 0.7, {}), SlotTarget(2, 0.5, {})])
    assert "sum of |weights|" in errors_for([SlotTarget(1, 0.5, {"AAPL": 0.8, "MSFT": 0.8})])
    assert "maximum is 5" in errors_for([SlotTarget(i, 0.1, {}) for i in range(1, 7)])
    assert "not in the tradable universe" in errors_for([SlotTarget(1, 0.1, {"DOGE": 1.0})])
    assert "not valid numbers" in errors_for([SlotTarget(1, 0.1, {"AAPL": math.nan})])
    assert "Duplicate slot" in errors_for([SlotTarget(1, 0.1, {}), SlotTarget(1, 0.1, {})])


def test_rejects_nonsense_risk_orders():
    msg = errors_for([SlotTarget(1, 0.2, {"AAPL": 1.0},
                                 risk_orders={"AAPL": {"stop_loss": 210.0}, "MSFT": {"stop_loss": 1.0}})],
                     prices={"AAPL": 200.0})
    assert "wrong side" in msg and "no position in MSFT" in msg


def test_error_message_includes_exposures():
    res = risk.validate_portfolio([SlotTarget(1, 0.2, {"TSLA": -1.0})])
    assert "gross 20.0%" in res.message() and "short 20.0%" in res.message()


# ---------------------------------------------------------------- daily processing


def day(conn, agent, d):
    """One trading day as the live league runs it: orders due at the open
    fill in the morning job, then the end-of-day processing."""
    risk.execute_pending(conn, agent, d)
    return risk.process_daily(conn, agent, d)



def test_borrow_fee_charged_daily_including_weekends(conn, agent):
    flat_prices(conn, "XOM", 100)
    # D = Thu 2024-01-04 open short; Thu close: 1 day; Fri: 1 day; Mon: 3 days.
    ledger.rebalance(conn, agent, "2024-01-04", [SlotTarget(1, 0.1, {"XOM": -1.0})])
    shares = -ledger.position_shares(conn, agent, 1, "XOM")
    for d in ["2024-01-04", "2024-01-05", "2024-01-08"]:
        day(conn, agent, d)
    rows = conn.execute("SELECT days, amount FROM borrow_fees ORDER BY seq").fetchall()
    assert [r["days"] for r in rows] == [1, 1, 3]
    assert sum(r["amount"] for r in rows) == pytest.approx(shares * 100 * 0.005 * 5 / 365)


def test_no_borrow_fee_on_longs(conn, agent):
    flat_prices(conn, "AAPL", 100)
    ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.5, {"AAPL": 1.0})])
    day(conn, agent, DAYS[1])
    assert conn.execute("SELECT COUNT(*) FROM borrow_fees").fetchone()[0] == 0


def test_stop_loss_triggers_on_close_and_fills_next_open(conn, agent):
    d1, d2, d3 = DAYS[1], DAYS[2], DAYS[3]
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    set_price(conn, "AAPL", d2, 98, 89)      # closes below the stop
    set_price(conn, "AAPL", d3, 87, 95)      # gaps lower at the next open
    ledger.rebalance(conn, agent, d1, [SlotTarget(1, 0.5, {"AAPL": 1.0},
                                                  risk_orders={"AAPL": {"stop_loss": 90.0}})])
    day(conn, agent, d1)
    assert ledger.position_shares(conn, agent, 1, "AAPL") > 0
    rep = day(conn, agent, d2)
    assert rep.new_orders and "stop_loss" in rep.new_orders[0]
    assert ledger.position_shares(conn, agent, 1, "AAPL") > 0   # not sold at the close
    rep = day(conn, agent, d3)
    assert ledger.position_shares(conn, agent, 1, "AAPL") == 0
    tr = conn.execute("SELECT * FROM trades WHERE reason='stop_loss'").fetchone()
    assert tr["ref_price"] == 87   # filled at the next open, not at the stop level


def test_take_profit_on_short(conn, agent):
    flat_prices(conn, "XOM", 100, DAYS[:2])
    set_price(conn, "XOM", DAYS[2], 85, 79)
    set_price(conn, "XOM", DAYS[3], 80)
    ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.1, {"XOM": -1.0},
                                                       risk_orders={"XOM": {"take_profit": 80.0}})])
    for d in DAYS[1:4]:
        day(conn, agent, d)
    assert ledger.position_shares(conn, agent, 1, "XOM") == 0
    tr = conn.execute("SELECT * FROM trades WHERE reason='take_profit'").fetchone()
    assert tr["shares"] > 0 and tr["realized_pnl"] > 0


def test_forced_cover_when_short_balloons(conn, agent):
    flat_prices(conn, "TSLA", 100, DAYS[:2])
    set_price(conn, "TSLA", DAYS[2], 120, 250)   # short explodes
    set_price(conn, "TSLA", DAYS[3], 250)
    ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.15, {"TSLA": -1.0})])
    day(conn, agent, DAYS[1])
    rep = day(conn, agent, DAYS[2])
    assert any("forced_cover" in o for o in rep.new_orders)
    day(conn, agent, DAYS[3])
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE reason='forced_cover'").fetchone()[0] == 1
    v = ledger.value_agent(conn, agent, DAYS[3], "open")
    short_pct = -v.ticker_value["TSLA"] / v.equity
    assert short_pct == pytest.approx(0.15, abs=0.005)   # brought back to the hard limit


def test_forced_cover_on_gross_exposure():
    val = ledger.Valuation("d", "close", equity=100, cash=0, long_value=0, short_value=0,
                           slot_equity={}, ticker_value={"AAPL": 90, "XOM": -19, "CVX": -18},
                           prices={})
    # gross 127% > 120% -> cover largest shorts until gross <= 100%
    covers = risk.plan_forced_covers(val)
    assert sum(covers.values()) == pytest.approx(27)
    assert covers["XOM"] == pytest.approx(19)


def test_no_forced_cover_within_limits():
    val = ledger.Valuation("d", "close", 100, 0, 0, 0, {}, {"AAPL": 80, "XOM": -19}, {})
    assert risk.plan_forced_covers(val) == {}


def test_daily_is_idempotent(conn, agent):
    flat_prices(conn, "XOM", 100)
    ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.1, {"XOM": -1.0})])
    day(conn, agent, DAYS[1])
    assert day(conn, agent, DAYS[1]).skipped
    assert conn.execute("SELECT COUNT(*) FROM borrow_fees").fetchone()[0] == 1


def test_failed_rebalance_leaves_no_trace(conn, agent):
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    with pytest.raises(RuntimeError):
        # Holding a ticker with no price at all makes valuation fail mid-way.
        conn.execute("INSERT INTO positions(agent_id, slot, ticker, shares, avg_price) "
                     "VALUES (?, 1, 'NOPRICE', 1, 1)", (agent,))
        ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.5, {"AAPL": 1.0})])
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == before


def test_end_of_day_never_fills_at_the_mornings_open(conn, agent):
    # The morning job didn't run; the stop must wait for the NEXT open,
    # not fill after the close at a price from before today's known close.
    flat_prices(conn, "AAPL", 100, DAYS[:2])
    set_price(conn, "AAPL", DAYS[2], 98, 89)
    set_price(conn, "AAPL", DAYS[3], 87, 70)
    set_price(conn, "AAPL", DAYS[4], 72, 72)
    ledger.rebalance(conn, agent, DAYS[1], [SlotTarget(1, 0.5, {"AAPL": 1.0},
                                                       risk_orders={"AAPL": {"stop_loss": 90.0}})])
    risk.process_daily(conn, agent, DAYS[2])                      # stop triggers for DAYS[3]
    risk.process_daily(conn, agent, DAYS[3])                      # morning run of DAYS[3] missed
    assert ledger.position_shares(conn, agent, 1, "AAPL") > 0
    assert conn.execute("SELECT exec_date FROM pending_orders").fetchone()[0] == DAYS[4]
    risk.execute_pending(conn, agent, DAYS[4])
    assert conn.execute("SELECT ref_price FROM trades WHERE reason='stop_loss'").fetchone()[0] == 72
