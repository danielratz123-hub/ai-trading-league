"""Risk-adjusted scoring: the formula, the drawdown path, the switch-over week."""
from __future__ import annotations

import pytest

from arena import ledger, scoring
from arena.learning import scores

from .learning_helpers import league_week1, prices, set_price


def test_formula_and_floor():
    assert scores.score(2.0, 4.0, False) == pytest.approx(0.5)
    assert scores.score(2.0, 0.3, False) == pytest.approx(2.0)        # 1% floor
    assert scores.score(-3.0, 6.0, False) == pytest.approx(-0.5)
    assert scores.score(10.0, 2.0, True) == 0.0                       # broke a hard limit


def test_max_drawdown_path():
    assert scores.max_drawdown_pct([100, 110, 99, 105]) == pytest.approx(10.0)
    assert scores.max_drawdown_pct([100, 101, 102]) == 0.0


def test_winner_and_ties():
    assert scores.winner({"a": {"score": 1.0}, "b": {"score": 0.5}}) == "a"
    assert scores.winner({"a": {"score": 1.0}, "b": {"score": 1.0}}) is None


def test_mode_switch(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    assert scores.mode_for(9) == "raw_return"
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    assert scores.mode_for(4) == "raw_return" and scores.mode_for(5) == "risk_adjusted"


def _two_agents_week(conn, week_no: int):
    """Two agents traded week_no at 2024-01-08's open; marked at 2024-01-16's open."""
    prices(conn)
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (?, '2024-01-05', '2024-01-08', "
                 "'executed')", (week_no,))
    league_week1(conn, "claude", {1: ("steady", 0.5, {"KO": 0.2, "PG": 0.2})}, through="2024-01-12")
    league_week1(conn, "gpt", {1: ("wild", 1.0, {"AAPL": 0.2, "MSFT": 0.2, "XOM": 0.2, "JPM": 0.2, "SPY": 0.2})},
                 through="2024-01-12")


def test_risk_adjusted_point_beats_raw_return(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    _two_agents_week(conn, 5)
    # gpt: deep dip mid-week, ends higher; claude: small, smooth gain.
    for t in ("AAPL", "MSFT", "XOM", "JPM", "SPY"):
        set_price(conn, t, "2024-01-10", 100, 80)
        set_price(conn, t, "2024-01-16", 112)
    for t in ("KO", "PG"):
        set_price(conn, t, "2024-01-16", 103)
    conn.execute("DELETE FROM snapshots WHERE kind='daily'")
    for a in ("claude", "gpt"):
        for d in ("2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12"):
            ledger.snapshot(conn, a, d, "daily", "close")
    marks = {a: ledger.snapshot(conn, a, "2024-01-16", "mark", "open") and
             ledger.latest_snapshot(conn, a, "mark")["id"] for a in ("claude", "gpt")}
    rets = scoring.score_week(conn, 5, "2024-01-08", marks)
    assert rets["gpt"] > rets["claude"]                                   # gpt has the higher raw return ...
    rows = {r["agent_id"]: r for r in conn.execute("SELECT * FROM weekly_scores WHERE week_no=5")}
    assert rows["gpt"]["mode"] == "risk_adjusted"
    assert rows["gpt"]["max_drawdown_pct"] > 15                           # ... after a ~20% dip
    assert rows["claude"]["score"] > rows["gpt"]["score"]                 # ... but claude wins on risk
    points = {r["agent_id"]: r["point"] for r in conn.execute("SELECT * FROM scores WHERE week_no=5")}
    assert points == {"claude": 1, "gpt": 0}
    assert rows["claude"]["point_won"] == 1
    assert rows["claude"]["return_pct"] == pytest.approx(rets["claude"] * 100)


def test_weeks_before_start_keep_raw_return(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    _two_agents_week(conn, 2)
    for t in ("AAPL", "MSFT", "XOM", "JPM", "SPY"):
        set_price(conn, t, "2024-01-10", 100, 80)
        set_price(conn, t, "2024-01-16", 112)
    marks = {a: ledger.snapshot(conn, a, "2024-01-16", "mark", "open") and
             ledger.latest_snapshot(conn, a, "mark")["id"] for a in ("claude", "gpt")}
    scoring.score_week(conn, 2, "2024-01-08", marks)
    points = {r["agent_id"]: r["point"] for r in conn.execute("SELECT * FROM scores WHERE week_no=2")}
    assert points == {"claude": 0, "gpt": 1}                              # raw return still decides
    assert {r["mode"] for r in conn.execute("SELECT mode FROM weekly_scores")} == {"raw_return"}


def test_learning_off_writes_nothing_new(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    _two_agents_week(conn, 5)
    marks = {a: ledger.snapshot(conn, a, "2024-01-16", "mark", "open") and
             ledger.latest_snapshot(conn, a, "mark")["id"] for a in ("claude", "gpt")}
    scoring.score_week(conn, 5, "2024-01-08", marks)
    assert conn.execute("SELECT COUNT(*) FROM weekly_scores").fetchone()[0] == 0
