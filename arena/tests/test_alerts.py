"""Telegram alerts: delivered when configured, throttled when repeated,
and never able to break a job or leak the bot token."""
import logging
from datetime import timedelta

import pytest

from arena import alerts


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_DB", str(tmp_path / "arena.db"))
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
