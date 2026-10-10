"""The learning layer end to end, on the synthetic market of test_tools:
weekly decision (journaled) -> execute (fills linked) -> daily close
(market data, grader, daily review) -> the new rules from start_week ->
weekly review with a challenger -> shadow verdict."""
from __future__ import annotations

import dataclasses
import json

import pytest

from arena import league, ledger
from arena.learning import config as lconfig, jobs, review, strategies as strat
from arena.prices import price_on_or_before

from .fakes import Factory, submit
from .learning_helpers import fixed_runner
from .test_tools import _db

WEEK1, WEEK2 = "2024-01-05", "2024-01-12"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    cfg = lconfig.get()
    # The new rules start at week 2 here (week 5 in the real config).
    monkeypatch.setattr(lconfig, "get", lambda: dataclasses.replace(
        cfg, scoring=dataclasses.replace(cfg.scoring, start_week=2)))
    c = _db()
    yield c
    c.close()


FETCH = {"vix_fetch": lambda: {"2024-01-08": 13.5, "2024-01-09": 14.0, "2024-01-10": 14.2},
         "news_fetch": lambda: [{"title": "Stocks drift", "source": "test", "ts": "2024-01-10"}]}


def slot(conn, d, name="tech momentum", weights=None, stop_pct=0.04, rules=None, conf=0.7, **extra):
    weights = weights or {"AAPL": 0.2, "MSFT": 0.2, "NVDA": 0.2}
    ro = {t: {"stop_loss": round(price_on_or_before(conn, t, d)[1] * (1 - stop_pct), 2)} for t in weights}
    s = {"slot": 1, "name": name, "type": "manual", "allocation": 0.6, "positions": weights, "risk_orders": ro,
         "reason": "trend", "confidence": conf, **extra}
    if rules:
        s["rules"] = rules
    return s


