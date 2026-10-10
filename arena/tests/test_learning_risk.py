"""Hourly risk check: rules, logging, wake-ups, reduce-only fills, no LLM when quiet."""
from __future__ import annotations

import json

import pytest

from arena import ledger
from arena.learning import jobs, riskcheck, strategies as strat

from .learning_helpers import fixed_runner, league_week1, minute, no_runner, prices, utc

D = "2024-01-10"              # Wednesday of week 1
NOW = utc(D, 11, 0)


@pytest.fixture
def league(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    return conn


def flat_minutes(conn, price=100.0, skip=()):
    for t in ("SPY", "AAPL", "MSFT", "XOM", "JPM", "KO", "PG", "VIXY"):
        if t not in skip:
            minute(conn, t, D, 10, 59, price)


def test_nothing_fires_logs_only_and_never_calls_the_agent(league):
    flat_minutes(league)
    out = jobs.risk_check(league, D, no_runner, now=NOW)
    assert out["fired"] == 0 and out["woken"] == []
    row = league.execute("SELECT * FROM risk_checks").fetchone()
    assert row["fired"] == 0 and row["positions"] == 7 and json.loads(row["woke"]) == []
    assert league.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
    # Strategy equity was snapshotted at the checkpoint.
    assert league.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] >= 2


def test_stop_loss_and_gap_fire_and_wake_only_that_agent(league):
    flat_minutes(league, skip=("AAPL",))
    minute(league, "AAPL", D, 10, 59, 93.0)          # -7% vs entry (6% stop) and vs yesterday's close (5% gap)
    runner = fixed_runner({"reasoning": "cut the loser", "confidence": 0.7,
                           "actions": [{"type": "reduce", "slot": 1, "ticker": "AAPL", "fraction": 0.5}]})
    out = jobs.risk_check(league, D, runner, now=NOW)
    rules = sorted(e["rule"] for e in out["events"])
    assert rules == ["gap_alert", "stop_loss"]
    assert len(runner.calls) == 1 and runner.calls[0]["kind"] == "risk"
    assert {f["rule"] for f in runner.calls[0]["inp"]["situation"]["fired"]} == {"gap_alert", "stop_loss"}
    # Filled now, at the minute price (+ slippage), reduce-only, linked to its decision.
    tr = league.execute("SELECT * FROM trades WHERE reason='risk_exit'").fetchone()
    assert tr["ticker"] == "AAPL" and tr["date"] == D and tr["shares"] < 0
    assert tr["ref_price"] == pytest.approx(93.0) and tr["note"].startswith("[reduce-only]")
    dec = league.execute("SELECT * FROM decisions WHERE id=?", (tr["decision_id"],)).fetchone()
    assert dec["checkpoint"] == "risk_exit" and dec["action"] == "sell" and dec["symbol"] == "AAPL"
    assert dec["confidence"] == 0.7
    held = ledger.position_shares(league, "claude", 1, "AAPL")
    assert held == pytest.approx(-tr["shares"])     # half sold, half left


def test_same_rule_wakes_at_most_once_a_day(league):
    flat_minutes(league, skip=("AAPL",))
    minute(league, "AAPL", D, 10, 59, 93.0)
    hold = {"reasoning": "keep it", "confidence": 0.4, "actions": [{"type": "hold", "slot": 1}]}
    jobs.risk_check(league, D, fixed_runner(hold), now=NOW)
    minute(league, "AAPL", D, 11, 59, 93.0)
    out = jobs.risk_check(league, D, no_runner, now=utc(D, 12, 0))
    assert out["fired"] >= 1 and all(e["suppressed"] for e in out["events"])
    hold_row = league.execute("SELECT action FROM decisions WHERE checkpoint='risk_exit'").fetchall()
    assert [r[0] for r in hold_row] == ["hold"]           # the first wake-up's hold, nothing more


def test_vix_spike_fires_for_every_agent_with_positions(league):
    flat_minutes(league, skip=("VIXY",))
    minute(league, "VIXY", D, 10, 59, 112.0)         # +12% on the proxy ~ VIX +24% (> 20%)
    league.execute("INSERT INTO daily_snapshots(symbol, day, close, volume) VALUES ('VIXY', '2024-01-09', 100, 0)")
    out = jobs.risk_check(league, D, None, now=NOW)
    assert [e["rule"] for e in out["events"]] == ["vix_spike"]
    assert league.execute("SELECT note FROM risk_checks").fetchone()[0].startswith("no agent runner")


def test_strategy_drawdown_flags_emergency_rewrite(league):
    flat_minutes(league, price=88.0)                 # every position -12%; slot 1 fully invested
    out = jobs.risk_check(league, D, None, now=NOW)
    assert "strategy_drawdown" in {e["rule"] for e in out["events"]}
    assert strat.in_slot(league, "claude", 1).emergency_flagged_at is not None


def test_failed_output_falls_back_to_hold(league):
    flat_minutes(league, skip=("AAPL",))
    minute(league, "AAPL", D, 10, 59, 93.0)
    out = jobs.risk_check(league, D, fixed_runner(None), now=NOW)
    assert out["woken"][0]["status"] == "fallback"
    rows = league.execute("SELECT action, fallback, reasoning FROM decisions WHERE checkpoint='risk_exit'").fetchall()
    assert rows and all(r["action"] == "hold" and r["fallback"] == 1 for r in rows)
    assert "FALLBACK" in rows[0]["reasoning"]
    assert league.execute("SELECT COUNT(*) FROM trades WHERE reason='risk_exit'").fetchone()[0] == 0


def test_risk_review_cannot_buy(league):
    from arena.learning.review import validate_output
    inp = {"strategies": [{"slot": 1}]}
    errs = validate_output("risk", {"reasoning": "x", "confidence": 0.5,
                                    "actions": [{"type": "buy", "slot": 1, "ticker": "AAPL"}]}, inp)
    assert errs and "reduce, close, raise_cash or hold" in errs[0]


def test_stale_prices_are_not_checked(league):
    minute(league, "AAPL", D, 9, 30, 50.0)          # 90 minutes old at 11:00: ignored
    out = riskcheck.run_check(league, D, NOW, None)
    assert out["positions_checked"] == 0 and out["fired"] == 0
