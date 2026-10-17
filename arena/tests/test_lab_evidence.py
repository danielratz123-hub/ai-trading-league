"""Backtest lab, phase 4: the evidence rules (EVIDENCE_RULES=1)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from arena.db import connect
from arena.lab import config as labconfig, evidence, histdata, states
from arena.learning import lessons, review, strategies as strat
from arena.prices import BAR_COLUMNS
from arena.tests.test_lab_engine import DAYS, FRAME, TICKERS

CHAMPION_CODE = """
def target_weights(prices, date):
    return {"AAPL": 0.2, "MSFT": 0.2}
"""
# Drifts in the synthetic market: AAPL/MSFT down, PFE/F up.
CHALLENGER_CODE = """
def target_weights(prices, date):
    return {"PFE": 0.2, "F": 0.2}
"""
AT = "2024-01-13T17:00:00+00:00"          # a Saturday weekly review


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_RULES", "1")
    hpath = tmp_path / "history.db"
    monkeypatch.setenv("HISTORY_DB", str(hpath))
    cfg = labconfig.get()
    small = dataclasses.replace(
        cfg,
        walk_forward=dataclasses.replace(cfg.walk_forward, design_months=12, test_months=3, step_months=3),
        evidence=dataclasses.replace(cfg.evidence, freeze_min_live_trades=0, freeze_min_weeks=0, min_oos_trades=3,
                                     regime_margin_pct=1000.0, shadow_min_trades=1, shadow_score_margin=0.0,
                                     shadow_bootstrap_prob=0.5, shadow_bootstrap_samples=200, shadow_max_weeks=200,
                                     lesson_min_backtest_trades=3, lesson_min_live_trades=2))
    monkeypatch.setattr(labconfig, "get", lambda: small)
    h = histdata.connect(hpath)
    h.executemany("INSERT INTO hist_calendar VALUES (?,?,?)", [(d, "09:30", "16:00") for d in DAYS])
    long = FRAME.stack(level=1, future_stack=True).reset_index()
    long.columns = ["date", "ticker", *long.columns[2:]]
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")
    histdata._insert(h, long[BAR_COLUMNS])
    for t in TICKERS:
        histdata._refresh_symbol(h, t)
    h.commit()
    c = connect(":memory:")
    from arena import ledger
    ledger.create_agent(c, "claude", 1000.0, "2024-01-02")
    states.label_all(c, h)
    s = strat.create(c, "claude", 1, "momentum", "buy strength", None, "initial", "test", 1, "2023-11-01T21:00:00+00:00")
    yield c, h, s
    c.close()
    h.close()


def test_switch_and_tools(lab, monkeypatch):
    assert [t["name"] for t in review.tools_for("weekly")] == ["submit_review", "run_backtest"]
    assert [t["name"] for t in review.tools_for("daily")] == ["submit_review"]
    assert "tilt" not in review.spec_for("daily")["parameters"]["properties"]
    monkeypatch.setenv("EVIDENCE_RULES", "0")
    assert [t["name"] for t in review.tools_for("weekly")] == ["submit_review"]
    assert "tilt" in review.spec_for("daily")["parameters"]["properties"]


def test_freeze_needs_trades_and_weeks(lab, monkeypatch):
    conn, _, s = lab
    cfg = labconfig.get()
    monkeypatch.setattr(labconfig, "get", lambda: dataclasses.replace(cfg, evidence=dataclasses.replace(
        cfg.evidence, freeze_min_live_trades=3, freeze_min_weeks=3)))
    frozen, why, _ = evidence.freeze_status(conn, s, AT)
    assert frozen and "0/3 live trades" in why
    for i in range(3):
        conn.execute("INSERT INTO trades(seq, agent_id, slot, date, ticker, shares, ref_price, fill_price, value, "
                     "commission, slippage_cost, realized_pnl, reason) VALUES (?,?,1,'2023-12-01','AAPL',1,1,1,1,0,0,0,"
                     "'rebalance')", (i, "claude"))
    assert not evidence.freeze_status(conn, s, AT)[0]
    assert evidence.freeze_status(conn, s, "2023-11-10T00:00:00+00:00")[0]      # trades, but not 3 weeks yet


def test_agent_backtest_tool_budget_holdout_and_log(lab, monkeypatch):
    conn, h, _ = lab
    bad = evidence.agent_backtest(conn, h, "claude", {"idea_key": "Bad Key", "code": CHALLENGER_CODE, "purpose": "design"})
    assert not bad["ok"] and "idea_key" in bad["refused"]
    locked = evidence.agent_backtest(conn, h, "claude", {"idea_key": "up-drift", "code": CHALLENGER_CODE,
                                                         "purpose": "holdout"})
    assert not locked["ok"] and "locked" in locked["refused"]
    into = evidence.agent_backtest(conn, h, "claude", {"idea_key": "up-drift", "code": CHALLENGER_CODE,
                                                       "purpose": "design", "start": "2017-01-02", "end": "2019-06-28"})
    assert not into["ok"] and "holdout" in into["refused"]
    ok = evidence.agent_backtest(conn, h, "claude", {"idea_key": "up-drift", "code": CHALLENGER_CODE,
                                                     "purpose": "design", "start": "2017-01-02", "end": "2017-12-29"})
    assert ok["ok"] and ok["attempts"] == 2 and "Survivorship" in ok["data_note"] and ok["by_state"]
    row = conn.execute("SELECT requested_by, purpose, status FROM backtest_runs WHERE id=?", (ok["run_id"],)).fetchone()
    assert tuple(row) == ("agent:claude", "design", "ok")
    cfg = labconfig.get()
    monkeypatch.setattr(labconfig, "get", lambda: dataclasses.replace(cfg, evidence=dataclasses.replace(
        cfg.evidence, backtest_budget=2)))
    over = evidence.agent_backtest(conn, h, "claude", {"idea_key": "up-drift", "code": CHALLENGER_CODE,
                                                       "purpose": "design"})
    assert not over["ok"] and "budget" in over["refused"]


def test_lessons_need_backtest_and_live_evidence(lab):
    conn, h, s = lab
    lid, _ = lessons.upsert(conn, agent_id="claude", key="up-drift-works", lesson="Upward drifters keep going",
                            kind="keep", evidence=[], day="2024-01-12", at="2024-01-12T21:00:00+00:00")
    wf = evidence.agent_backtest(conn, h, "claude", {"idea_key": "up-drift", "code": CHALLENGER_CODE,
                                                     "purpose": "walk_forward"})
    assert wf["ok"] and wf["trades"] >= 3
    evidence.record_lesson_evidence(conn, "claude", lid, [], [wf["run_id"]], AT)
    assert evidence.promote_lessons(conn, "claude", AT)["confirmed"] == []           # no live trades yet
    from arena.learning import journal
    ids = [journal.record(conn, agent_id="claude", version_id=s.version_id, checkpoint="weekly_rebalance",
                          input_sha="x", input_kind="weekly_decision", reasoning="r", action="buy", symbol=t,
                          target_weight=0.2, week_no=1, decided_at="2024-01-06T15:00:00+00:00") for t in ("PFE", "F")]
    evidence.record_lesson_evidence(conn, "claude", lid, ids, [], AT)
    ev = evidence.lesson_evidence(conn, lid)
    assert ev["live"]["trades"] == 2 and ev["backtest"]["best_walk_forward"]["trades"] == wf["trades"]
    assert evidence.promote_lessons(conn, "claude", AT)["confirmed"] == [lid]
    assert [l["id"] for l in lessons.for_prompt(conn, "claude", None, "2024-02-01T00:00:00+00:00")] == [lid]
    # An unconfirmed hypothesis is retired after hypothesis_max_weeks.
    old, _ = lessons.upsert(conn, agent_id="claude", key="old-idea", lesson="x", kind="keep", evidence=[1],
                            day="2023-10-01", at="2023-10-01T00:00:00+00:00")
    assert old in evidence.promote_lessons(conn, "claude", AT)["retired"]


def _code_champion(conn, s):
    evidence._store_code(conn, s.version_id, "champion-idea", CHAMPION_CODE, AT)


def _rewrite(code=CHALLENGER_CODE, key="up-drift"):
    return {"slot": 1, "rules": "hold the up-drifters", "hypothesis": "drift persists", "trigger": "lesson",
            "code": code, "idea_key": key}


def test_challenger_goes_backtest_holdout_shadow_promotion(lab):
    conn, h, s = lab
    _code_champion(conn, s)
    ok, msg, vid = evidence.propose(conn, h, "claude", s, _rewrite(), AT, 3)
    assert ok, msg
    stages = [(r["stage"], r["passed"]) for r in conn.execute("SELECT stage, passed FROM promotion_log ORDER BY id")]
    assert stages == [("proposed", 1), ("freeze", 1), ("walk_forward", 1), ("regimes", 1), ("holdout", 1),
                      ("shadow_started", 1)]
    purposes = [r[0] for r in conn.execute("SELECT purpose FROM backtest_runs WHERE idea_key='up-drift' ORDER BY id")]
    assert purposes == ["walk_forward", "holdout"]
    champ = strat.in_slot(conn, "claude", 1)
    assert champ.version_id == s.version_id and champ.challenger["version_id"] == vid      # champion unaffected
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    # Shadow on "new" data: start the record back in the data, then evaluate.
    conn.execute("UPDATE lab_shadows SET start_day='2018-01-02'")
    res = evidence.evaluate_shadows(conn, h, "claude", through="2018-12-31", at=AT)
    assert res[0]["status"] == "promoted", res
    assert strat.in_slot(conn, "claude", 1).version_id == vid
    assert conn.execute("SELECT code FROM strategy_code WHERE agent_id='claude' AND slot=1").fetchone()[0] == CHALLENGER_CODE
    assert conn.execute("SELECT COUNT(*) FROM promotion_log WHERE stage='promoted'").fetchone()[0] == 1


def test_failed_holdout_retires_the_idea_for_good(lab):
    conn, h, s = lab
    evidence._store_code(conn, s.version_id, "champion-idea", CHALLENGER_CODE, AT)   # champion = the good one
    ok, msg, vid = evidence.propose(conn, h, "claude", s, _rewrite(code=CHAMPION_CODE, key="down-drift"), AT, 3)
    assert not ok and msg.startswith("walk_forward")          # worse than the champion: stops at gate 1
    assert strat.version_row(conn, vid)["role"] == "dropped"
    # A challenger that passes walk-forward but then fails the holdout is retired.
    cfg = labconfig.get()
    import arena.lab.config as lc
    lc_get = lc.get
    lc.get = lambda: dataclasses.replace(cfg, evidence=dataclasses.replace(cfg.evidence, holdout_margin=1e9))
    try:
        _code_champion(conn, s)
        ok, msg1, _ = evidence.propose(conn, h, "claude", s, _rewrite(key="once-only"), AT, 3)
        assert ok, msg1
        # Same idea again: the holdout was used; the idea is refused and retired.
        strat.drop(conn, strat.in_slot(conn, "claude", 1).challenger["version_id"])
        ok2, msg2, _ = evidence.propose(conn, h, "claude", s, _rewrite(key="once-only"), AT, 3)
        assert not ok2 and "holdout" in msg2
        assert conn.execute("SELECT 1 FROM retired_ideas WHERE idea_key='once-only'").fetchone()
        ok3, msg3, _ = evidence.propose(conn, h, "claude", s, _rewrite(key="once-only"), AT, 3)
        assert not ok3 and "retired" in msg3
    finally:
        lc.get = lc_get


def test_a_new_challenger_restarts_the_shadow_record_and_timeouts_retire(lab, monkeypatch):
    conn, h, s = lab
    _code_champion(conn, s)
    assert evidence.propose(conn, h, "claude", s, _rewrite(key="first"), AT, 3)[0]
    first = strat.in_slot(conn, "claude", 1).challenger["version_id"]
    assert evidence.propose(conn, h, "claude", s, _rewrite(code=CHALLENGER_CODE + "\n# v2\n", key="second"), AT, 3)[0]
    rows = {r["version_id"]: r["status"] for r in conn.execute("SELECT version_id, status FROM lab_shadows")}
    assert rows[first] == "retired" and list(rows.values()).count("running") == 1
    cfg = labconfig.get()
    monkeypatch.setattr(labconfig, "get", lambda: dataclasses.replace(cfg, evidence=dataclasses.replace(
        cfg.evidence, shadow_min_trades=10_000, shadow_max_weeks=2)))
    conn.execute("UPDATE lab_shadows SET start_day='2018-01-02' WHERE status='running'")
    res = evidence.evaluate_shadows(conn, h, "claude", through="2018-03-30", at=AT)
    assert res[0]["status"] == "retired"
    assert conn.execute("SELECT 1 FROM retired_ideas WHERE idea_key='second'").fetchone()
    assert strat.in_slot(conn, "claude", 1).version_id == s.version_id         # the champion never changed


def test_bootstrap_and_weekly_scores():
    import pandas as pd
    assert evidence.bootstrap_prob([1.0] * 10, [0.0] * 10, 500) == 1.0
    assert evidence.bootstrap_prob([0.0] * 10, [1.0] * 10, 500) == 0.0
    p = evidence.bootstrap_prob([1, -1] * 10, [0, 0] * 10, 2000)
    assert 0.3 < p < 0.7
    eq = pd.Series([100, 101, 102, 101, 103, 104], index=pd.to_datetime(
        ["2024-01-08", "2024-01-09", "2024-01-12", "2024-01-15", "2024-01-16", "2024-01-19"]))
    assert len(evidence.weekly_scores(eq)) == 2


def test_daily_review_only_records_observations(lab):
    conn, _, _ = lab
    errs = review.validate_output("daily", {"reasoning": "x", "confidence": 0.5, "tilt": {"allocations": {"1": 1},
                                                                                           "reason": "r"}},
                                  {"strategies": [{"slot": 1}]})
    assert any("only records observations" in e for e in errs)
    errs = review.validate_output("weekly", {"reasoning": "x", "confidence": 0.5,
                                             "rewrite": {"slot": 1, "rules": "r", "hypothesis": "h", "trigger": "lesson"}},
                                  {"strategies": [{"slot": 1}]})
    assert any("code" in e for e in errs)
    ok = review.validate_output("weekly", {"reasoning": "x", "confidence": 0.5, "lessons": [
        {"key": "a-b", "lesson": "l", "kind": "keep", "backtest_run_ids": [1]}]}, {"strategies": [{"slot": 1}]})
    assert ok == []


def test_review_server_runs_backtests_in_the_weekly_review(lab, tmp_path):
    conn, h, _ = lab
    from types import SimpleNamespace
    from arena.mcp_server import ReviewServer
    db = tmp_path / "arena.db"
    disk = connect(db)
    from arena import ledger
    ledger.create_agent(disk, "claude", 1000.0, "2024-01-02")
    disk.close()
    (tmp_path / "in.json").write_text(json.dumps({"strategies": [{"slot": 1}], "agent": "claude"}))
    srv = ReviewServer(SimpleNamespace(review="weekly", review_input=str(tmp_path / "in.json"),
                                       result=str(tmp_path / "r.json"), db=str(db), agent="claude"))
    assert [t["name"] for t in srv.list_tools()] == ["submit_review", "run_backtest"]
    text, err = srv.call("run_backtest", {"idea_key": "up-drift", "code": CHALLENGER_CODE, "purpose": "design",
                                          "start": "2017-01-02", "end": "2017-06-30"})
    out = json.loads(text)
    assert not err and out["ok"] and out["budget_left_this_week"] == labconfig.get().evidence.backtest_budget - 1