def test_week1_is_journaled_and_fills_are_linked(conn):
    f = Factory({"claude": [[[submit([slot(conn, WEEK1, rules="Buy the strongest large-cap tech; 4% stops.")])]]],
                 "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    rows = conn.execute("SELECT * FROM decisions WHERE agent_id='claude' ORDER BY id").fetchall()
    assert [(r["action"], r["symbol"]) for r in rows] == [("buy", "AAPL"), ("buy", "MSFT"), ("buy", "NVDA")]
    assert all(r["checkpoint"] == "weekly_rebalance" and r["confidence"] == 0.7 and r["week_no"] == 1 for r in rows)
    s = strat.in_slot(conn, "claude", 1)
    assert s.name == "tech momentum" and s.rules_text.startswith("Buy the strongest")
    # The input snapshot is exactly the prompt the agent got, briefing section included.
    sha = json.loads(rows[0]["input_snapshot"])["sha256"]
    text = conn.execute("SELECT content FROM decision_inputs WHERE sha256=?", (sha,)).fetchone()[0]
    assert "Your strategies (learning layer)" in text and "Tracking only until week 2" in text

    league.execute(conn)
    fills = conn.execute("SELECT decision_id FROM trades WHERE agent_id='claude'").fetchall()
    assert fills and all(r[0] is not None for r in fills)


def _through_week1(conn):
    f = Factory({"claude": [[[submit([slot(conn, WEEK1, rules="Buy the strongest large-cap tech; 4% stops.")])]]],
                 "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    for d in ("2024-01-08", "2024-01-09", "2024-01-10"):
        league.daily(conn, d)


def test_daily_close_and_review(conn):
    _through_week1(conn)
    runner = fixed_runner({"reasoning": "nothing to change yet", "confidence": 0.55,
                           "strategy_notes": [{"slot": 1, "note": "on plan"}]})
    out = jobs.daily_review(conn, "2024-01-10", runner, **FETCH)
    assert out["close"]["collector"]["regime"] in ("trend_up", "trend_down", "choppy", "high_vol")
    assert out["close"]["collector"]["vix"] == 14.2 and out["close"]["collector"]["headlines"] == 1
    snap = conn.execute("SELECT indicators FROM daily_snapshots WHERE symbol='AAPL' AND day='2024-01-10'").fetchone()
    assert set(json.loads(snap[0])) == {"rsi_14", "sma_20", "sma_50", "sma_200", "atr_14", "avg_volume_20"}
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] >= 3       # week-1 buys graded
    claude = next(r for r in out["reviews"] if r["agent"] == "claude")
    assert claude["status"] == "ok"
    hold = conn.execute("SELECT * FROM decisions WHERE checkpoint='daily_review' AND agent_id='claude'").fetchone()
    assert hold["action"] == "hold" and hold["reasoning"].startswith("on plan") and hold["confidence"] == 0.55
    # The prompt held only what the spec allows.
    inp = runner.calls[0]["inp"]
    assert set(inp) == {"agent", "kind", "as_of", "day", "regime", "market_context_last_5_days", "headlines",
                        "strategies", "active_lessons", "graded_decisions_since_last_review",
                        "candidate_lessons", "limits"}
    assert inp["graded_decisions_since_last_review"]


def test_new_rules_apply_from_start_week(conn):
    _through_week1(conn)
    for d in ("2024-01-11", "2024-01-12"):
        league.daily(conn, d)
    wrong_name = slot(conn, WEEK2, name="something else")
    too_big = slot(conn, WEEK2, weights={"AAPL": 0.25, "MSFT": 0.2})
    ok = slot(conn, WEEK2, weights={"AAPL": 0.2, "MSFT": 0.2, "XOM": 0.2})
    f = Factory({"claude": [[[submit([wrong_name])], [submit([too_big])], [submit([ok])]]],
                 "gpt": [[[submit([])]]]})
    reports = league.decide(conn, WEEK2, factory=f)
    claude = next(r for r in reports if r.agent_id == "claude")
    assert claude.status == "accepted" and claude.attempts == 3
    errs = [e for r in conn.execute("SELECT errors FROM decision_attempts WHERE ok=0") for e in json.loads(r["errors"])]
    assert any("runs the strategy" in e for e in errs)
    assert any("no single position may exceed 20%" in e for e in errs)
    assert any("position_max_pct" in e for e in errs)
    acts = {(r["action"], r["symbol"]) for r in conn.execute(
        "SELECT action, symbol FROM decisions WHERE week_no=2 AND agent_id='claude'")}
    assert ("sell", "NVDA") in acts and ("buy", "XOM") in acts


def test_weekly_review_challenger_and_shadow_verdict(conn):
    _through_week1(conn)
    for d in ("2024-01-11", "2024-01-12"):
        league.daily(conn, d)
    # The decision was made "now" (wall clock); date the strategy into the test calendar.
    conn.execute("UPDATE strategies SET created_at='2024-01-06T15:00:00+00:00'")
    s = strat.in_slot(conn, "claude", 1)
    for i in range(3):          # enough trades for a rewrite
        ledger.execute_trade(conn, "claude", 1, "AAPL", 0.01 if i % 2 == 0 else -0.01, "2024-01-11", 100.0, "rebalance")
    strat.flag_emergency(conn, s.strategy_id, "test")
    rw = {"slot": 1, "rules": "Tech momentum, but only above the 50-day average.", "hypothesis": "fewer whipsaws",
          "trigger": "drawdown_limit", "params": {"stop_loss_pct": 0.05}}
    out = jobs.weekly_review(conn, "2024-01-13", fixed_runner({"reasoning": "rewrite", "confidence": 0.6, "rewrite": rw},
                                                             {"reasoning": "hold", "confidence": 0.5}))
    assert not out["agents"]["claude"]["review"]["rejected"], out["agents"]["claude"]["review"]["rejected"]
    s = strat.in_slot(conn, "claude", 1)
    assert s.challenger and s.challenger["params"]["stop_loss_pct"] == 0.05
    # Week 2's decision must now include the challenger's positions.
    no_shadow = slot(conn, WEEK2, weights={"AAPL": 0.2, "MSFT": 0.2})
    with_shadow = slot(conn, WEEK2, weights={"AAPL": 0.2, "MSFT": 0.2},
                       challenger_positions={"MSFT": 0.2, "XOM": 0.2, "JPM": 0.2})
    f = Factory({"claude": [[[submit([no_shadow])], [submit([with_shadow])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK2, factory=f)
    errs = json.loads(conn.execute("SELECT errors FROM decision_attempts WHERE ok=0 ORDER BY id DESC").fetchone()[0])
    assert any("challenger_positions" in e for e in errs)
    run = conn.execute("SELECT * FROM shadow_runs").fetchone()
    assert run["verdict"] == "pending" and json.loads(run["weights"]) == {"MSFT": 0.2, "XOM": 0.2, "JPM": 0.2}
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE shadow=1").fetchone()[0] == 3
    # A week later the weekly review decides it.
    league.execute(conn)
    for d in ("2024-01-15", "2024-01-16", "2024-01-17", "2024-01-18", "2024-01-19"):
        league.daily(conn, d)
    out = jobs.weekly_review(conn, "2024-01-20", fixed_runner())
    verdict = out["agents"]["claude"]["shadow"][0]
    run = conn.execute("SELECT * FROM shadow_runs").fetchone()
    better = (run["return_pct"] > run["champion_return_pct"]
              and run["max_drawdown_pct"] < run["champion_max_drawdown_pct"])
    assert verdict["verdict"] == ("promoted" if better else "dropped")
    s = strat.in_slot(conn, "claude", 1)
    assert (s.version == 2) == better and s.challenger is None
    lesson = conn.execute("SELECT * FROM lessons WHERE source='engine'").fetchone()
    assert lesson["key"].startswith("challenger-s1-v2-") and json.loads(lesson["evidence_decisions"])


def test_invalid_output_twice_falls_back_to_hold(conn):
    _through_week1(conn)
    out = jobs.daily_review(conn, "2024-01-10", fixed_runner(None, None), **FETCH)
    for r in out["reviews"]:
        assert r["status"] in ("fallback", "ok")
    claude = next(r for r in out["reviews"] if r["agent"] == "claude")
    assert claude["status"] == "fallback"
    row = conn.execute("SELECT * FROM decisions WHERE checkpoint='daily_review' AND agent_id='claude'").fetchone()
    assert row["action"] == "hold" and row["fallback"] == 1 and "FALLBACK" in row["reasoning"]
    run = conn.execute("SELECT * FROM review_runs WHERE agent_id='claude'").fetchone()
    assert run["status"] == "fallback" and "confidence" in run["errors"]


def test_review_server_validates_and_allows_one_retry(conn, tmp_path):
    from types import SimpleNamespace

    from arena.mcp_server import ReviewServer
    inp = {"strategies": [{"slot": 1}], "agent": "claude"}
    (tmp_path / "in.json").write_text(json.dumps(inp))
    srv = ReviewServer(SimpleNamespace(review="daily", review_input=str(tmp_path / "in.json"),
                                       result=str(tmp_path / "r.json")))
    assert [t["name"] for t in srv.list_tools()] == ["submit_review"]
    text, err = srv.call("submit_review", {"reasoning": "x", "confidence": 3})
    assert err and "confidence" in text and "last try" in text
    text, err = srv.call("submit_review", {"reasoning": "x", "confidence": 0.3})
    assert not err
    state = json.loads((tmp_path / "r.json").read_text())
    assert state["accepted"] and state["attempts"] == 2
    srv2 = ReviewServer(SimpleNamespace(review="daily", review_input=str(tmp_path / "in.json"),
                                        result=str(tmp_path / "r2.json")))
    srv2.call("submit_review", {})
    text, err = srv2.call("submit_review", {})
    assert err and "falls back to 'hold'" in text
    text, err = srv2.call("submit_review", {"reasoning": "x", "confidence": 0.3})
    assert err and "No retries left" in text


def test_validation_catches_structure():
    inp = {"strategies": [{"slot": 1}, {"slot": 3}]}
    v = review.validate_output
    assert v("daily", {"reasoning": "ok", "confidence": 0.5}, inp) == []
    assert any("slot 2 has no active strategy" in e for e in v("daily", {
        "reasoning": "ok", "confidence": 0.5,
        "param_changes": [{"slot": 2, "params": {"stop_loss_pct": 0.05}, "change_reason": "x"}]}, inp))
    assert any("key must look like" in e for e in v("weekly", {
        "reasoning": "ok", "confidence": 0.5,
        "lessons": [{"key": "Bad Key", "lesson": "x", "kind": "keep", "evidence_decisions": [1]}]}, inp))
    assert v("daily", "not an object", inp) == ["The review must be a JSON object."]
