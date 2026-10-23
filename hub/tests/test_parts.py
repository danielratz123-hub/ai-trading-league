import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from hub import agents, review
from hub.config import load
from hub.db import DB
from hub.gitops import Repo, branch_name, slug
from hub.web import create_app
from hub.worker import Worker

from .fakes import FakeGitHub, iso, make_cfg


# ---------------------------------------------------------------- review

@pytest.mark.parametrize("text,want", [
    ("**Verdict:** ✅ Ready to merge", review.OK),
    ("**Verdict:** 🔧 Needs fixes", review.FIX),
    ("**Verdict:** ⛔ Stop, Joe must look", review.STOP),
    ("Verdict: needs fixes", review.FIX),
    ("no verdict here", review.UNKNOWN),
])
def test_verdict(text, want):
    assert review.verdict(text) == want


def test_latest_summary_only_claude_after_push():
    t0 = 1_800_000_000.0
    comments = [
        {"user": {"login": "claude[bot]"}, "created_at": iso(t0 - 600), "body": "Verdict: 🔧 old"},
        {"user": {"login": "someone"}, "created_at": iso(t0 + 60), "body": "Verdict: ✅ fake"},
        {"user": {"login": "claude[bot]"}, "created_at": iso(t0 + 90), "body": "Verdict: ✅ new"},
    ]
    assert review.latest_summary(comments, t0)["body"] == "Verdict: ✅ new"
    assert review.latest_summary(comments[:2], t0) is None


def test_checks_state_prefers_pull_request_runs():
    runs = [{"name": "CI", "status": "completed", "conclusion": "failure", "event": "push"},
            {"name": "CI", "status": "completed", "conclusion": "success", "event": "pull_request"},
            {"name": "Claude review", "status": "in_progress", "conclusion": None, "event": "pull_request"}]
    s = review.checks_state(runs)
    assert s.ci == "success" and s.review is None and not s.done


# ---------------------------------------------------------------- agents

def test_child_env_hides_secrets(monkeypatch):
    monkeypatch.setenv("HUB_GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg")
    monkeypatch.setenv("CODEX_HOME", "/x")
    env = agents.child_env()
    assert "HUB_GITHUB_TOKEN" not in env and "TELEGRAM_BOT_TOKEN" not in env
    assert env["CODEX_HOME"] == "/x"
    assert "CODEX_HOME" not in agents.child_env(drop=("CODEX_HOME",))


def test_agent_command_fills_prompt():
    cfg = {"cmd": ["gemini", "--yolo", "-p", "{prompt}"]}
    assert agents.agent_command(cfg, "do it") == ["gemini", "--yolo", "-p", "do it"]


def test_run_logs_output(tmp_path):
    r = agents.run([sys.executable, "-c", "print('hello')"], tmp_path, tmp_path / "a.log", 30, os.environ.copy())
    assert r.ok and "hello" in r.tail


def test_run_time_limit_and_stop(tmp_path):
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    t = time.monotonic()
    r = agents.run(slow, tmp_path, tmp_path / "b.log", 1, os.environ.copy())
    assert r.timed_out and not r.ok and time.monotonic() - t < 15
    r = agents.run(slow, tmp_path, tmp_path / "c.log", 60, os.environ.copy(), should_stop=lambda: True)
    assert r.stopped and not r.ok


def test_config_command_override(monkeypatch):
    monkeypatch.setenv("HUB_CMD_GEMINI", '["gemini", "-m", "pro", "-p", "{prompt}"]')
    monkeypatch.setenv("HUB_CMD_CODEX", "not json")
    cfg = load()
    assert cfg.agents["gemini"]["cmd"][:3] == ["gemini", "-m", "pro"]
    assert cfg.agents["codex"]["cmd"][0] == "codex"
    assert not cfg.configured


# ---------------------------------------------------------------- git

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repo_round_trip(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a\n")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(seed, "add", "a.txt")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
    _git(seed, "push", "-q", str(origin), "main")

    r = Repo(tmp_path / "work", "joe/league", "", "main", url=str(origin))
    r.fresh("hub/codex-1-x")
    assert not r.commit_all("nothing")
    (r.path / "b.txt").write_text("b\n")
    (r.path / ".env").write_text("SECRET=1\n")
    assert r.commit_all("add b")
    assert sorted(r.changed_files()) == [".env", "b.txt"]
    assert r.forbidden_files() == [".env"]
    r.push("hub/codex-1-x")

    r2 = Repo(tmp_path / "work2", "joe/league", "", "main", url=str(origin))
    r2.resume("hub/codex-1-x")
    assert (r2.path / "b.txt").exists() and r2.head() == r.head()
    r2.remove()
    assert not r2.path.exists()


def test_branch_names():
    assert slug("Add SPY buy-and-hold player!!") == "add-spy-buy-and-hold-player"
    assert branch_name("gemini", 12, "Fix  the thing") == "hub/gemini-12-fix-the-thing"


# ---------------------------------------------------------------- web

def client(tmp_path, configured=True):
    cfg = make_cfg(tmp_path) if configured else make_cfg(tmp_path, token="")
    db = DB(":memory:")
    w = Worker(cfg, db, FakeGitHub()) if configured else None
    return TestClient(create_app(cfg, db, w)), db


def test_page_and_state(tmp_path):
    c, db = client(tmp_path)
    assert c.get("/hub/").status_code == 200
    s = c.get("/hub/api/state").json()
    assert s["configured"] and [a["key"] for a in s["agents"]] == ["claude", "codex", "gemini"]


def test_create_needs_header_and_valid_agent(tmp_path):
    c, db = client(tmp_path)
    task = {"title": "Do a thing", "agent": "codex"}
    assert c.post("/hub/api/tasks", json=task).status_code == 403
    assert c.post("/hub/api/tasks", json={**task, "agent": "x"}, headers={"X-Hub": "1"}).status_code == 400
    r = c.post("/hub/api/tasks", json=task, headers={"X-Hub": "1"})
    assert r.status_code == 200 and db.get(r.json()["id"])["status"] == "queued"
    d = c.get(f"/hub/api/tasks/{r.json()['id']}").json()
    assert d["task"]["title"] == "Do a thing" and d["events"][0]["kind"] == "created"


def test_not_configured_refuses_tasks(tmp_path):
    c, db = client(tmp_path, configured=False)
    assert c.get("/hub/api/state").json()["configured"] is False
    r = c.post("/hub/api/tasks", json={"title": "Do a thing", "agent": "codex"}, headers={"X-Hub": "1"})
    assert r.status_code == 409


def test_merge_refused_unless_ready(tmp_path):
    c, db = client(tmp_path)
    tid = db.create_task("Do a thing", "", "", "codex")
    assert c.post(f"/hub/api/tasks/{tid}/merge", headers={"X-Hub": "1"}).status_code == 409


def test_pip_only_when_requirements_change(tmp_path, monkeypatch):
    base = tmp_path / "base.txt"
    base.write_text("pandas==3\n")
    monkeypatch.setenv("HUB_BASE_REQUIREMENTS", str(base))
    work = tmp_path / "w"
    work.mkdir()
    (work / "requirements.txt").write_text("pandas==3\n")
    assert not agents.requirements_changed(work)
    (work / "requirements.txt").write_text("pandas==3\nrich==1\n")
    assert agents.requirements_changed(work)


def test_claude_login_goes_only_to_claude(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    cfg = load()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in agents.child_env()
    claude_env = agents.child_env(extra=tuple(cfg.agents["claude"]["env_keys"]))
    assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert "env_keys" not in cfg.agents["codex"]
