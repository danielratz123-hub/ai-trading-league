"""Operations: bootstrap of a running league, the scheduler's new jobs, the
collector, the dashboard endpoint and the command-line dry run."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from arena import league, scheduler
from arena.learning import bootstrap, collector, strategies as strat
from arena.prices import ET
from arena.scheduler import LearningTimes, due_jobs

from .fakes import Factory, manual, submit
from .test_tools import _db

WEEK1, WEEK2 = "2024-01-05", "2024-01-12"


# --------------------------------------------------------------------------
# Bootstrap: an existing league (decided with the layer OFF) is migrated
# --------------------------------------------------------------------------

def test_bootstrap_migrates_a_running_league(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    conn = _db()
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"AAPL": 0.5, "MSFT": 0.5}, name="tech")])]],
                            [[submit([manual(1, 0.5, {"AAPL": 0.5, "XOM": 0.5}, name="tech"),
                                      manual(2, 0.3, {"TLT": 0.5, "GLD": 0.5}, name="bonds")])]]],
                 "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    for d in ("2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12"):
        league.daily(conn, d)
    league.decide(conn, WEEK2, factory=f)
    league.execute(conn)                     # scores week 1
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0     # layer was off

    monkeypatch.setenv("LEARNING_ENABLED", "1")
    out = bootstrap.run(conn, vix_fetch=None)
    assert out["decisions"] > 0 and out["weekly_scores"] == 1
    assert bootstrap.run(conn, vix_fetch=None) == {"skipped": "already bootstrapped"}
    s1, s2 = strat.in_slot(conn, "claude", 1), strat.in_slot(conn, "claude", 2)
    assert (s1.name, s2.name) == ("tech", "bonds")
    kinds = {r[0] for r in conn.execute("SELECT change_kind FROM strategy_versions")}
    assert kinds == {"initial"}                                   # no cooldown starts
    acts = [(r["week_no"], r["action"], r["symbol"]) for r in conn.execute(
        "SELECT week_no, action, symbol FROM decisions WHERE agent_id='claude' ORDER BY id")]
    assert (1, "buy", "AAPL") in acts and (2, "sell", "MSFT") in acts and (2, "buy", "TLT") in acts
    linked = conn.execute("SELECT COUNT(*) FROM trades WHERE decision_id IS NOT NULL").fetchone()[0]
    assert linked == conn.execute("SELECT COUNT(*) FROM trades WHERE reason='rebalance'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] > 0
    ws = conn.execute("SELECT * FROM weekly_scores WHERE week_no=1 AND agent_id='claude'").fetchone()
    assert ws["mode"] == "raw_return"
    # The prompt the agent got is the stored input.
    sha = json.loads(conn.execute("SELECT input_snapshot FROM decisions LIMIT 1").fetchone()[0])["sha256"]
    assert "Week 1" in conn.execute("SELECT content FROM decision_inputs WHERE sha256=?", (sha,)).fetchone()[0]


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

LT = LearningTimes((10, 0), 60, (15, 30), (16, 30), 5, (12, 0))
CAL = {"2026-10-09": "16:00", "2026-10-12": "16:00", "2026-11-27": "13:00"}


def at(d, h, m):
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET)


def names(jobs):
    return [(j.name, j.key) for j in jobs]


def test_risk_check_times():
    assert LT.risk_times((16, 0)) == [(10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (15, 30)]
    assert LT.risk_times((13, 0)) == [(10, 0), (11, 0), (12, 0), (12, 30)]


def test_risk_checks_only_near_their_time():
    jobs = due_jobs(at("2026-10-12", 11, 5), CAL, {}, True, LT)
    assert ("risk-check", "risk-2026-10-12-1100") in names(jobs)
    assert not any(j.name == "risk-check" for j in due_jobs(at("2026-10-12", 11, 45), CAL, {}, True, LT))
    assert not any(j.name == "risk-check" for j in due_jobs(at("2026-10-12", 11, 5), CAL, {}, True, None))


def test_daily_review_waits_for_daily():
    now = at("2026-10-12", 16, 50)
    assert ("daily-review", "daily-review-2026-10-12") not in names(due_jobs(now, CAL, {}, True, LT))
    hist = {("daily", "2026-10-12"): [{"exit_code": 0, "started_at": at("2026-10-12", 16, 45)}]}
    assert ("daily-review", "daily-review-2026-10-12") in names(due_jobs(now, CAL, hist, True, LT))


def test_weekly_review_before_decide():
    sat_10 = at("2026-10-10", 10, 0)
    assert ("decide", "2026-10-09") in names(due_jobs(sat_10, CAL, {}, True, None))     # layer off: as before
    assert not any(j.name == "decide" for j in due_jobs(sat_10, CAL, {}, True, LT))
    sat_12 = at("2026-10-10", 12, 0)
    jobs = names(due_jobs(sat_12, CAL, {}, True, LT))
    assert ("weekly-review", "weekly-review-2026-10-09") in jobs and not any(n == "decide" for n, _ in jobs)
    done = {("weekly-review", "weekly-review-2026-10-09"): [{"exit_code": 0, "started_at": sat_12}]}
    jobs = names(due_jobs(at("2026-10-10", 12, 20), CAL, done, True, LT))
    assert ("decide", "2026-10-09") in jobs and not any(n == "weekly-review" for n, _ in jobs)
    # A review that keeps failing doesn't block decide for more than the grace period.
    failing = {("weekly-review", "weekly-review-2026-10-09"): [{"exit_code": 1, "started_at": at("2026-10-10", 15, 30)}]}
    assert not any(j.name == "decide" for j in due_jobs(at("2026-10-10", 15, 45), CAL, failing, True, LT))
    assert ("decide", "2026-10-09") in names(due_jobs(at("2026-10-10", 16, 5), CAL, failing, True, LT))


def test_job_argv_passes_the_date():
    j = scheduler.Job("weekly-review", "weekly-review-2026-10-09", "2026-10-10")
    assert j.argv("x.db")[-2:] == ["--date", "2026-10-10"]


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------

def test_collector_stores_minute_bars_and_reads_fresh_prices(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    seen = {}

    def fetch(symbols, start, end):
        seen["symbols"] = symbols
        return pd.DataFrame([{"symbol": "SPY", "timestamp": pd.Timestamp("2024-01-10 15:59", tz="UTC"),
                              "open": 470.0, "high": 471.0, "low": 469.5, "close": 470.5, "volume": 1200, "vwap": 470.2}])
    now = datetime.fromisoformat("2024-01-10T16:01:00+00:00")
    assert collector.collect_minute(conn, fetch, now) == 1
    assert {"SPY", "VIXY", "XLK", "XLRE"} <= set(seen["symbols"])
    assert collector.latest_minute_price(conn, "SPY", "2024-01-10T16:05:00+00:00")[0] == 470.5
    assert collector.latest_minute_price(conn, "SPY", "2024-01-10T17:00:00+00:00") is None     # stale
    assert collector.prune_minute_bars(conn, datetime.fromisoformat("2024-03-01T00:00:00+00:00")) == 1


def test_vix_csv_parsing(monkeypatch):
    import io
    csv = "DATE,OPEN,HIGH,LOW,CLOSE\n10/08/2026,16.1,17.0,15.9,16.4\n10/09/2026,16.4,18.2,16.0,17.9\n"

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=20: R(csv.encode()))
    assert collector.fetch_vix_history("https://example.invalid/vix.csv") == {"2026-10-08": 16.4, "2026-10-09": 17.9}


# --------------------------------------------------------------------------
# Dashboard endpoint
# --------------------------------------------------------------------------

def test_api_learning_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from arena.api.app import create_app
    from arena.db import connect
    db = tmp_path / "arena.db"
    connect(db).close()
    monkeypatch.setenv("ARENA_DB", str(db))
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    body = TestClient(create_app()).get("/api/learning").json()
    assert body["available"] is True and body["start_week"] == 5
    assert set(body) >= {"weekly_scores", "strategy_by_regime", "reports", "lessons", "versions", "shadow", "rejected"}


# --------------------------------------------------------------------------
# Command line: the dry run never changes the real database
# --------------------------------------------------------------------------

def test_review_dry_run_cli(tmp_path, monkeypatch, capsys):
    from arena import main as cli
    from arena.db import connect
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    db = tmp_path / "arena.db"
    src = _db()
    dst = connect(db)
    src.backup(dst)
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"AAPL": 0.5, "MSFT": 0.5}, name="tech")])]]],
                 "gpt": [[[submit([])]]]})
    league.decide(dst, WEEK1, factory=f)
    league.execute(dst)
    league.daily(dst, "2024-01-08")
    before = dst.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    dst.close()
    monkeypatch.setattr("arena.learning.collector.fetch_vix_history", lambda *a, **k: {})
    monkeypatch.setattr("arena.learning.collector.fetch_headlines", lambda *a, **k: [])
    cli.main(["review-dry-run", "--db", str(db), "--fake-agents"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and '"status": "ok"' in out and "dry run: worked on a throwaway copy" in out
    after = connect(db)
    assert after.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == before == 0
    assert after.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
