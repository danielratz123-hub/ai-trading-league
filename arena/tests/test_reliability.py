"""Reliability features: API-error labels, the daily heartbeat, Alpaca
reconciliation, the per-day usage alert, SPY per week, and Drive backups."""
import argparse
import gzip
import shutil
from datetime import datetime, timezone

import pytest

from arena import alerts, mirror
from arena.db import connect

from .test_scheduler import names


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_sender", lambda m: sent.append(m) or True)
    return sent


def test_api_errors_are_named():
    assert alerts.api_source("alpaca.common.exceptions.APIError: 401 Authorization Required") == "Alpaca"
    assert alerts.api_source("Claude Code: API Error: 529 Overloaded") == "Anthropic (Claude)"
    assert alerts.api_source('Codex error: {"message": "rate limited"}') == "OpenAI (ChatGPT)"
    assert alerts.api_source("ValueError: boom") is None


def test_scheduler_runs_reconcile_and_heartbeat():
    assert ("reconcile", "reconcile-2024-03-11") not in names("2024-03-11 16:59")
    assert ("reconcile", "reconcile-2024-03-11") in names("2024-03-11 17:00")
    assert not any(j == "reconcile" for j, _ in names("2024-03-09 17:00"))          # Saturday: no trading
    assert ("heartbeat", "heartbeat-2024-03-09") in names("2024-03-09 18:00")      # every day
    assert ("heartbeat", "heartbeat-2024-03-11") not in names("2024-03-11 17:59")


def test_compare_positions_allows_only_whole_share_rounding_of_shorts():
    expected = {"AMD": 29.0054, "XLRE": -124.3828, "NVDA": 32.6983}
    assert mirror.compare_positions({"AMD": 29.0054, "XLRE": -124.0, "NVDA": 32.6983}, expected) == []
    bad = mirror.compare_positions({"AMD": 30.0, "XLRE": -125.0, "TSLA": 3.0}, expected)
    assert [m["ticker"] for m in bad] == ["AMD", "NVDA", "TSLA", "XLRE"]


class FakeBroker:
    def __init__(self, pos):
        self.pos = pos

    def positions(self):
        return dict(self.pos)


def test_reconcile_command_alerts_on_mismatch(tmp_path, monkeypatch, outbox):
    from arena import ledger
    from arena.main import cmd_reconcile
    conn = connect(tmp_path / "a.db")
    ledger.create_agent(conn, "claude", 1000, "2024-01-02")
    ledger.create_agent(conn, "gpt", 1000, "2024-01-02")
    conn.executemany("INSERT INTO mirror_holdings(agent_id, ticker, qty) VALUES (?,?,?)",
                     [("claude", "AMD", 9.29), ("gpt", "AMD", 19.71), ("claude", "XLRE", -124.38)])
    monkeypatch.setenv("MIRROR_MODE", "combined")
    real = mirror.reconcile
    monkeypatch.setattr(mirror, "reconcile", lambda c: real(c, FakeBroker({"AMD": 29.0, "XLRE": -124.0})))
    cmd_reconcile(conn, argparse.Namespace())
    assert not outbox                                                   # matches (with rounding)
    monkeypatch.setattr(mirror, "reconcile", lambda c: real(c, FakeBroker({"AMD": 20.0, "XLRE": -124.0})))
    cmd_reconcile(conn, argparse.Namespace())
    assert "MISMATCH" in outbox[-1] and "AMD" in outbox[-1]
    assert conn.execute("SELECT COUNT(*) FROM events_log WHERE source='reconcile' AND level='error'").fetchone()[0] == 1
    monkeypatch.setenv("MIRROR_MODE", "off")
    assert mirror.reconcile(conn)["checked"] is False


def test_heartbeat_message(tmp_path, outbox):
    from arena.main import cmd_heartbeat
    db = tmp_path / "a.db"
    conn = connect(db)
    conn.execute("INSERT INTO job_runs(job, key, started_at, finished_at, exit_code) VALUES "
                 "('daily', 'x', ?, ?, 1)", (datetime.now(timezone.utc).isoformat(),) * 2)
    cmd_heartbeat(conn, argparse.Namespace(db=str(db)))
    assert outbox and outbox[-1].count("Still alive") == 1 and "failures in: daily" in outbox[-1]


