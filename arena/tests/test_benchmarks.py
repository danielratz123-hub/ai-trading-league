"""Benchmark players: bench_spy is scored like an agent but is never an agent."""
from __future__ import annotations

import pytest

from arena import benchmarks, league
from arena.learning import scores

from .conftest import DAYS, set_price
from .learning_helpers import league_week1, prices

EXEC, END = "2024-01-08", "2024-01-16"
SPY_PATH = [100, 100, 110, 99, 105, 105]      # closes 8th..12th (the 15th is a holiday)


def _spy(conn, end_open=108.0):
    set_price(conn, "SPY", EXEC, 100, 100)
    for d, c in zip(["2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12"], SPY_PATH[1:]):
        set_price(conn, "SPY", d, c, c)
    set_price(conn, "SPY", EXEC, 100, 100)
    set_price(conn, "SPY", END, end_open, end_open)


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM benchmark_scores ORDER BY week_no, player_id")]


def _league(conn, week_no=1):
    prices(conn)
    _spy(conn)
    league_week1(conn, "claude")
    league_week1(conn, "gpt")
    if week_no != 1:
        conn.execute("UPDATE weeks SET week_no=?", (week_no,))


def test_spy_return_and_score_match_formula(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    _spy(conn)
    out = benchmarks.score_benchmarks(conn, 5, EXEC, END)["bench_spy"]
    path = [100, 100, 110, 99, 105, 105, 108]
    dd = scores.max_drawdown_pct(path)
    assert out["mode"] == "risk_adjusted"
    assert out["return_pct"] == pytest.approx(8.0)
    assert out["max_drawdown_pct"] == pytest.approx(dd) and dd == pytest.approx(10.0)
    assert out["score"] == pytest.approx(scores.score(8.0, dd, False)) and out["score"] == pytest.approx(0.8)
    row = conn.execute("SELECT * FROM benchmark_scores WHERE week_no=5 AND player_id='bench_spy'").fetchone()
    assert row["week_start"] == EXEC and row["score"] == pytest.approx(0.8)


def test_raw_return_mode(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    prices(conn)
    _spy(conn)
    out = benchmarks.score_benchmarks(conn, 5, EXEC, END)["bench_spy"]
    assert out["mode"] == "raw_return" and out["score"] == pytest.approx(8.0)


def test_scoring_hook_leaves_agents_alone(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    _league(conn)

    agents_before = [tuple(r) for r in conn.execute("SELECT * FROM agents")]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("trades", "pending_orders", "agents")}
    league._mark_and_score(conn, END)
    names = [r[0] for r in conn.execute("SELECT agent_id FROM agents ORDER BY agent_id")]
    assert [r["player_id"] for r in conn.execute("SELECT player_id FROM benchmark_scores")] == ["bench_spy"]
    assert names == ["claude", "gpt"]
    assert [tuple(r) for r in conn.execute("SELECT * FROM agents")] == agents_before
    for t, n in counts.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE agent_id LIKE 'bench%'").fetchone()[0] == 0


def test_agent_points_identical_with_and_without_benchmarks(monkeypatch):
    from arena.db import connect
    from arena.trading_calendar import store_days
    results = []
    for enabled in (False, True):
        c = connect(":memory:")
        store_days(c, [(d, "09:30", "16:00") for d in DAYS])
        prices(c)
        _spy(c)
        league_week1(c, "claude")
        league_week1(c, "gpt", {1: ("w", 1.0, {"AAPL": 0.5, "SPY": 0.5})})
        set_price(c, "AAPL", END, 130)
        monkeypatch.setattr(benchmarks, "PLAYER_IDS", ["bench_spy"] if enabled else [])
        league._mark_and_score(c, END)
        results.append([tuple(r) for r in c.execute("SELECT week_no, agent_id, weekly_return, point FROM scores")])
        c.close()
    assert results[0] == results[1] and results[0]


def test_twice_gives_same_rows_and_backfill_is_repeatable(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    _league(conn)
    league._mark_and_score(conn, END)
    first = _rows(conn)
    assert len(first) == 1
    benchmarks.score_benchmarks(conn, 1, EXEC, END)
    assert _rows(conn) == first
    conn.execute("DELETE FROM benchmark_scores")
    assert benchmarks.backfill_benchmarks(conn) == 1
    assert _rows(conn) == first
    assert benchmarks.backfill_benchmarks(conn) == 1
    assert _rows(conn) == first


def test_missing_price_does_not_crash_scoring(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    _league(conn)
    conn.execute("DELETE FROM prices WHERE ticker='SPY' AND date=?", (END,))
    assert benchmarks.score_benchmarks(conn, 1, EXEC, END) == {}
    league._mark_and_score(conn, END)                       # no SPY open at the mark -> skipped, no raise
    assert _rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2


def test_benchmark_failure_never_breaks_scoring(conn, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    _league(conn)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(benchmarks, "score_benchmarks", boom)
    league._mark_and_score(conn, END)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM events_log WHERE source='benchmarks' AND level='error'").fetchone()[0] == 1
