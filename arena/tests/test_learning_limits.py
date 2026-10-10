"""Limits enforced in code: every hard limit, param ranges, cooldowns,
rewrite caps/triggers, emergency-only rewrites, regime-confirmed tilts."""
from __future__ import annotations

import json

import pytest

from arena import ledger
from arena.learning import hardlimits, journal, lessons, review, strategies as strat
from arena.learning.execution import order_check
from arena.ledger import SlotTarget

from .learning_helpers import EXEC, league_week1, prices, set_price, stops

D = "2024-01-10"
AS_OF = "2024-01-10T21:45:00+00:00"


@pytest.fixture
def league(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    s = league_week1(conn, through="2024-01-12")
    return conn, s


def target(weights, slot=1, alloc=0.5, with_stops=True):
    return SlotTarget(slot, alloc, weights, "momentum", "manual", risk_orders=stops(weights) if with_stops else {})


# --------------------------------------------------------------------------
# Hard limits: decision time
# --------------------------------------------------------------------------

def test_oversize_position(league):
    conn, _ = league
    errs = hardlimits.validate_slots(conn, [target({"AAPL": 0.25, "MSFT": 0.2})], D)
    assert any("AAPL: 25% of the strategy" in e for e in errs)
    assert not hardlimits.validate_slots(conn, [target({"AAPL": 0.2, "MSFT": 0.2})], D)


def test_leverage(league):
    conn, _ = league
    w = {"AAPL": 0.2, "MSFT": 0.2, "XOM": 0.2, "JPM": 0.2, "KO": 0.2, "PG": 0.2}
    errs = hardlimits.validate_slots(conn, [target(w)], D)
    assert any("gross exposure 120%" in e for e in errs)


def test_short_cap(league):
    conn, _ = league
    errs = hardlimits.validate_slots(conn, [target({"AAPL": -0.2, "MSFT": -0.15, "XOM": 0.2})], D)
    assert any("shorts are 35%" in e for e in errs)
    assert not hardlimits.validate_slots(conn, [target({"AAPL": -0.15, "MSFT": -0.15, "XOM": 0.2})], D)


def test_missing_stop(league):
    conn, _ = league
    errs = hardlimits.validate_slots(conn, [target({"AAPL": 0.2}, with_stops=False)], D)
    assert any("no stop_loss" in e for e in errs)


@pytest.mark.parametrize("ticker", ["TQQQ", "SQQQ", "BTCUSD", "BTC/USD", "AAPL240119C00190000", "UVXY"])
def test_banned_instruments(league, ticker):
    conn, _ = league
    assert hardlimits.instrument_type(ticker) is None
    errs = hardlimits.validate_slots(conn, [target({ticker: 0.1})], D)
    assert any("not an allowed instrument" in e for e in errs)


def test_allowed_instruments():
    assert hardlimits.instrument_type("SPY") == "us_etfs"
    assert hardlimits.instrument_type("AAPL") == "us_stocks"


def test_penny_stock(league):
    conn, _ = league
    set_price(conn, "KO", "2024-01-10", 4.5)
    errs = hardlimits.validate_slots(conn, [target({"KO": 0.1})], D)
    assert any("below the 5.00 minimum" in e for e in errs)
    # Reducing a position you already hold is never blocked.
    assert not hardlimits.validate_slots(conn, [target({"KO": 0.1})], D, held={(1, "KO"): 0.2})


def test_illiquid(league):
    conn, _ = league
    conn.execute("UPDATE prices SET volume=200000 WHERE ticker='JPM'")
    errs = hardlimits.validate_slots(conn, [target({"JPM": 0.1})], D)
    assert any("20-day average volume" in e for e in errs)


# --------------------------------------------------------------------------
# Hard limits: order time (the layer before the ledger / Alpaca)
# --------------------------------------------------------------------------

def order(conn, ticker="AAPL", delta=1.0, reason="emergency", slot_equity=500.0, has_stop=True, price=100.0):
    return hardlimits.Order("claude", 1, ticker, delta, price, D, reason,
                            ledger.position_shares(conn, "claude", 1, ticker), slot_equity, has_stop)


def test_order_oversize_missing_stop_and_banned(league):
    conn, _ = league
    assert any("of the strategy" in e for e in hardlimits.check_order(conn, order(conn, delta=1.5)))   # 20% -> 50%
    assert any("no stop-loss" in e for e in hardlimits.check_order(conn, order(conn, "SPY", 0.5, has_stop=False)))
    assert any("not an allowed" in e for e in hardlimits.check_order(conn, order(conn, "TQQQ", 0.5)))
    assert hardlimits.check_order(conn, order(conn, delta=-0.5)) == []                    # reduce: always ok


def test_daily_trade_cap(league):
    conn, _ = league
    for i in range(5):
        ledger.execute_trade(conn, "claude", 1, "SPY", 0.02 if i % 2 == 0 else -0.01, D, 100.0, "emergency")
    errs = hardlimits.check_order(conn, order(conn, "SPY", 0.05))
    assert any("already 5 trades today" in e for e in errs)
    # The weekly rebalance and tilts don't count; engine stops never do.
    assert hardlimits.trades_today(conn, "claude", 1, EXEC) == 0
    assert hardlimits.check_order(conn, order(conn, "SPY", 0.05, reason="rebalance")) == []


def test_rebalance_order_check_refuses_and_logs(league, monkeypatch):
    conn, _ = league
    targets = [target({"AAPL": 0.2, "MSFT": 0.2, "XOM": 0.2, "JPM": 0.2, "KO": 0.2}),
               SlotTarget(2, 0.3, {"PG": 0.2, "TQQQ": 0.1}, "defensive", "manual", risk_orders=stops({"PG": 0.2}))]
    set_price(conn, "TQQQ", "2024-01-11", 50)
    eq = ledger.value_agent(conn, "claude", "2024-01-11", "open").equity
    res = ledger.rebalance(conn, "claude", "2024-01-11", targets,
                           order_check=order_check(conn, "claude", "2024-01-11", targets, eq, "rebalance", "test"))
    assert any("TQQQ" in s and "refused" in s for s in res.skipped)
    assert ledger.position_shares(conn, "claude", 2, "TQQQ") == 0
    row = conn.execute("SELECT * FROM rejected_changes WHERE kind='order'").fetchone()
    assert "TQQQ" in row["detail"] and "not an allowed instrument" in row["reason"]


def test_audit_flags_violations(league):
    conn, _ = league
    seq0 = conn.execute("SELECT MAX(seq) FROM trades").fetchone()[0]
    ledger.execute_trade(conn, "claude", 1, "TQQQ", 1.0, D, 50.0, "emergency")
    for _ in range(6):
        ledger.execute_trade(conn, "claude", 1, "SPY", 0.01, D, 100.0, "emergency")
    v = hardlimits.audit_trades(conn, "claude", seq0, 10 ** 9)
    assert any("TQQQ" in x for x in v) and any("more than 5 trades" in x for x in v)


# --------------------------------------------------------------------------
# Reviews: params, cooldown, emergency rewrites, rewrites, tilts, lessons
# --------------------------------------------------------------------------

def run(conn, kind, output, as_of=AS_OF, day=D):
    from .learning_helpers import fixed_runner
    return review.run_review(conn, "claude", kind, day, fixed_runner(output), as_of=as_of)


BASE = {"reasoning": "test", "confidence": 0.6}


def test_param_ranges_and_cooldown(league):
    conn, _ = league
    r = run(conn, "daily", {**BASE, "param_changes": [{"slot": 1, "params": {"stop_loss_pct": 0.12},
                                                       "change_reason": "wider"}]})
    assert r["status"] == "ok" and any("outside its allowed range" in x for x in r["rejected"])
    assert strat.in_slot(conn, "claude", 1).version == 1
    r = run(conn, "daily", {**BASE, "param_changes": [{"slot": 1, "params": {"stop_loss_pct": 0.04},
                                                       "change_reason": "tighter in chop"}]})
    s = strat.in_slot(conn, "claude", 1)
    assert s.version == 2 and s.params["stop_loss_pct"] == 0.04
    v = conn.execute("SELECT * FROM strategy_versions WHERE id=?", (s.version_id,)).fetchone()
    assert v["change_kind"] == "param_tune" and v["change_reason"] == "tighter in chop"
    # Next day: cooldown (2 weeks) refuses another change.
    r = run(conn, "daily", {**BASE, "param_changes": [{"slot": 1, "params": {"stop_loss_pct": 0.05},
                                                       "change_reason": "again"}]},
            as_of="2024-01-11T21:45:00+00:00", day="2024-01-11")
    assert any("cooldown" in x for x in r["rejected"])
    assert conn.execute("SELECT COUNT(*) FROM rejected_changes WHERE kind='param'").fetchone()[0] == 2
    # After the cooldown it's allowed again.
    r = run(conn, "daily", {**BASE, "param_changes": [{"slot": 1, "params": {"stop_loss_pct": 0.05},
                                                       "change_reason": "again"}]},
            as_of="2024-01-25T21:45:00+00:00", day="2024-01-25")
    assert not r["rejected"] and strat.in_slot(conn, "claude", 1).version == 3


def test_emergency_rewrite_only_when_flagged(league):
    conn, s = league
    rw = {"slot": 1, "rules": "only buy above the 50-day average, 4% stops", "change_reason": "drawdown",
          "params": {"stop_loss_pct": 0.04}, "positions": {"AAPL": 0.2, "MSFT": 0.2},
          "risk_orders": {"AAPL": {"stop_loss": 97.0}, "MSFT": {"stop_loss": 97.0}}}
    r = run(conn, "daily", {**BASE, "emergency_rewrites": [rw]})
    assert any("not flagged" in x for x in r["rejected"])
    strat.flag_emergency(conn, s[1].strategy_id, "test drawdown")
    r = run(conn, "daily", {**BASE, "emergency_rewrites": [rw]})
    assert not r["rejected"], r["rejected"]
    st = strat.in_slot(conn, "claude", 1)
    assert st.version == 2 and st.emergency_flagged_at is None and st.params["stop_loss_pct"] == 0.04
    pend = conn.execute("SELECT * FROM pending_orders WHERE kind='emergency'").fetchone()
    assert pend and json.loads(pend["payload"])["positions"] == {"AAPL": 0.2, "MSFT": 0.2}


def _many_trades(conn, n=5):
    for i in range(n):
        ledger.execute_trade(conn, "claude", 1, "SPY", 0.02 if i % 2 == 0 else -0.01, "2024-01-09", 100.0, "rebalance")


def test_weekly_rewrite_needs_a_trigger_and_is_capped(league):
    conn, s = league
    sat, day = "2024-01-13T17:00:00+00:00", "2024-01-13"
    rw = {"slot": 1, "rules": "new rules", "hypothesis": "fewer whipsaws", "trigger": "drawdown_limit"}
    r = run(conn, "weekly", {**BASE, "rewrite": rw}, as_of=sat, day=day)
    assert any("drawdown limit was not hit" in x for x in r["rejected"])
    strat.flag_emergency(conn, s[1].strategy_id, "dd")
    r = run(conn, "weekly", {**BASE, "rewrite": rw}, as_of=sat, day=day)
    assert not r["rejected"], r["rejected"]
    st = strat.in_slot(conn, "claude", 1)
    assert st.challenger is not None and st.version == 1             # the champion is unchanged
    # Second rewrite the same week (another slot): refused by the cap.
    strat.flag_emergency(conn, s[2].strategy_id, "dd")
    r = run(conn, "weekly", {**BASE, "rewrite": {**rw, "slot": 2}}, as_of="2024-01-14T17:00:00+00:00", day="2024-01-14")
    assert any("already used this week" in x for x in r["rejected"])


def test_rewrite_needs_enough_trades(league):
    conn, s = league
    strat.flag_emergency(conn, s[2].strategy_id, "dd")               # slot 2 has only 2 trades
    r = run(conn, "weekly", {**BASE, "rewrite": {"slot": 2, "rules": "x", "hypothesis": "y",
                                                 "trigger": "drawdown_limit"}},
            as_of="2024-01-13T17:00:00+00:00", day="2024-01-13")
    assert any("only 2 trades" in x for x in r["rejected"])


def test_rewrite_refused_in_cooldown(league):
    conn, s = league
    strat.add_version(conn, s[1].strategy_id, "tuned", {"stop_loss_pct": 0.05, "position_max_pct": 0.2},
                      "param_tune", "t", "champion", 1, at="2024-01-09T21:00:00+00:00")
    strat.flag_emergency(conn, s[1].strategy_id, "dd")
    r = run(conn, "weekly", {**BASE, "rewrite": {"slot": 1, "rules": "x", "hypothesis": "y",
                                                 "trigger": "drawdown_limit"}},
            as_of="2024-01-13T17:00:00+00:00", day="2024-01-13")
    assert any("cooldown" in x for x in r["rejected"])


def test_tilt_needs_confirmed_regime_and_is_capped(league):
    conn, _ = league
    def ctx(days):
        conn.execute("DELETE FROM market_context")
        for d, label in days:
            conn.execute("INSERT INTO market_context(ts, day, kind, regime, sector_moves, headlines, regime_inputs) "
                         "VALUES (?,?,'close',?, '{}','[]','{}')", (f"{d}T21:00:00+00:00", d, label))
    tilt = {"allocations": {"1": 0.45, "2": 0.35}, "reason": "rotate"}
    ctx([("2024-01-08", "choppy"), ("2024-01-09", "choppy"), ("2024-01-10", "trend_up")])
    r = run(conn, "daily", {**BASE, "tilt": tilt})
    assert any("not allowed now" in x for x in r["rejected"])
    ctx([("2024-01-08", "choppy"), ("2024-01-09", "trend_up"), ("2024-01-10", "trend_up")])
    r = run(conn, "daily", {**BASE, "tilt": {"allocations": {"1": 0.2, "2": 0.6}, "reason": "big"}})
    assert any("the maximum per tilt is 10%" in x for x in r["rejected"])
    r = run(conn, "daily", {**BASE, "tilt": tilt})
    assert not r["rejected"], r["rejected"]
    assert conn.execute("SELECT COUNT(*) FROM pending_orders WHERE kind='tilt'").fetchone()[0] == 1
    acts = {row["action"] for row in conn.execute("SELECT action FROM decisions WHERE checkpoint='daily_review' "
                                                  "AND source_ref=?", (f"review:{r['run_id']}",))}
    assert acts == {"buy", "sell"}


def test_tilt_fills_at_the_next_open(league):
    conn, _ = league
    from arena.learning import execution
    execution.queue(conn, "claude", "2024-01-10", "tilt", {"allocations": {"1": 0.45, "2": 0.35}}, "test")
    execution.run_open(conn, "2024-01-11", weekly_executed=False)
    v = ledger.value_agent(conn, "claude", "2024-01-11", "open")
    assert v.slot_equity[2] / v.equity == pytest.approx(0.35, abs=0.01)
    assert conn.execute("SELECT status FROM pending_orders WHERE kind='tilt'").fetchone()[0] == "done"
    # On a weekly execution day a waiting tilt is superseded.
    execution.queue(conn, "claude", "2024-01-11", "tilt", {"allocations": {"1": 0.5, "2": 0.3}}, "test")
    execution.run_open(conn, "2024-01-12", weekly_executed=True)
    assert conn.execute("SELECT status FROM pending_orders WHERE kind='tilt' ORDER BY id DESC").fetchone()[0] == "skipped"


def test_lessons_need_evidence_and_confirm_once_a_day(league):
    conn, s = league
    sha = journal.store_input(conn, "p")
    did = journal.record(conn, agent_id="claude", version_id=s[1].version_id, checkpoint="weekly_rebalance",
                         input_sha=sha, input_kind="t", reasoning="r", action="buy", symbol="AAPL",
                         decided_at="2024-01-06T15:00:00+00:00")
    conn.execute("INSERT INTO outcomes_history(decision_id, pnl_1d_pct, evaluated_at) VALUES (?, 1.0, ?)",
                 (did, "2024-01-09T22:00:00+00:00"))
    lesson = {"key": "trend-follow-works", "lesson": "trend entries paid", "kind": "keep"}
    r = run(conn, "daily", {**BASE, "lessons": [{**lesson, "evidence_decisions": [99999]}]})
    assert any("no valid evidence" in x for x in r["rejected"])
    for day in ("2024-01-10", "2024-01-10", "2024-01-11", "2024-01-12"):
        run(conn, "daily", {**BASE, "lessons": [{**lesson, "evidence_decisions": [did]}]},
            as_of=f"{day}T21:45:00+00:00", day=day)
    row = conn.execute("SELECT * FROM lessons WHERE key='trend-follow-works'").fetchone()
    assert row["times_confirmed"] == 3 and row["status"] == "candidate"
    assert lessons.promote_ready(conn, "claude") == [row["id"]]
    assert conn.execute("SELECT status FROM lessons WHERE id=?", (row["id"],)).fetchone()[0] == "active"


def test_open_prices_are_fetched_for_emergency_positions(league):
    from arena import league as lg
    from arena.learning import execution
    conn, _ = league
    execution.queue(conn, "claude", "2024-01-10", "emergency",
                    {"slot": 1, "allocation": 0.5, "positions": {"CVX": 0.2}, "name": "momentum"}, "test")
    assert "CVX" in lg.tickers_needed_at_open(conn, "2024-01-11")