def test_daily_usage_alert_never_pauses(tmp_path, outbox, monkeypatch):
    from arena import league
    from arena.api.queries import daily_costs
    conn = connect(tmp_path / "a.db")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO agent_turns(week_no, agent_id, model, status, input_tokens, output_tokens, started_at) "
                 "VALUES (1, 'claude', 'claude-opus-5-5', 'accepted', 2000000, 400000, ?)", (now,))
    day = daily_costs(conn)[0]
    cost = day["agents"]["claude"]["est_ai_cost_usd"]
    assert cost == pytest.approx((2_000_000 * 4 + 400_000 * 20) / 1e6)    # $16 at the config's rates
    import dataclasses
    monkeypatch.setattr(league, "SETTINGS", dataclasses.replace(league.SETTINGS, daily_ai_cost_alert_usd=10))
    league.check_daily_cost(conn, "claude")
    assert "Usage alert" in outbox[-1] and "NOT paused" in outbox[-1]
    league.check_daily_cost(conn, "claude")
    assert len(outbox) == 1                                             # once per agent per day
    assert conn.execute("SELECT value FROM league_state WHERE key='paused'").fetchone() is None


def test_spy_benchmark_per_scored_week(tmp_path):
    from arena import league
    from arena.api.queries import spy_week_return, weeks
    from arena.prices import get_price
    from arena.trading_calendar import store_days

    from .fakes import Factory, manual, submit
    from .test_tools import _market
    conn = connect(tmp_path / "a.db")
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    f = Factory({a: [[[submit([manual(1, 1.0, {"SPY": 0.2})])]]] * 2 for a in ("claude", "gpt")})
    league.simulate(conn, "2024-01-02", 2, factory=f, progress=lambda _: None)
    w = {x["week"]: x for x in weeks(conn)}
    e1, e2 = w[1]["exec_date"], w[2]["exec_date"]
    want = get_price(conn, "SPY", e2, "open") / get_price(conn, "SPY", e1, "open") - 1
    assert w[1]["spy_return"] == pytest.approx(want, abs=1e-6) == spy_week_return(conn, 1)


def test_backup_keeps_30_days_only_and_includes_config_without_secrets(tmp_path, monkeypatch):
    from datetime import timedelta

    from arena import backup, ledger
    from arena.prices import ET
    path = tmp_path / "data" / "arena.db"
    path.parent.mkdir()
    conn = connect(path)
    ledger.create_agent(conn, "claude", 1000, "2024-01-02")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "should-never-be-backed-up")
    store = backup.DirStorage(tmp_path / "drive")
    start = datetime(2026, 1, 1, 17, 30, tzinfo=ET)
    for i in range(35):
        backup.run_backup(path, store, prefix="p/", keep_daily=30, keep_monthly=0, now=start + timedelta(days=i))
    daily = store.list("p/daily/")
    assert sum(n.endswith(".db.gz") for n in daily) == 30 and sum(n.endswith(".json") for n in daily) == 30
    assert store.list("p/monthly/") == []                               # monthly copies off
    cfg = (store.root / daily[-1]).read_text() if daily[-1].endswith(".json") else \
        (store.root / [n for n in daily if n.endswith(".json")][-1]).read_text()
    assert "claude-opus-5-5" in cfg and "should-never-be-backed-up" not in cfg
    # Restore picks the newest DATABASE, never a config file.
    out = tmp_path / "restored.db"
    name = backup.fetch_backup("latest", out, path, store, prefix="p/")
    assert name.endswith("arena-2026-02-04.db.gz")


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone isn't installed here (it is in the Docker image)")
def test_rclone_storage_round_trip(tmp_path):
    from arena import backup
    remote = backup.RcloneStorage(str(tmp_path / "remote"))
    src = tmp_path / "x.db.gz"
    src.write_bytes(gzip.compress(b"hello"))
    remote.put(src, "p/daily/arena-2026-01-01.db.gz")
    assert remote.list("p/daily/") == ["p/daily/arena-2026-01-01.db.gz"]
    assert remote.list("p/monthly/") == []
    back = tmp_path / "back.gz"
    remote.get("p/daily/arena-2026-01-01.db.gz", back)
    assert gzip.decompress(back.read_bytes()) == b"hello"
    remote.delete("p/daily/arena-2026-01-01.db.gz")
    assert remote.list("p/daily/") == []
