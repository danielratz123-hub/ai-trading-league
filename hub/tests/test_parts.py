import json
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

from .fakes import FakeGitHub, make_cfg


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


def test_review_section_takes_the_last_verdict():
    out = ("prompt echo: **Verdict:** ✅ Ready to merge  /  🔧 Needs fixes  /  ⛔ Stop\n"
           "...thinking...\n**Verdict:** 🔧 Needs fixes\n**Must fix:** 1. x")
    sec = review.review_section(out)
    assert sec.startswith("**Verdict:** 🔧") and review.verdict(sec) == review.FIX
    assert review.review_section("nothing here") == ""


def test_ci_state_prefers_pull_request_runs():
    runs = [{"name": "CI", "status": "completed", "conclusion": "failure", "event": "push"},
            {"name": "CI", "status": "completed", "conclusion": "success", "event": "pull_request"},
            {"name": "Claude", "status": "completed", "conclusion": "skipped", "event": "pull_request"}]
    assert review.ci_state(runs) == "success"
    assert review.ci_state([{"name": "CI", "status": "in_progress", "conclusion": None,
                             "event": "pull_request"}]) is None
    assert review.ci_state([]) is None


def test_parse_plan():
    agents_cfg = {"claude": {}, "codex": {}, "gemini": {}}
    out = 'echo BEGIN_PLAN {"x": 1} END_PLAN\nBEGIN_PLAN\n```json\n{"summary": "S", "tasks": [' \
          '{"title": "A", "builder": "Codex"}, {"title": "", "builder": "codex"}, {"title": "B", "builder": "x"}]}' \
          '\n```\nEND_PLAN'
    summary, tasks = review.parse_plan(out, agents_cfg, "gemini")
    assert summary == "S" and [(t["title"], t["builder"]) for t in tasks] == [("A", "codex"), ("B", "gemini")]
    with pytest.raises(review.PlanError):
        review.parse_plan("no plan", agents_cfg, "claude")
    with pytest.raises(review.PlanError):
        review.parse_plan("BEGIN_PLAN {bad json END_PLAN", agents_cfg, "claude")
    many = "BEGIN_PLAN " + json.dumps({"tasks": [{"title": f"t{i}"} for i in range(9)]}) + " END_PLAN"
    assert len(review.parse_plan(many, agents_cfg, "claude")[1]) == review.MAX_PLAN_TASKS


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


def test_task_with_reviewer_and_plan_flow(tmp_path):
    c, db = client(tmp_path)
    h = {"X-Hub": "1"}
    r = c.post("/hub/api/tasks", json={"title": "Do a thing", "agent": "codex", "reviewer": "gemini"}, headers=h)
    assert db.get(r.json()["id"])["reviewer"] == "gemini"
    assert c.post("/hub/api/tasks", json={"title": "Do a thing", "agent": "codex", "reviewer": "x"},
                  headers=h).status_code == 400
    assert c.post("/hub/api/plans", json={"request": "short", "manager": "claude"}, headers=h).status_code == 422
    r = c.post("/hub/api/plans", json={"request": "Add an SPY benchmark player", "manager": "gemini",
                                        "reviewer": "claude"}, headers=h)
    pid = r.json()["id"]
    assert c.get("/hub/api/state").json()["plans"][0]["status"] == "planning"
    assert c.post(f"/hub/api/plans/{pid}/approve", json={}, headers=h).status_code == 409
    db.update_plan(pid, status="proposed", tasks_json=json.dumps(
        [{"title": "A", "details": "", "done_when": "", "builder": "codex", "reviewer": "claude"}]))
    assert c.post(f"/hub/api/plans/{pid}/approve", json={"tasks": [{"reviewer": "gemini"}]},
                  headers=h).status_code == 200
    d = c.get(f"/hub/api/plans/{pid}").json()
    assert d["plan"]["status"] == "approved" and d["tasks"][0]["reviewer"] == "gemini"
    assert c.post(f"/hub/api/plans/{pid}/cancel", headers=h).status_code == 200


def test_gemini_runs_through_antigravity(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-123")
    cfg = load()
    assert cfg.agents["gemini"]["cmd"][0] == "agy"
    assert agents.agent_command(cfg.agents["gemini"], "hi") == ["agy", "-p", "hi", "--dangerously-skip-permissions"]
    assert "GEMINI_API_KEY" not in agents.child_env(extra=tuple(cfg.agents["claude"]["env_keys"]))
    assert "AIzaSy-test-key-123" in cfg.secrets
