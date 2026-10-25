"""Emergency switch tests: every broker is fake and all storage is temporary."""
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from arena import alerts, league, ledger, main, mirror
from arena.db import connect
from .conftest import DAYS, flat_prices
from .test_mirror import FakeBroker
from .test_risk import test_stop_loss_triggers_on_close_and_fills_next_open as stop_loss_case


@pytest.fixture
def halt_db(tmp_path, monkeypatch):
    path = tmp_path / "halt.sqlite"
    monkeypatch.setenv("ARENA_DB", str(path))
    conn = connect(path)
    for agent in ("claude", "gpt"):
        ledger.create_agent(conn, agent, 1000, DAYS[0])
    flat_prices(conn, "AAPL", 100)
    ledger.rebalance(conn, "claude", DAYS[1], [ledger.SlotTarget(1, 0.1, {"AAPL": 1.0})])
    monkeypatch.setenv("MIRROR_MODE", "combined")
    yield conn
    conn.close()


def fake_client():
    return Mock(get_account=Mock(return_value=SimpleNamespace(account_number="fake", equity=1000)),
                get_all_positions=Mock(return_value=[]),
                submit_order=Mock(return_value=SimpleNamespace(id="fake-order")),
                cancel_orders=Mock(return_value=[]))


@pytest.mark.parametrize("entry", ["submit", "sync", "sync_all", "sync_combined",
                                   "brokers_for_each", "check_accounts", "reconcile",
                                   "account_id", "equity", "positions"])
def test_every_entry_refuses_then_resumes(halt_db, monkeypatch, entry):
    conn = halt_db
    broker = mirror.AlpacaPaperBroker.__new__(mirror.AlpacaPaperBroker)
    broker.client = fake_client()
    monkeypatch.setattr(mirror, "keys_for", lambda a: ("fake-key", "fake-secret"))
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", lambda *a: broker)
    fake = FakeBroker(1000)
    calls = {
        "submit": lambda: broker.submit("AAPL", 1),
        "sync": lambda: mirror.sync(conn, DAYS[2], fake, agent="claude"),
        "sync_all": lambda: mirror.sync_all(conn, DAYS[2], brokers={"combined": fake}),
        "sync_combined": lambda: mirror.sync_combined(conn, DAYS[2], fake),
        "brokers_for_each": lambda: mirror.brokers_for_each(["claude"]),
        "check_accounts": lambda: mirror.check_accounts(["claude"]),
        "reconcile": lambda: mirror.reconcile(conn, fake),
        "account_id": broker.account_id, "equity": broker.equity, "positions": broker.positions,
    }
    league.set_halted(conn, True)
    with pytest.raises(mirror.MirrorHalted, match="halt is active"):
        calls[entry]()
    assert not broker.client.mock_calls
    league.resume(conn)
    calls[entry]()
    if entry in ("sync", "sync_all", "sync_combined"):
        assert fake.orders


@pytest.mark.parametrize("failure", ["missing", "schema", "invalid"])
def test_fail_closed(tmp_path, monkeypatch, failure):
    path = tmp_path / "unreadable.sqlite"
    monkeypatch.setenv("ARENA_DB", str(path))
    if failure != "missing":
        import sqlite3
        c = sqlite3.connect(path)
        if failure == "invalid":
            c.execute("CREATE TABLE league_state(key TEXT, value TEXT)")
            c.execute("INSERT INTO league_state VALUES ('halted', 'broken')")
        c.commit()
        c.close()
    broker = mirror.AlpacaPaperBroker.__new__(mirror.AlpacaPaperBroker)
    broker.client = fake_client()
    with pytest.raises(mirror.MirrorHalted, match="could not be read"):
        broker.submit("AAPL", 1)
    assert not broker.client.mock_calls
    if failure == "missing":
        assert not path.exists()


@pytest.mark.parametrize("bad", [None, "exception", "partial"])
def test_halt_cancels_every_account_and_alerts(halt_db, monkeypatch, bad):
    cancelled, sent = [], []
    def broker(agent, secret):
        class Account:
            def cancel_all_orders(self):
                assert league.is_halted(halt_db) and league.is_paused(halt_db)
                cancelled.append(agent)
                if agent == "claude" and bad == "exception":
                    raise RuntimeError("unavailable")
                if agent == "claude" and bad == "partial":
                    return [SimpleNamespace(status=500)]
                return [SimpleNamespace(status=200)]
        return Account()
    monkeypatch.setattr(mirror, "keys_for", lambda a: (a, "fake"))
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", broker)
    monkeypatch.setattr(alerts, "send", lambda message, **kw: sent.append((message, kw)))
    out = league.halt(halt_db)
    assert cancelled == ["claude", "gpt"]
    assert out["halted"] and out["paused"]
    assert all(league.is_paused(halt_db, a) for a in cancelled)
    assert set(out["errors"]) == ({"claude"} if bad else set())
    assert out["cancelled"] == (["gpt"] if bad else cancelled)
    assert sent[-1][1]["level"] == "error" and "HALT" in sent[-1][0]
    if bad:
        assert "claude" in sent[-1][0]
    with pytest.raises(league.LeagueError, match="paused"):
        league._require_running(halt_db)
    league.resume(halt_db)
    assert not league.is_halted(halt_db) and not league.is_paused(halt_db)


