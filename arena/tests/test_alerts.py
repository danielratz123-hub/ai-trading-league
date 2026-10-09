"""Telegram alerts: delivered when configured, throttled when repeated,
and never able to break a job or leak the bot token."""
import logging
from datetime import timedelta

import pytest

from arena import alerts


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "_state_file", lambda: tmp_path / "alerts-state.json")
    monkeypatch.setattr(alerts, "_sender", None)
    return tmp_path


def test_not_configured_means_log_only(state, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert alerts.send("hello") is False


def test_throttling_by_key(state, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_sender", lambda m: sent.append(m) or True)
    assert alerts.send("login broken", key="k", every=timedelta(hours=2))
    assert not alerts.send("login broken", key="k", every=timedelta(hours=2))   # throttled
    assert alerts.send("other", key="k2")
    assert alerts.send("no key") and alerts.send("no key")                       # no key: always sent
    assert len(sent) == 4 and "AI Trading League" in sent[0]


def test_network_failure_never_raises_and_never_logs_the_token(state, monkeypatch, caplog):
    import httpx
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SUPER-SECRET-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(url, **kw):
        raise httpx.ConnectError(f"could not reach {url}")      # the URL contains the token

    monkeypatch.setattr(httpx, "post", boom)
    with caplog.at_level(logging.DEBUG):
        assert alerts.send("job failed", level="error") is False
    assert "SUPER-SECRET-TOKEN" not in caplog.text
    assert "ConnectError" in caplog.text


def test_telegram_request_shape(state, monkeypatch):
    import httpx
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = []

    class Resp:
        status_code = 200

    def post(url, json, timeout):
        calls.append((url, json, timeout))
        return Resp()

    monkeypatch.setattr(httpx, "post", post)
    assert alerts.send("x" * 10_000)
    url, body, timeout = calls[0]
    assert url == "https://api.telegram.org/bott0k/sendMessage" and body["chat_id"] == "42"
    assert len(body["text"]) <= alerts.MAX_CHARS and timeout <= 10


def test_broken_state_file_does_not_stop_alerts(state, monkeypatch):
    (state / "alerts-state.json").write_text("{not json", encoding="utf-8")
    sent = []
    monkeypatch.setattr(alerts, "_sender", lambda m: sent.append(m) or True)
    assert alerts.send("still works", key="k")


# ---------------------------------------------------------------- where alerts are sent from

@pytest.fixture
def outbox(state, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_sender", lambda m: sent.append(m) or True)
    return sent


def test_failed_and_retried_jobs_alert(outbox, tmp_path):
    from arena.db import connect
    from arena.scheduler import MAX_ATTEMPTS, TEMPFAIL, Job, _alert_job
    conn = connect(tmp_path / "jobs.db")
    job = Job("daily", "2024-03-11", "2024-03-11")

    def ran(code):
        conn.execute("INSERT INTO job_runs(job, key, started_at, exit_code) VALUES (?,?,?,?)",
                     (job.name, job.key, "2024-03-11T16:45", code))
        _alert_job(conn, job, code, "Traceback...\nValueError: boom")

    ran(1)
    assert "FAILED" in outbox[-1] and "Will retry" in outbox[-1] and "boom" in outbox[-1]
    ran(0)
    assert "succeeded on retry 1" in outbox[-1]
    n = len(outbox)
    ran(TEMPFAIL)                       # login problem: decide already alerted
    assert len(outbox) == n
    for _ in range(MAX_ATTEMPTS):
        ran(2)
    assert "Gave up" in outbox[-1]


def test_nudged_and_failed_turns_alert(outbox):
    from arena.league import _alert_turn
    _alert_turn("gpt", 3, True, None, nudged=True, rejected=2, last_errors=["NVDA over 15% short"])
    assert any("rejected 2 time(s)" in m and "NVDA" in m for m in outbox)
    assert any("needed a nudge" in m for m in outbox)
    _alert_turn("claude", 3, False, "timed out after 1800s", nudged=False, rejected=0, last_errors=[])
    assert "FAILED" in outbox[-1] and "timed out" in outbox[-1]
    n = len(outbox)
    _alert_turn("claude", 3, False, "x", nudged=False, rejected=0, last_errors=[], simulate=True)
    assert len(outbox) == n                                     # practice runs stay quiet


def test_execute_summary(outbox, tmp_path):
    import argparse
    import json

    import pandas as pd

    from arena import league, ledger, prices
    from arena.db import connect
    from arena.ledger import SlotTarget
    from arena.main import _execute_summary
    from arena.trading_calendar import store_days

    from .test_tools import _market
    conn = connect(tmp_path / "x.db")
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    d = "2024-04-01"
    conn.execute("DELETE FROM prices WHERE date >= ?", (d,))
    for a in ("claude", "gpt"):
        ledger.create_agent(conn, a, 1000, "2024-03-28")
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (1, '2024-03-28', ?, 'decided')", (d,))
    conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload) VALUES "
                 "('claude', '2024-03-28', ?, 'rebalance', ?)",
                 (d, json.dumps([SlotTarget(1, 1.0, {"MSFT": 0.25}).__dict__])))
    prices.store_opening_prices(conn, pd.DataFrame({"ticker": ["SPY", "MSFT"], "open": [500.0, 400.0]}), d, "test")
    rep = league.run_open(conn, d)
    _execute_summary(conn, d, rep, argparse.Namespace(dry_run=False))
    msg = outbox[-1]
    assert "Execute 2024-04-01" in msg and "claude: 1 trade(s)" in msg and "gpt: 0 trade(s)" in msg
    assert "equity $" in msg


def test_mirror_errors_alert(outbox, monkeypatch, tmp_path):
    import argparse

    from arena import mirror
    from arena.db import connect
    from arena.main import _maybe_mirror
    monkeypatch.setattr(mirror, "mode", lambda: "combined")

    def boom(conn, d):
        raise ConnectionError("Alpaca is down")
    monkeypatch.setattr(mirror, "sync_all", boom)
    _maybe_mirror(connect(tmp_path / "m.db"), "2024-04-01", argparse.Namespace(dry_run=False))
    assert "mirror problem" in outbox[-1] and "Alpaca is down" in outbox[-1]
