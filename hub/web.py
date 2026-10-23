"""The page at /hub and its small JSON API.

Login: Caddy asks the league dashboard (/auth/check) before any /hub request
reaches this app, so only Joe's signed-in browser gets here. Every change
(POST) also needs the X-Hub header, which other websites can't send.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Config
from .db import DB
from .worker import Worker, log_text

PAGE = Path(__file__).parent / "page.html"


class NewTask(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(default="", max_length=20000)
    done_when: str = Field(default="", max_length=4000)
    agent: str


class Resume(BaseModel):
    instructions: str = Field(default="", max_length=20000)


def same_site(x_hub: str | None = Header(default=None)) -> None:
    if x_hub != "1":
        raise HTTPException(403, "Missing X-Hub header.")


def public(t: dict, repo: str) -> dict:
    out = {k: t[k] for k in ("id", "title", "body", "done_when", "agent", "status", "issue", "branch",
                             "pr", "round", "note", "created_at", "updated_at")}
    base = f"https://github.com/{repo}"
    out["issue_url"] = f"{base}/issues/{t['issue']}" if t["issue"] else None
    out["pr_url"] = f"{base}/pull/{t['pr']}" if t["pr"] else None
    return out


def create_app(cfg: Config, db: DB, worker: Worker | None) -> FastAPI:
    app = FastAPI(title="Agent Hub", docs_url=None, redoc_url=None, openapi_url=None)
    agent_tests: dict[str, dict] = {}
    test_lock = threading.Lock()

    def need_worker() -> Worker:
        if worker is None or not cfg.configured:
            raise HTTPException(409, "The hub isn't set up yet: HUB_REPO and HUB_GITHUB_TOKEN "
                                     "are missing (see HUB.md).")
        return worker

    def task_or_404(task_id: int) -> dict:
        t = db.get(task_id)
        if not t:
            raise HTTPException(404, "No such task.")
        return t

    @app.get("/hub/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/hub")
    def hub_root():
        return RedirectResponse("/hub/", status_code=307)

    @app.get("/hub/")
    def page():
        return FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    @app.get("/hub/api/state")
    def state():
        return {
            "configured": cfg.configured,
            "repo": cfg.repo,
            "max_rounds": cfg.max_rounds,
            "testing": test_lock.locked(),
            "agents": [{"key": k, "label": v["label"], "test": agent_tests.get(k)}
                       for k, v in cfg.agents.items()],
            "tasks": [public(t, cfg.repo) for t in db.list(50)],
        }

    @app.get("/hub/api/tasks/{task_id}")
    def task(task_id: int):
        t = task_or_404(task_id)
        return {"task": public(t, cfg.repo), "events": db.events(task_id),
                "log": log_text(cfg, t)}

    @app.post("/hub/api/tasks", dependencies=[Depends(same_site)])
    def create(body: NewTask):
        w = need_worker()
        if body.agent not in cfg.agents:
            raise HTTPException(400, "Unknown agent.")
        tid = db.create_task(body.title.strip(), body.body.strip(), body.done_when.strip(), body.agent)
        db.event(tid, "created", f"Task for {cfg.agents[body.agent]['label']}")
        w.wake.set()
        return {"id": tid}

    @app.post("/hub/api/tasks/{task_id}/cancel", dependencies=[Depends(same_site)])
    def cancel(task_id: int):
        task_or_404(task_id)
        need_worker().cancel(task_id)
        return {"ok": True}

    @app.post("/hub/api/tasks/{task_id}/continue", dependencies=[Depends(same_site)])
    def resume(task_id: int, body: Resume):
        task_or_404(task_id)
        need_worker().resume(task_id, body.instructions)
        return {"ok": True}

    @app.post("/hub/api/tasks/{task_id}/merge", dependencies=[Depends(same_site)])
    def merge(task_id: int):
        task_or_404(task_id)
        try:
            err = need_worker().merge(task_id)
        except Exception as e:  # noqa: BLE001 - GitHub refused: show why
            return JSONResponse({"detail": f"GitHub refused the merge: {e}"}, status_code=409)
        if err:
            raise HTTPException(409, err)
        return {"ok": True}

    @app.post("/hub/api/agents/test", dependencies=[Depends(same_site)])
    def test_agents():
        """Ask each CLI for its version and a one-word answer: shows which
        subscriptions are signed in. Uses a tiny bit of each plan."""
        if not test_lock.acquire(blocking=False):
            raise HTTPException(409, "A test is already running.")

        def probe_all() -> None:
            try:
                for key, a in cfg.agents.items():
                    agent_tests[key] = {"ok": None, "detail": "testing…"}
                    agent_tests[key] = _probe(a, cfg.data_dir)
            finally:
                test_lock.release()

        threading.Thread(target=probe_all, daemon=True, name="agent-test").start()
        return {"started": True}

    return app


def _probe(agent_cfg: dict, data_dir: Path) -> dict:
    from .agents import agent_command, child_env
    env = child_env(extra=tuple(agent_cfg.get("env_keys", ())))
    scratch = data_dir / "probe"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        v = subprocess.run(agent_cfg["version"], capture_output=True, text=True, timeout=60,
                           env=env, stdin=subprocess.DEVNULL)
        version = (v.stdout or v.stderr).strip().splitlines()[0][:80] if (v.stdout or v.stderr) else "?"
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "detail": f"not installed ({type(e).__name__})"}
    try:
        r = subprocess.run(agent_command(agent_cfg, "Reply with exactly the word OK and nothing else."),
                           cwd=scratch, capture_output=True, text=True, timeout=180,
                           env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "version": version, "detail": "no answer in 3 min (signed in?)"}
    out = (r.stdout + r.stderr).strip()
    ok = r.returncode == 0 and "OK" in r.stdout
    return {"ok": ok, "version": version,
            "detail": "signed in and answering" if ok else (out[-300:] or f"exit {r.returncode}")}