def test_cancellation_is_allowed_while_halted(halt_db):
    league.set_halted(halt_db, True)
    broker = mirror.AlpacaPaperBroker.__new__(mirror.AlpacaPaperBroker)
    broker.client = fake_client()
    broker.cancel_all_orders()
    broker.client.cancel_orders.assert_called_once_with()


def test_api_admin_halt_resume(halt_db, monkeypatch):
    from arena.api.app import create_app
    monkeypatch.setenv("ADMIN_TOKEN", "fake-admin")
    monkeypatch.setattr(mirror, "keys_for", lambda a: ("fake", "fake"))
    cancel = Mock(return_value=[])
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", lambda *a: SimpleNamespace(cancel_all_orders=cancel))
    client = TestClient(create_app())
    assert client.post("/api/admin/halt").status_code == 401
    assert not league.is_halted(halt_db)
    headers = {"Authorization": "Bearer fake-admin"}
    response = client.post("/api/admin/halt", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"halted": True, "paused": True,
                               "cancelled": ["claude", "gpt"], "errors": {}}
    assert cancel.call_count == 2
    # Individual resume cannot lift the global emergency switch.
    client.post("/api/admin/resume?agent=claude", headers=headers)
    assert league.is_halted(halt_db) and league.is_paused(halt_db, "claude")
    assert client.post("/api/admin/resume", headers=headers).status_code == 200
    assert not league.is_halted(halt_db) and not league.is_paused(halt_db)


def test_cli_halt_and_resume(halt_db, monkeypatch):
    cancel = Mock(return_value=[])
    monkeypatch.setattr(mirror, "keys_for", lambda a: ("fake", "fake"))
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", lambda *a: SimpleNamespace(cancel_all_orders=cancel))
    main.COMMANDS["halt"](halt_db, Namespace(dry_run=False))
    assert league.is_halted(halt_db)
    league.set_paused(halt_db, True, "gpt")
    main.cmd_resume(halt_db, Namespace(agent=None))
    assert not league.is_halted(halt_db) and not league.is_paused(halt_db)
    assert league.is_paused(halt_db, "gpt")


def test_scheduled_mirror_refusal_does_not_crash(halt_db):
    league.set_halted(halt_db, True)
    args = Namespace(dry_run=False, date=DAYS[2], apply=True)
    main._maybe_mirror(halt_db, DAYS[2], args)
    main.cmd_mirror(halt_db, args)
    main.cmd_reconcile(halt_db, args)


def test_stop_loss_still_works_while_halted(conn, agent):
    league.set_halted(conn, True)
    league.set_paused(conn, True)
    stop_loss_case(conn, agent)


def test_cli_resume_halt_option(halt_db):
    league.set_halted(halt_db, True)
    path = halt_db.execute("PRAGMA database_list").fetchone()[2]
    main.main(["resume", "--halt", "--db", path])
    assert not league.is_halted(halt_db) and not league.is_paused(halt_db)


@pytest.mark.parametrize("route", ["test-alpaca", "test-mirror"])
@pytest.mark.parametrize("state", ["halted", "missing", "invalid"])
def test_setup_alpaca_checks_refuse_without_network(halt_db, monkeypatch, tmp_path, route, state):
    from alpaca.trading.client import TradingClient
    from arena.api.app import create_app

    network = Mock(side_effect=AssertionError("Alpaca must not be contacted"))
    monkeypatch.setattr(TradingClient, "get_account", network)
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", network)
    if state == "halted":
        league.set_halted(halt_db, True)
    elif state == "missing":
        monkeypatch.setenv("ARENA_DB", str(tmp_path / "missing.sqlite"))
    else:
        halt_db.execute("INSERT OR REPLACE INTO league_state VALUES ('halted', 'broken')")
        halt_db.commit()
    client = TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(f"/api/control/{route}", headers={"X-Arena-Control": "1"})
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert "emergency halt" in result["message"]
    assert ("run resume" if state == "halted" else "could not be read") in result["message"]
    network.assert_not_called()


@pytest.mark.parametrize("route", ["test-alpaca", "test-mirror"])
def test_setup_alpaca_checks_work_after_resume(halt_db, monkeypatch, route):
    from alpaca.trading import client as alpaca_client
    from arena.api.app import create_app

    account = Mock(return_value=SimpleNamespace(status="ACTIVE"))
    monkeypatch.setattr(alpaca_client, "TradingClient", lambda *a, **kw: SimpleNamespace(get_account=account))
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setattr(mirror, "keys_for", lambda a: ("fake-key", "fake-secret"))
    broker = Mock(account_id=Mock(return_value="fake-account"), equity=Mock(return_value=1000))
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", lambda *a: broker)
    league.set_halted(halt_db, True)
    league.resume(halt_db)
    client = TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 50000))
    response = client.post(f"/api/control/{route}", headers={"X-Arena-Control": "1"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    if route == "test-alpaca":
        account.assert_called_once_with()
    else:
        broker.account_id.assert_called_with()
        broker.equity.assert_called_once_with()
