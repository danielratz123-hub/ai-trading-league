"""The page at /hub and its small JSON API.

Login: Caddy asks the league dashboard (/auth/check) before any /hub request
reaches this app, so only Joe's signed-in browser gets here. Every change
(POST) also needs the X-Hub header, which other websites can't send.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from .agents import agent_command, child_env, clean_tail
from .config import MODEL_RE, Config
from .db import DB
from .worker import Worker, log_text, plan_log

PAGE = Path(__file__).parent / "page.html"


def _model(v: str) -> str:
    v = (v or "").strip()
    if v and not MODEL_RE.match(v):
        raise ValueError("letters, numbers and . _ - : / @ + only")
    return v


class NewTask(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(default="", max_length=20000)
    done_when: str = Field(default="", max_length=4000)
    agent: str
    reviewer: str = "claude"
    model: str = ""
    reviewer_model: str = ""
    auto_merge: bool = False
    _check = field_validator("model", "reviewer_model")(_model)


class NewPlan(BaseModel):
    request: str = Field(min_length=10, max_length=20000)
    manager: str
    reviewer: str = "claude"
    manager_model: str = ""
    reviewer_model: str = ""
    auto_merge: bool = False
    _check = field_validator("manager_model", "reviewer_model")(_model)


class HubSettings(BaseModel):
    max_parallel: int = Field(ge=1, le=3)


class AgentSettings(BaseModel):
    model: str = ""
    effort: str = ""
    enabled: bool = True
    _check = field_validator("model")(_model)


class Choice(BaseModel):
    builder: str | None = None
    reviewer: str | None = None


class Approve(BaseModel):
    tasks: list[Choice] = Field(default_factory=list, max_length=20)


class Notes(BaseModel):
    notes: str = Field(default="", max_length=20000)


class Resume(BaseModel):
    instructions: str = Field(default="", max_length=20000)


def same_site(x_hub: str | None = Header(default=None)) -> None:
    if x_hub != "1":
        raise HTTPException(403, "Missing X-Hub header.")


def public(t: dict, repo: str) -> dict:
    out = {k: t.get(k) for k in ("id", "title", "body", "done_when", "agent", "reviewer", "status",
                                 "issue", "branch", "pr", "round", "note", "plan_id", "plan_order",
                                 "review_verdict", "model", "reviewer_model", "deploy", "merged_at",
                                 "auto_merge", "created_at", "updated_at")}
    out["after"] = DB.after(t)
    base = f"https://github.com/{repo}"
    out["issue_url"] = f"{base}/issues/{t['issue']}" if t["issue"] else None
    out["pr_url"] = f"{base}/pull/{t['pr']}" if t["pr"] else None
    return out


def public_plan(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "request", "manager", "reviewer", "manager_model",
                                  "reviewer_model", "auto_merge", "status", "summary", "tasks", "feedback",
                                  "note", "created_at", "updated_at")}


def today_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def create_app(cfg: Config, db: DB, worker: Worker | None) -> FastAPI:
    app = FastAPI(title="Agent Hub", docs_url=None, redoc_url=None, openapi_url=None)
    agent_tests: dict[str, dict] = {}
    found_models: dict[str, list[str]] = {}
    test_lock = threading.Lock()

    def settings(key: str) -> dict:
        if worker is not None:
            return worker.agent_settings(key)
        s = db.setting(f"agent:{key}", {}) or {}
        return {"model": s.get("model", ""), "effort": s.get("effort", ""), "enabled": s.get("enabled", True)}

    def agent_or_400(key: str, role: str) -> None:
        if key not in cfg.agents:
            raise HTTPException(400, f"Unknown {role}.")
        if not settings(key)["enabled"]:
            raise HTTPException(400, f"{cfg.agents[key]['label']} is switched off (Agents panel).")

    def running() -> list[dict]:
        if worker is None:
            return []
        with worker.jobs_lock:
            jobs = [dict(j, key=k) for k, j in worker.jobs.items()]
        return [{**{k: v for k, v in j.items() if k != "log"},
                 "label": cfg.agents.get(j["agent"], {}).get("label", j["agent"]),
                 "log": clean_tail(Path(j["log"]), 2500) if j.get("log") else ""}
                for j in sorted(jobs, key=lambda j: j.get("started") or 0)]

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
            "agents": [{"key": k, "label": v["label"], "color": v["color"], **settings(k),
                        "efforts": v["efforts"], "models": sorted(set(v["models"]) | set(found_models.get(k, []))),
                        "can_list_models": bool(v["list_models"]), "has_models": bool(v["model_args"]),
                        "today": usage.get(k, {"runs": 0, "minutes": 0}), "test": agent_tests.get(k)}
                       for k, v in cfg.agents.items() for usage in [db.runs_since(today_start())]],
            "running": running(),
            "max_parallel": worker.max_parallel() if worker else cfg.max_parallel,
            "held_back": (worker.room() if worker and worker.jobs and worker.waiting_jobs() else None),
            "tasks": [public(t, cfg.repo) for t in db.list(50)],
            "plans": [public_plan(p) for p in db.plans(20)],
        }

    @app.get("/hub/api/tasks/{task_id}")
    def task(task_id: int):
        t = task_or_404(task_id)
        return {"task": public(t, cfg.repo), "events": db.events(task_id),
                "log": log_text(cfg, t)}

    @app.post("/hub/api/tasks", dependencies=[Depends(same_site)])
    def create(body: NewTask):
        w = need_worker()
        agent_or_400(body.agent, "builder")
        agent_or_400(body.reviewer, "reviewer")
        tid = db.create_task(body.title.strip(), body.body.strip(), body.done_when.strip(), body.agent,
                             body.reviewer, model=body.model, reviewer_model=body.reviewer_model,
                             auto_merge=body.auto_merge)
        db.event(tid, "created", f"{w.model_label(body.agent, body.model)} builds, "
                                 f"{w.model_label(body.reviewer, body.reviewer_model)} reviews")
        w.wake.set()
        return {"id": tid}

    @app.post("/hub/api/plans", dependencies=[Depends(same_site)])
    def create_plan(body: NewPlan):
        w = need_worker()
        agent_or_400(body.manager, "manager")
        agent_or_400(body.reviewer, "reviewer")
        pid = db.create_plan(body.request.strip(), body.manager, body.reviewer, body.manager_model,
                             body.reviewer_model, auto_merge=body.auto_merge)
        w.wake.set()
        return {"id": pid}

    def plan_or_404(plan_id: int) -> dict:
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(404, "No such plan.")
        return p

    @app.get("/hub/api/plans/{plan_id}")
    def plan(plan_id: int):
        p = plan_or_404(plan_id)
        return {"plan": public_plan(p), "log": plan_log(cfg, plan_id),
                "tasks": [public(t, cfg.repo) for t in db.plan_tasks(plan_id)]}

    @app.post("/hub/api/plans/{plan_id}/approve", dependencies=[Depends(same_site)])
    def approve(plan_id: int, body: Approve):
        plan_or_404(plan_id)
        err = need_worker().approve_plan(plan_id, [c.model_dump() for c in body.tasks])
        if err:
            raise HTTPException(409, err)
        return {"ok": True}

    @app.post("/hub/api/plans/{plan_id}/revise", dependencies=[Depends(same_site)])
    def revise(plan_id: int, body: Notes):
        plan_or_404(plan_id)
        err = need_worker().revise_plan(plan_id, body.notes)
        if err:
            raise HTTPException(409, err)
        return {"ok": True}

    @app.post("/hub/api/plans/{plan_id}/reject", dependencies=[Depends(same_site)])
    def reject(plan_id: int):
        plan_or_404(plan_id)
        need_worker().reject_plan(plan_id)
        return {"ok": True}

    @app.post("/hub/api/plans/{plan_id}/cancel", dependencies=[Depends(same_site)])
    def cancel_plan(plan_id: int):
        plan_or_404(plan_id)
        need_worker().cancel_plan(plan_id)
        return {"ok": True}

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

    @app.post("/hub/api/settings", dependencies=[Depends(same_site)])
    def save_settings(body: HubSettings):
        db.set_setting("max_parallel", body.max_parallel)
        if worker is not None:
            worker.wake.set()
        return {"ok": True}

    @app.post("/hub/api/agents/{key}", dependencies=[Depends(same_site)])
    def save_agent(key: str, body: AgentSettings):
        if key not in cfg.agents:
            raise HTTPException(404, "No such agent.")
        a = cfg.agents[key]
        if body.effort and body.effort not in a["efforts"]:
            raise HTTPException(400, f"Effort must be one of: {', '.join(a['efforts']) or 'none'}.")
        if body.model and not a["model_args"]:
            raise HTTPException(400, f"{a['label']} has no model setting in agents.yaml.")
        others_on = [k for k in cfg.agents if k != key and settings(k)["enabled"]]
        if not body.enabled and not others_on:
            raise HTTPException(400, "Keep at least one agent switched on.")
        db.set_setting(f"agent:{key}", body.model_dump())
        return {"ok": True}

    @app.post("/hub/api/agents/{key}/models", dependencies=[Depends(same_site)])
    def list_models(key: str):
        """Ask the CLI which models it can use (only agents with list_models)."""
        a = cfg.agents.get(key)
        if not a or not a["list_models"]:
            raise HTTPException(404, "This agent can't list its models; type a model name instead.")
        try:
            r = subprocess.run(a["list_models"], capture_output=True, text=True, timeout=90,
                               env=child_env(extra=tuple(a["env_keys"])), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HTTPException(502, f"Couldn't list models ({type(e).__name__}).") from e
        names = []
        for line in (r.stdout or "").splitlines():
            word = line.strip().lstrip("*-•> ").split(" ")[0].strip("`',")
            if MODEL_RE.match(word) and any(c.isdigit() or c in "-./" for c in word) and word not in names:
                names.append(word)
        if not names:
            raise HTTPException(502, "The CLI didn't print any model names: "
                                     + ((r.stderr or r.stdout).strip()[-200:] or f"exit {r.returncode}"))
        found_models[key] = names[:40]
        return {"models": found_models[key]}

    @app.post("/hub/api/agents/test", dependencies=[Depends(same_site)])
    def test_agents():
        """Ask each CLI for its version and a one-word answer: shows which
        subscriptions are signed in. Uses a tiny bit of each plan."""
        if not test_lock.acquire(blocking=False):
            raise HTTPException(409, "A test is already running.")

        def probe_all() -> None:
            try:
                for key, a in cfg.agents.items():
                    if not settings(key)["enabled"]:
                        agent_tests.pop(key, None)
                        continue
                    agent_tests[key] = {"ok": None, "detail": "testing…"}
                    agent_tests[key] = _probe({**a, "model": settings(key)["model"],
                                               "effort": settings(key)["effort"]}, cfg.data_dir)
            finally:
                test_lock.release()

        threading.Thread(target=probe_all, daemon=True, name="agent-test").start()
        return {"started": True}

    return app


def _probe(agent_cfg: dict, data_dir: Path) -> dict:
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
