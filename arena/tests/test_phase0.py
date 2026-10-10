"""ROADMAP Phase 0: kill switch, guardrails, alerts, structured logs, outside
monitoring, staging and the retrying job runner."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

from arena import alerts, health, league, ledger, logs, scheduler
from arena.db import connect
from arena.learning import execution, hardlimits, jobs, strategies as strat

from .fakes import Factory, manual, submit
from .learning_helpers import fixed_runner, league_week1, minute, prices, set_price, utc
from .test_tools import _db


@pytest.fixture
def sent(monkeypatch):
    """Every Telegram message, captured."""
    out = []
    monkeypatch.setattr(alerts, "_sender", lambda msg: out.append(msg) or True)
    return out


# --------------------------------------------------------------------------
# 0.4 Kill switch
# --------------------------------------------------------------------------

def test_kill_switch_per_agent_stops_its_orders_only(monkeypatch, sent):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    conn = _db()
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"AAPL": 0.5, "MSFT": 0.5})])]]],
                 "gpt": [[[submit([manual(1, 0.5, {"XOM": 0.5, "JPM": 0.5})])]]]})
    league.decide(conn, "2024-01-05", factory=f)
    league.set_paused(conn, True, "gpt")                     # flipped after gpt decided
    assert any("gpt PAUSED" in m for m in sent)
    assert league.is_paused(conn, "gpt") and not league.is_paused(conn) and not league.is_paused(conn, "claude")
    league.execute(conn)                                     # the very next cycle
    assert ledger.positions(conn, "gpt") == [] and ledger.positions(conn, "claude")
    assert conn.execute("SELECT status, note FROM pending_orders WHERE agent_id='gpt'").fetchone()[1] \
        == "agent paused (kill switch)"
    # Next week: gpt gets no turn at all; claude plays on.
    f2 = Factory({"claude": [[[submit([manual(1, 0.4, {"AAPL": 0.5, "MSFT": 0.5})])]]], "gpt": [[[submit([])]]]})
    reports = {r.agent_id: r.status for r in league.decide(conn, "2024-01-12", factory=f2)}
    assert reports == {"claude": "accepted", "gpt": "paused"}
    league.set_paused(conn, False, "gpt")
    assert not league.is_paused(conn, "gpt") and any("gpt resumed" in m for m in sent)


def test_kill_switch_global_blocks_decide(conn):
    league.set_paused(conn, True)
    with pytest.raises(league.LeagueError, match="paused"):
        league.decide(conn, "2024-01-05", factory=Factory({}))
    assert league.is_paused(conn, "claude")                  # global covers every agent


def test_paused_agent_is_not_woken_and_its_tilt_is_skipped(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    league.set_paused(conn, True, "claude")
    for t in ("SPY", "MSFT", "XOM", "JPM", "KO", "PG", "VIXY"):
        minute(conn, t, "2024-01-10", 10, 59, 100.0)
    minute(conn, "AAPL", "2024-01-10", 10, 59, 90.0)         # stop-loss would fire
    out = jobs.risk_check(conn, "2024-01-10", fixed_runner(), now=utc("2024-01-10", 11, 0))
    assert out["fired"] and out["woken"] == [{"agent": "claude", "status": "not woken: paused (kill switch)"}]
    execution.queue(conn, "claude", "2024-01-10", "tilt", {"allocations": {"1": 0.45, "2": 0.35}}, "t")
    execution.run_open(conn, "2024-01-11", weekly_executed=False)
    assert "agent paused" in conn.execute("SELECT note FROM pending_orders WHERE kind='tilt'").fetchone()[0]


# --------------------------------------------------------------------------
# 0.3 Guardrails: daily loss limit, stale analysis (the rest: test_learning_limits.py)
# --------------------------------------------------------------------------

def _order(conn, **kw):
    # SPY: not held yet, so a small buy is far below every size limit.
    base = dict(agent_id="claude", slot=1, ticker="SPY", delta=0.1, price=100.0, d="2024-01-10", reason="emergency",
                held_shares=0.0, slot_equity=500.0, has_stop=True)
    return hardlimits.Order(**{**base, **kw})


def test_daily_loss_limit_blocks_adding_but_not_reducing(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    eq = ledger.value_agent(conn, "claude", "2024-01-09", "close").equity
    loss = hardlimits.day_loss(conn, "claude", "2024-01-10", eq * 0.93)
    assert loss == pytest.approx(0.07)
    errs = hardlimits.check_order(conn, _order(conn, day_loss=loss))
    assert any("daily loss limit" in e for e in errs)
    held = ledger.position_shares(conn, "claude", 1, "AAPL")
    assert hardlimits.check_order(conn, _order(conn, ticker="AAPL", held_shares=held, delta=-0.1, day_loss=loss)) == []
    assert hardlimits.check_order(conn, _order(conn, day_loss=0.02)) == []


def test_price_moved_since_analysis_refuses(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    errs = hardlimits.check_order(conn, _order(conn, price=110.0, analysis_price=100.0))
    assert any("analysis is stale" in e for e in errs)
    assert hardlimits.check_order(conn, _order(conn, price=105.0, analysis_price=100.0)) == []


def test_stale_analysis_through_the_order_layer(conn, monkeypatch, sent):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    set_price(conn, "XOM", "2024-01-11", 115.0)              # gapped +15% overnight
    val = ledger.value_agent(conn, "claude", "2024-01-11", "open")
    targets = execution.current_targets(conn, "claude", val)
    for t in targets:
        if t.slot == 2:
            t.weights = {**t.weights, "XOM": 0.2}
            t.risk_orders = {**t.risk_orders, "XOM": {"stop_loss": 100.0}}
    check = execution.order_check(conn, "claude", "2024-01-11", targets, val.equity, "emergency", "test",
                                  analysis_date="2024-01-10")
    res = ledger.rebalance(conn, "claude", "2024-01-11", targets, reason="emergency", order_check=check)
    assert any("XOM" in s for s in res.skipped)
    assert any("Guardrail refused an order" in m and "stale" in m for m in sent)     # 0.5: alert


# --------------------------------------------------------------------------
# 0.5 Alerts
# --------------------------------------------------------------------------

def test_drawdown_limit_alert(conn, monkeypatch, sent):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    league_week1(conn, through="2024-01-09")
    for t in ("SPY", "AAPL", "MSFT", "XOM", "JPM", "KO", "PG", "VIXY"):
        minute(conn, t, "2024-01-10", 10, 59, 87.0)
    jobs.risk_check(conn, "2024-01-10", None, now=utc("2024-01-10", 11, 0))
    assert any("Drawdown limit hit" in m for m in sent)
    assert strat.in_slot(conn, "claude", 1).emergency_flagged_at


# --------------------------------------------------------------------------
# 0.7 A failing review job retries and alerts
# --------------------------------------------------------------------------

def test_failed_review_job_retries_then_gives_up_with_alerts(tmp_path, sent):
    from arena.trading_calendar import store_days
    db = tmp_path / "arena.db"
    c = connect(db)
    store_days(c, [("2024-01-10", "09:30", "16:00")])
    ledger.create_agent(c, "claude", 1000.0, "2024-01-10")
    c.close()
    sched = connect(db)
    job = scheduler.Job("daily-review", "daily-review-2024-01-10", "2024-01-10")
    assert scheduler.run_job(sched, job, tmp_path, db) != 0            # no `daily` for that day: fails
    assert any("daily-review" in m and "FAILED" in m and "retry in 30 minutes" in m for m in sent)
    assert [r["exit_code"] for r in scheduler._history(sched)[("daily-review", job.key)]] == [1]
    lt = scheduler.LearningTimes((10, 0), 60, (15, 30), (16, 30), 5, (12, 0))
    t0 = datetime.fromisoformat("2024-01-10T16:50").replace(tzinfo=scheduler.ET)
    hist = {("daily", "2024-01-10"): [{"exit_code": 0, "started_at": t0}],
            ("daily-review", job.key): [{"exit_code": 1, "started_at": t0}]}
    due = lambda now: [j.name for j in scheduler.due_jobs(now, {"2024-01-10": "16:00"}, hist, True, lt)]  # noqa: E731
    assert "daily-review" not in due(t0 + timedelta(minutes=10))       # waits ...
    assert "daily-review" in due(t0 + timedelta(minutes=31))           # ... then retries
    for _ in range(2):
        scheduler.run_job(sched, job, tmp_path, db)
    assert any("Gave up after 3 attempts" in m for m in sent)
    # Its whole output is in the JSON log under one run_id per attempt.
    lines = [json.loads(x) for f in tmp_path.glob("*.jsonl") for x in f.read_text().splitlines()]
    runs = {x["run_id"] for x in lines if x.get("job") == "daily-review"}
    assert len(runs) == 3 and all(r.startswith("job-") for r in runs)
    assert any(x.get("event") == "job_finished" and x["exit_code"] != 0 for x in lines)


# --------------------------------------------------------------------------
# 0.8 Structured logs
# --------------------------------------------------------------------------

def test_json_log_lines_carry_run_id_agent_and_model(capsys, monkeypatch):
    monkeypatch.setenv("ARENA_LOG_JSON", "1")
    logs.setup(logging.WARNING)
    try:
        with logs.bind(run_id="job-42", agent="claude", model="claude-opus-5-5"):
            logging.getLogger("arena.review").info("review daily started")
        logging.getLogger("arena.other").warning("outside")
    finally:
        monkeypatch.delenv("ARENA_LOG_JSON")
        logs.setup(logging.WARNING)
    a, b = [json.loads(x) for x in capsys.readouterr().err.strip().splitlines()]
    assert (a["run_id"], a["agent"], a["model"], a["msg"]) == ("job-42", "claude", "claude-opus-5-5", "review daily started")
    assert b["run_id"] != "job-42" and "agent" not in b


# --------------------------------------------------------------------------
# 0.9 Outside monitoring
# --------------------------------------------------------------------------

def test_outside_ping_reports_ok_and_failures(tmp_path):
    db = tmp_path / "arena.db"
    connect(db).close()
    stop = __import__("threading").Event()
    health.start_heartbeat(db, stop)
    calls = []
    try:
        assert health.ping_once("https://hc.example/abc", db, None, post=lambda u, b: calls.append((u, b)))
        assert not health.ping_once("https://hc.example/abc", db, "Alpaca not reachable: x",
                                    post=lambda u, b: calls.append((u, b)))
    finally:
        stop.set()
    assert calls[0] == ("https://hc.example/abc", "ok")
    assert calls[1][0] == "https://hc.example/abc/fail" and "Alpaca" in calls[1][1]
    assert health.start_outside_ping(db, stop, "") is None             # not configured: nothing runs


# --------------------------------------------------------------------------
# 0.6 Staging
# --------------------------------------------------------------------------

def test_staging_replays_a_day_on_a_copy(tmp_path, monkeypatch):
    from arena import staging
    from arena.learning.jobs import fake_runner
    monkeypatch.setenv("MIRROR_MODE", "combined")
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    live = tmp_path / "arena.db"
    src = _db()
    c = connect(live)
    src.backup(c)
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"AAPL": 0.5, "MSFT": 0.5})])]]], "gpt": [[[submit([])]]]})
    league.decide(c, "2024-01-05", factory=f)
    league.execute(c)
    league.daily(c, "2024-01-08")
    before = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    c.close()
    out = staging.run_day(live, tmp_path / "staging.db", "2024-01-09", fake_runner, week=True, log=lambda *_: None)
    assert len(out["risk_checks"]) == 7 and out["daily_review"]["reviews"]
    assert "weekly_review" in out
    import os
    assert os.environ["MIRROR_MODE"] == "off"                          # never the paper account
    c = connect(live)
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == before
    assert c.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0] == 0   # the live file is untouched
