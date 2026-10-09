"""The setup page's backend: .env editing and the local-only control API."""
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from arena import envfile
from arena.api import control
from arena.db import connect

HDR = {"X-Arena-Control": "1"}


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment stays\nALPACA_API_KEY=\n# GPT_MODEL=gpt-old\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "ENV_PATH", p)
    monkeypatch.setattr(envfile, "EXAMPLE_PATH", tmp_path / "missing.example")
    return p


# ---------------------------------------------------------------- .env editing


def test_set_values_replaces_uncomments_and_appends(env_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    envfile.set_values({"ALPACA_API_KEY": "PKTEST1234ABCD", "GPT_MODEL": "gpt-x", "MIRROR_PAPER": "1"})
    text = env_path.read_text(encoding="utf-8")
    assert "# comment stays" in text and "OTHER=keep" in text
    assert "ALPACA_API_KEY=PKTEST1234ABCD" in text
    assert "GPT_MODEL=gpt-x" in text and "gpt-old" not in text          # commented line reused
    assert text.strip().endswith("MIRROR_PAPER=1")                        # new key appended
    assert os.environ["ALPACA_API_KEY"] == "PKTEST1234ABCD"               # visible to new commands


@pytest.mark.parametrize("updates, err", [
    ({"ANTHROPIC_API_KEY": "x"}, "can't be changed"),
    ({"ALPACA_API_KEY": "abc\nMIRROR_PAPER=1"}, "single line"),
    ({"REASONING_EFFORT": "ultra"}, "must be one of"),
])
def test_set_values_rejects_bad_input(env_path, updates, err):
    with pytest.raises(ValueError, match=err):
        envfile.set_values(updates)


def test_describe_never_reveals_secrets(env_path):
    envfile.set_values({"ALPACA_SECRET_KEY": "supersecretvalue9876"})
    d = envfile.describe()
    assert d["ALPACA_SECRET_KEY"] == {"set": True, "hint": "…9876"}
    assert "supersecretvalue" not in str(d)


def test_first_use_creates_env_with_an_admin_token(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", p)
    monkeypatch.setattr(envfile, "EXAMPLE_PATH", tmp_path / "none")
    envfile.ensure_env_file()
    assert len(envfile.read_values()["ADMIN_TOKEN"]) >= 24


# ---------------------------------------------------------------- control API


@pytest.fixture
def app(env_path, tmp_path, monkeypatch):
    db = tmp_path / "arena.db"
    connect(db).close()
    monkeypatch.setenv("ARENA_DB", str(db))
    monkeypatch.delenv("ARENA_DISABLE_CONTROL", raising=False)
    import arena.api.app as app_module
    return app_module.create_app()


def local(app):
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_local_page_works(app):
    r = local(app).get("/api/control/state")
    assert r.status_code == 200
    s = r.json()
    assert s["env"]["ALPACA_API_KEY"]["set"] is False and s["league"]["started"] is False


def test_requests_from_the_network_are_refused(app):
    remote = TestClient(app, base_url="http://127.0.0.1:8000", client=("203.0.113.9", 50000))
    assert remote.get("/api/control/state").status_code == 403


def test_dns_rebinding_host_is_refused(app):
    c = TestClient(app, base_url="http://evil.example:8000", client=("127.0.0.1", 50000))
    assert c.get("/api/control/state").status_code == 403


def test_other_websites_cannot_press_buttons(app):
    c = local(app)
    assert c.post("/api/control/env", json={"MIRROR_PAPER": "1"}).status_code == 403          # no header
    assert c.post("/api/control/env", json={"MIRROR_PAPER": "1"},
                  headers={**HDR, "Origin": "https://evil.example"}).status_code == 403
    # Cross-site pages can't add the header: CORS preflight refuses it.
    pre = c.options("/api/control/env", headers={"Origin": "https://evil.example",
                                                 "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "x-arena-control"})
    assert "x-arena-control" not in pre.headers.get("access-control-allow-headers", "").lower()


def test_control_can_be_disabled(app, monkeypatch):
    monkeypatch.setenv("ARENA_DISABLE_CONTROL", "1")
    assert local(app).get("/api/control/state").status_code == 404


def test_saving_keys_returns_only_hints(app, env_path):
    r = local(app).post("/api/control/env", json={"ALPACA_API_KEY": "PKLIVEKEY0000WXYZ"}, headers=HDR)
    assert r.status_code == 200 and "PKLIVEKEY" not in r.text and r.json()["env"]["ALPACA_API_KEY"]["hint"] == "…WXYZ"
    assert "ALPACA_API_KEY=PKLIVEKEY0000WXYZ" in env_path.read_text(encoding="utf-8")
    bad = local(app).post("/api/control/env", json={"OPENAI_API_KEY": "x"}, headers=HDR)
    assert bad.status_code == 400


def test_only_known_actions_run(app):
    c = local(app)
    assert c.post("/api/control/run/rm-rf", json={}, headers=HDR).status_code == 404
    assert c.post("/api/control/run/practice-week", json={"start": "2025-06-02", "weeks": 9}, headers=HDR).status_code == 400
    assert c.post("/api/control/run/practice-week", json={"start": "not a date"}, headers=HDR).status_code == 400


def test_task_runner_streams_output_and_runs_one_task_at_a_time():
    runner = control.TaskRunner()
    runner.start("demo", [[sys.executable, "-c", "import time; print('hello'); time.sleep(1)"]])
    with pytest.raises(Exception, match="still running"):
        runner.start("other", [[sys.executable, "-c", "pass"]])
    for _ in range(100):
        if runner.status != "running":
            break
        time.sleep(0.1)
    s = runner.state()
    assert s["status"] == "done" and "hello" in s["log"]


def test_failing_step_stops_the_task():
    runner = control.TaskRunner()
    runner.start("demo", [[sys.executable, "-c", "raise SystemExit(3)"], [sys.executable, "-c", "print('never')"]])
    for _ in range(100):
        if runner.status != "running":
            break
        time.sleep(0.1)
    assert runner.status == "failed" and "never" not in runner.state()["log"]
