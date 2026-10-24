"""The hub's brain: plans and tasks, from Joe's request to "ready to merge".

A plan (optional):
    planning ──► managing ──► proposed ──(Joe approves)──► approved ──► done
    The manager agent splits Joe's request into tasks; they run one after
    another, each one starting when the one before it is merged.

A task:
    queued ──► building ──► reviewing ──► ready ──(Joe merges)──► merged
                  ▲             │  the reviewer agent reads the PR and
                  └── queued ◄──┘  says ✅ / 🔧 / ⛔; 🔧 = a fix round
                     (fix round)    (up to max_rounds, then needs_joe)

One agent runs at a time: they share Joe's subscriptions and a small VPS.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from . import agents, prompts, review
from .config import Config
from .db import DB, PLAN_FINAL, RUNNING
from .gitops import Repo, branch_name

log = logging.getLogger("hub")

# Never merged automatically, whatever the review says: Joe merges these.
PROTECTED = re.compile(r"^(\.github/|scripts/(deploy|restore)\.sh$|docker-compose\.yml$|Dockerfile|Caddyfile$|"
                       r"requirements\.txt$|arena/api/auth\.py$|arena/config\.py$|arena/risk\.py$|"
                       r"arena/backup\.py$|arena/learning/hardlimits\.py$|config/learning\.yaml$|"
                       r"arena/agents/|hub/)")


def free_mb() -> int | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


class Worker:
    def __init__(self, cfg: Config, db: DB, gh, *,
                 repo_factory: Callable[[dict], Repo] | None = None,
                 run_agent=agents.run_agent, run_checks=agents.run_checks,
                 notify: Callable[[str], None] = lambda text: None,
                 clock: Callable[[], float] = time.time, threaded: bool = False):
        self.cfg, self.db, self.gh = cfg, db, gh
        self.repo_factory = repo_factory or (lambda t: Repo(
            cfg.work_dir / (f"plan-{t['plan']}" if "plan" in t else f"task-{t['id']}"),
            cfg.repo, cfg.token, cfg.base_branch))
        self.run_agent, self.run_checks = run_agent, run_checks
        self.notify, self.clock = notify, clock
        self.wake = threading.Event()
        self.lock = threading.Lock()     # one state change at a time (worker vs. web)
        # Jobs running now: {"t12" / "r12" / "p3": {agent, role, title, log, ...}}.
        # threaded=False (tests) runs one job per step, in this thread.
        self.threaded = threaded
        self.jobs: dict[str, dict] = {}
        self.jobs_lock = threading.Lock()
        self._here = threading.local()   # which job this thread is doing

    # ------------------------------------------------------------------ loop

    def recover(self) -> None:
        """After a restart: a task that was mid-run can't be resumed safely; a
        manager that was mid-run simply plans again."""
        for t in self.db.with_status(*RUNNING):
            self._stop(t, "needs_joe", "The hub restarted while the agent was working. "
                                       "Press Continue to run it again.")
        for p in self.db.plans_with_status("managing"):
            self.db.update_plan(p["id"], status="planning")

    def forever(self) -> None:
        self.recover()
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.error("hub step failed:\n%s", traceback.format_exc())
            self.wake.wait(self.cfg.poll_seconds)
            self.wake.clear()

    def step(self) -> None:
        """Check GitHub, then start agent jobs while there's room."""
        for t in self.db.with_status("reviewing", "ready"):
            try:
                self.poll(t)
            except Exception as e:  # noqa: BLE001 - GitHub hiccups: try again next pass
                log.warning("poll of task %s failed: %s", t["id"], e)
        for t in self.db.with_status("merged"):
            if t.get("deploy") == "pending":
                try:
                    self.poll_deploy(t)
                except Exception as e:  # noqa: BLE001
                    log.warning("deploy check of task %s failed: %s", t["id"], e)
        self.dispatch()

    # ------------------------------------------------------------- parallel runs

    def max_parallel(self) -> int:
        n = self.db.setting("max_parallel", None)
        return max(1, min(int(n if n else self.cfg.max_parallel), 3))

    def waiting_jobs(self) -> list[tuple[str, str, Callable[[], None], str]]:
        """(job key, agent, what to call, title) in priority order: reviews
        first (they unblock merges), then managers, then builds."""
        out = []
        for t in self.db.with_status("reviewing"):
            if t["head_sha"] and t["review_sha"] != t["head_sha"]:
                out.append((f"r{t['id']}", self._reviewer(t), lambda t=t: self.run_review(t), t["title"]))
        for p in self.db.plans_with_status("planning"):
            out.append((f"p{p['id']}", self._first(p["manager"], "claude"),
                        lambda p=p: self.run_manager(p), p["request"][:120]))
        for t in self.db.with_status("queued"):
            out.append((f"t{t['id']}", t["agent"], lambda t=t: self.run(t), t["title"]))
        return out

    def room(self) -> str | None:
        """None if one more run fits next to the league, else why not."""
        if len(self.jobs) >= self.max_parallel():
            return "limit"
        if not self.jobs:
            return None                  # the first run always goes
        try:
            load = os.getloadavg()[0] / (os.cpu_count() or 1)
        except OSError:
            load = 0.0
        if load > self.cfg.max_load:
            return f"CPU busy ({load:.0%} per core)"
        mb = free_mb()
        if mb is not None and mb < self.cfg.min_free_mb:
            return f"low memory ({mb} MB free)"
        return None

    def dispatch(self) -> None:
        busy_agents = {j["agent"] for j in self.jobs.values()}
        for key, agent, call, title in self.waiting_jobs():
            if key in self.jobs or agent in busy_agents:
                continue                 # already running / that subscription is busy
            if self.room():
                return
            with self.jobs_lock:
                self.jobs[key] = {"agent": agent, "title": title, "role": "", "model": "",
                                  "log": "", "started": self.clock()}
            busy_agents.add(agent)
            if not self.threaded:
                self._job(key, call)
                return                   # tests: one job per step
            threading.Thread(target=self._job, args=(key, call), daemon=True, name=f"job-{key}").start()

    def _job(self, key: str, call: Callable[[], None]) -> None:
        self._here.job = key
        try:
            call()
        except Exception:  # noqa: BLE001 - a job must never kill the hub
            log.error("job %s failed:\n%s", key, traceback.format_exc())
        finally:
            with self.jobs_lock:
                self.jobs.pop(key, None)
            self._here.job = None
            self.wake.set()              # a slot is free: look for the next job

    # ------------------------------------------------------------- agents

    def label(self, agent: str) -> str:
        return self.cfg.agents.get(agent, {}).get("label", agent)

    def agent_settings(self, key: str) -> dict:
        """Joe's choices for an agent on the page: default model, effort, on/off."""
        s = self.db.setting(f"agent:{key}", {}) or {}
        return {"model": s.get("model", ""), "effort": s.get("effort", ""),
                "enabled": s.get("enabled", True)}

    def enabled(self) -> dict[str, dict]:
        return {k: a for k, a in self.cfg.agents.items() if self.agent_settings(k)["enabled"]}

    def model_for(self, key: str, override: str = "") -> str:
        """The model a run uses: the task's choice, else the agent's default,
        else "" (the CLI's own default)."""
        return override or self.agent_settings(key)["model"]

    def model_label(self, key: str, override: str = "") -> str:
        m = self.model_for(key, override)
        return f"{self.label(key)} ({m})" if m else self.label(key)

    def _first(self, *keys: str) -> str:
        for k in keys:
            if k in self.cfg.agents:
                return k
        return next(iter(self.cfg.agents))

    def _reviewer(self, t: dict) -> str:
        return self._first(t.get("reviewer") or "", "claude")

    def _run(self, role: str, key: str, model: str, prompt: str, cwd: Path, log_path: Path,
             should_stop: Callable[[], bool], title: str, task_id: int | None = None,
             plan_id: int | None = None):
        """Run one agent: with its model and effort, shown on the page while
        it runs, and counted in the run log."""
        a = dict(self.cfg.agents[key])
        a["model"] = model
        a["effort"] = self.agent_settings(key)["effort"]
        self._show(role=role, agent=key, model=model, title=title, task_id=task_id, plan_id=plan_id,
                   log=str(log_path))
        started = time.monotonic()
        result = self.run_agent(a, prompt, cwd, log_path, self.cfg.agent_timeout, should_stop)
        self.db.add_run(key, model, role, task_id, plan_id, time.monotonic() - started, result.ok)
        return result

    def _show(self, **info) -> None:
        """Update what the page shows for this thread's job."""
        job = getattr(self._here, "job", None)
        with self.jobs_lock:
            if job in self.jobs:
                self.jobs[job].update(info, started=self.jobs[job].get("started") or self.clock())

    def _stopping(self, task_id: int) -> Callable[[], bool]:
        return lambda: bool((self.db.get(task_id) or {}).get("cancel"))

    def _stop(self, t: dict, status: str, note: str, tell: bool = True) -> None:
        self.db.update(t["id"], status=status, note=note)
        self.db.event(t["id"], status, note)
        if tell and status in ("needs_joe", "failed"):
            self.notify(f"⚠️ Task #{t['id']} ({t['title']}) needs you: {note}")

    def _full_log(self, path: Path) -> str:
        return agents.tail(path, 200_000)

    # ------------------------------------------------------------- building

    def run(self, t: dict) -> None:
        tid, fixing = t["id"], bool(t["pr"])
        builder = self.label(t["agent"])
        self.db.update(tid, status="fixing" if fixing else "building", note=None)
        self.db.event(tid, "start", f"{self.model_label(t['agent'], t.get('model') or '')} "
                                    f"{'fix round ' + str(t['round']) if fixing else 'starts building'}")
        repo = self.repo_factory(t)
        try:
            if not fixing:
                if not t["issue"]:
                    issue = self.gh.create_issue(f"[task] {t['title']}", self._issue_body(t), [t["agent"]])
                    self.db.update(tid, issue=issue)
                    self.db.event(tid, "issue", f"Issue #{issue} created")
                t = self.db.get(tid)
                branch = t["branch"] or branch_name(t["agent"], t["issue"], t["title"])
                self.db.update(tid, branch=branch)
                t = self.db.get(tid)
                repo.fresh(branch)
                prompt = prompts.build(builder, t)
            else:
                repo.resume(t["branch"])
                prompt = prompts.fix(builder, t, t["feedback"] or "", self.cfg.max_rounds)

            result = self._run("build" if not fixing else "fix", t["agent"], self.model_for(t["agent"], t.get("model") or ""),
                               prompt, repo.path, self.cfg.log_dir / f"task-{tid}-r{t['round']}.log",
                               self._stopping(tid), t["title"], task_id=tid)
            if result.stopped:
                return self._cancelled(t)
            self.db.event(tid, "agent", result.tail[-4000:])
            if not result.ok:
                why = "ran out of time" if result.timed_out else f"stopped with an error (exit {result.exit_code})"
                return self._stop(t, "needs_joe", f"{builder} {why}. See the log below.")
            summary = result.tail
            repo.commit_all(f"{builder}: {t['title']}" if not fixing
                            else f"{builder}: review fixes (round {t['round']})")

            checks = self._checks(t, repo)
            if checks.stopped:
                return self._cancelled(t)

            bad = repo.forbidden_files()
            if bad:
                return self._stop(t, "needs_joe", "The change includes files that must never be "
                                  f"committed ({', '.join(bad[:5])}). Nothing was pushed.")
            diff = repo.diff_text()
            if any(secret in diff for secret in self.cfg.secrets):
                return self._stop(t, "needs_joe", "The change contains one of the hub's secrets "
                                  "(a token). Nothing was pushed. Check the work copy on the server.")
            if not repo.changed_files():
                return self._stop(t, "needs_joe", f"{builder} finished without changing anything.")

            repo.push(t["branch"])
            head = repo.head()
            if not fixing:
                pr = self.gh.create_pr(f"[{t['agent']}] {t['title']}", t["branch"], self.cfg.base_branch,
                                       prompts.pr_body(t, self.model_label(t["agent"], t.get("model") or ""),
                                                       self.model_label(self._reviewer(t), t.get("reviewer_model") or ""),
                                                       summary, checks.ok))
                self.db.update(tid, pr=pr)
                self.db.event(tid, "pr", f"PR #{pr} opened")
            else:
                self.gh.comment(t["pr"], f"🧰 Agent Hub: {builder} pushed fixes "
                                         f"(round {t['round']}).\n\n{summary.strip()[-3000:]}")
                self.db.event(tid, "pushed", f"Fixes pushed (round {t['round']})")
            note = None if checks.ok else "The hub's own test run failed; the reviewer and CI will see it."
            self.db.update(tid, status="reviewing", head_sha=head, pushed_at=self.clock(),
                           feedback=None, note=note, review_sha=None, review_verdict=None)
            self.db.event(tid, "reviewing", f"Waiting for {self.label(self._reviewer(t))}'s review and CI")
            self.wake.set()
        except Exception as e:  # noqa: BLE001 - surface it on the page, keep the hub alive
            log.error("task %s failed:\n%s", tid, traceback.format_exc())
            self._stop(self.db.get(tid), "failed", f"{type(e).__name__}: {e}"[:1500])

    def _checks(self, t: dict, repo: Repo):
        tid = t["id"]
        log_path = self.cfg.log_dir / f"task-{tid}-r{t['round']}-checks.log"

        def run_checks():
            self._show(role="checks", agent=t["agent"], model="", title=t["title"], task_id=tid,
                       log=str(log_path))
            return self.run_checks(repo.path, log_path, self.cfg.check_timeout, self._stopping(tid))

        checks = run_checks()
        tries = 0
        while not checks.ok and not checks.stopped and tries < 2:
            tries += 1
            self.db.event(tid, "checks", f"Checks failed, asking {self.label(t['agent'])} to fix them "
                                         f"(try {tries} of 2)\n{checks.tail[-2000:]}")
            fix = self._run("fix", t["agent"], self.model_for(t["agent"], t.get("model") or ""),
                            prompts.checks_failed(checks.tail), repo.path,
                            self.cfg.log_dir / f"task-{tid}-r{t['round']}-fix{tries}.log",
                            self._stopping(tid), t["title"], task_id=tid)
            if fix.stopped:
                checks.stopped = True
                return checks
            repo.commit_all(f"{self.label(t['agent'])}: fix failing checks")
            checks = run_checks()
        self.db.event(tid, "checks", "Checks pass ✅" if checks.ok else "Checks still failing")
        return checks

    def _issue_body(self, t: dict) -> str:
        plan = f" (plan #{t['plan_id']}, step {t['plan_order'] + 1})" if t.get("plan_id") else ""
        return (f"### Who builds it\n{t['agent']}\n\n### Who reviews it\n{self._reviewer(t)}\n\n"
                f"### What to build\n{t['body'] or t['title']}\n\n### Done when\n{t['done_when'] or '-'}\n\n"
                f"_Started from the Agent Hub (task #{t['id']}{plan})._")

    def _cancelled(self, t: dict) -> None:
        t = self.db.get(t["id"])
        if t["pr"]:
            try:
                self.gh.comment(t["pr"], "🧰 Agent Hub: task cancelled by Joe.")
                self.gh.close_pr(t["pr"])
            except Exception as e:  # noqa: BLE001
                log.warning("could not close PR #%s: %s", t["pr"], e)
        self._stop(t, "cancelled", "Cancelled", tell=False)
        self.repo_factory(t).remove()

    # ------------------------------------------------------------ reviewing

    def run_review(self, t: dict) -> None:
        """The chosen reviewer reads the PR in a copy of its branch and gives a verdict."""
        tid = t["id"]
        rev = self._reviewer(t)
        rev_model = self.model_for(rev, t.get("reviewer_model") or "")
        rev_label, builder = self.model_label(rev, t.get("reviewer_model") or ""), self.label(t["agent"])
        self.db.event(tid, "review", f"{rev_label} is reviewing PR #{t['pr']}"
                      + (" (it also built it)" if rev == t["agent"] else ""))
        repo = self.repo_factory(t)
        try:
            repo.resume(t["branch"])
            head = repo.head()
            if head != t["head_sha"]:
                self.db.update(tid, head_sha=head)
            result = self._run("review", rev, rev_model,
                               prompts.review(self.label(rev), builder, t, self.cfg.base_branch),
                               repo.path, self.cfg.log_dir / f"task-{tid}-r{t['round']}-review.log",
                               self._stopping(tid), t["title"], task_id=tid)
            repo.reset_to(head)                      # a reviewer never changes the branch
            if result.stopped:
                return self._cancelled(t)
            if not result.ok:
                why = "ran out of time" if result.timed_out else f"stopped with an error (exit {result.exit_code})"
                return self._stop(t, "needs_joe", f"The reviewer ({rev_label}) {why}. See the log below.")
            text = review.review_section(self._full_log(
                self.cfg.log_dir / f"task-{tid}-r{t['round']}-review.log"))
            verdict = review.verdict(text)
            if verdict == review.UNKNOWN:
                return self._stop(t, "needs_joe", f"Couldn't find a verdict in {rev_label}'s review.")
            warn = ("\n\n> ⚠️ The builder reviewed its own work. A second opinion from another agent is safer."
                    if rev == t["agent"] else "")
            self.gh.comment(t["pr"], f"🧰 **Review by {rev_label}** (Agent Hub, round {t['round']}){warn}\n\n{text}")
            self.db.update(tid, review_sha=head, review_verdict=verdict, review_text=text)
            self.db.event(tid, "verdict", text[:6000])
            self.wake.set()
        except Exception as e:  # noqa: BLE001
            log.error("review of task %s failed:\n%s", tid, traceback.format_exc())
            self._stop(self.db.get(tid), "failed", f"Review failed: {type(e).__name__}: {e}"[:1500])

    # ------------------------------------------------------------- polling

    def poll(self, t: dict) -> None:
        tid = t["id"]
        pr = self.gh.pr(t["pr"])
        if pr.get("merged"):
            self._merged(t, str(pr.get("merge_commit_sha") or ""), "merged")
            return self._after_merge(t)
        if pr.get("state") == "closed":
            self._stop(t, "cancelled", f"PR #{t['pr']} was closed", tell=False)
            return
        conflict = pr.get("mergeable") is False and pr.get("mergeable_state") == "dirty"
        if conflict and (t["status"] == "ready" or t["review_sha"] == t["head_sha"]):
            return self._fix_conflict(t)
        if t["status"] == "ready":
            return
        head = (pr.get("head") or {}).get("sha")
        if head and t["head_sha"] and head != t["head_sha"]:
            # Someone (Joe) pushed to the branch: review the new commit.
            self.db.update(tid, head_sha=head, pushed_at=self.clock(), review_sha=None)
            self.db.event(tid, "pushed", "New commit on the branch: it gets a new review")
            return
        if t["review_sha"] != t["head_sha"]:
            return                                    # review not done yet (step runs it)

        ci = review.ci_state(self.gh.workflow_runs(t["head_sha"]))
        if ci is None:
            if self.clock() - (t["pushed_at"] or 0) > self.cfg.review_timeout:
                self._stop(t, "needs_joe", f"No CI result after {self.cfg.review_timeout // 60} min.")
            return

        verdict = t["review_verdict"]
        rev_label = self.label(self._reviewer(t))
        if verdict == review.OK and ci == "success":
            self.db.update(tid, status="ready", note=None)
            self.db.event(tid, "ready", "✅ Ready to merge")
            if t.get("auto_merge"):
                return self._auto_merge(self.db.get(tid))
            self.notify(f"✅ Task #{tid} ({t['title']}) is ready to merge: PR #{t['pr']}.")
            return
        if verdict == review.STOP:
            return self._stop(t, "needs_joe", f"{rev_label}'s review says Joe must look (⛔).")
        if t["round"] >= self.cfg.max_rounds:
            return self._stop(t, "needs_joe", f"Still not ready after {t['round']} fix rounds.")
        feedback = f"REVIEW BY {rev_label.upper()}:\n{(t['review_text'] or '').strip()}"
        if ci != "success":
            feedback += "\n\nCI FAILED on GitHub. Run the checks from AGENTS.md and make them pass."
        self.db.update(tid, status="queued", round=t["round"] + 1, feedback=feedback)
        self.db.event(tid, "fix", f"Fixes needed: starting round {t['round'] + 1}")
        self.wake.set()

    def _fix_conflict(self, t: dict) -> None:
        if t["round"] >= self.cfg.max_rounds + 2:
            return self._stop(t, "needs_joe", "The branch conflicts with main and the builder couldn't fix it.")
        self.db.update(t["id"], status="queued", round=t["round"] + 1, feedback=prompts.CONFLICT)
        self.db.event(t["id"], "conflict", "Main changed under this PR: the builder merges main and "
                                           f"fixes the conflict (round {t['round'] + 1})")
        self.wake.set()

    def _auto_merge(self, t: dict) -> None:
        """Joe asked for this task to merge itself. Never for protected files."""
        try:
            touched = [f for f in self.gh.pr_files(t["pr"]) if PROTECTED.match(f)]
        except Exception as e:  # noqa: BLE001
            touched = [f"(couldn't list the files: {e})"]
        if touched:
            note = ("Not merged automatically: it changes " + ", ".join(touched[:4])
                    + (" and more" if len(touched) > 4 else "") + ". Check it and merge it yourself.")
            self.db.update(t["id"], note=note)
            self.db.event(t["id"], "auto-merge", note)
            self.notify(f"✅ Task #{t['id']} ({t['title']}) is ready, but you must merge it: {note}")
            return
        err = self.merge(t["id"], how="merged automatically (✅ review, CI green)")
        if err:
            self.db.event(t["id"], "auto-merge", f"Couldn't merge automatically: {err}")
        else:
            self.notify(f"🤖 Task #{t['id']} ({t['title']}) was merged automatically and is deploying.")

    def _merged(self, t: dict, sha: str, how: str) -> None:
        """A PR got merged: watch its deploy, clean up the branch and work copy."""
        self.db.update(t["id"], status="merged", note=None, merge_sha=sha or None,
                       deploy="pending" if sha else None, merged_at=self.clock())
        self.db.event(t["id"], "merged", f"PR #{t['pr']} {how}" + ("; deploying" if sha else ""))
        self.repo_factory(t).remove()
        try:
            self.gh.delete_branch(t["branch"] or "")
        except Exception as e:  # noqa: BLE001 - a leftover branch is harmless
            log.warning("could not delete branch %s: %s", t["branch"], e)

    def poll_deploy(self, t: dict) -> None:
        """After a merge, CI on main tests and deploys to the VPS."""
        state = review.deploy_state(self.gh.workflow_runs(t["merge_sha"]))
        if state is None:
            if self.clock() - (t.get("merged_at") or 0) > self.cfg.deploy_timeout:
                self.db.update(t["id"], deploy="unknown")
                self.db.event(t["id"], "deploy", "No deploy result after "
                                                 f"{self.cfg.deploy_timeout // 60} min: check GitHub Actions")
            return
        self.db.update(t["id"], deploy=state)
        if state == "success":
            self.db.event(t["id"], "deployed", "✅ Live on the VPS")
        else:
            self.db.event(t["id"], "deploy", "Deploy failed. The league keeps running on the previous "
                                             "version. Open GitHub Actions and press Re-run.")
            self.notify(f"⚠️ Deploy of task #{t['id']} ({t['title']}) failed. The league runs on the "
                        "previous version; re-run it in GitHub Actions.")

    def _after_merge(self, t: dict) -> None:
        """Start the next task of the plan, or finish the plan."""
        if not t.get("plan_id"):
            return
        steps = self.db.plan_tasks(t["plan_id"])
        merged = {x["id"] for x in steps if x["status"] == "merged"}
        for x in steps:
            if x["status"] == "waiting" and set(self.db.after(x)) <= merged:
                self.db.update(x["id"], status="queued")
                self.db.event(x["id"], "start", f"Step {t['plan_order'] + 1} (task #{t['id']}) merged: starting")
                self.wake.set()
        plan = self.db.plan(t["plan_id"])
        if plan and plan["status"] == "approved" and all(
                x["status"] in ("merged", "cancelled") for x in self.db.plan_tasks(t["plan_id"])):
            self.db.update_plan(plan["id"], status="done")
            self.notify(f"🏁 Plan #{plan['id']} is done: every task is merged.")

    # ------------------------------------------------------------- managing

    def run_manager(self, p: dict) -> None:
        pid = p["id"]
        manager = self._first(p["manager"], "claude")
        team = self.enabled() or self.cfg.agents
        self.db.update_plan(pid, status="managing", note=None)
        repo = self.repo_factory({"plan": pid})
        log_path = self.cfg.log_dir / f"plan-{pid}.log"
        try:
            repo.fresh(f"plan-{pid}")                 # local only, never pushed
            result = self._run("manage", manager, self.model_for(manager, p.get("manager_model") or ""),
                               prompts.plan(self.label(manager), p["request"], team, p),
                               repo.path, log_path,
                               lambda: (self.db.plan(pid) or {}).get("status") == "cancelled",
                               p["request"].split("\n")[0][:120], plan_id=pid)
            if (self.db.plan(pid) or {}).get("status") == "cancelled":
                return
            if not result.ok:
                why = "ran out of time" if result.timed_out else f"stopped with an error (exit {result.exit_code})"
                self.db.update_plan(pid, status="failed", note=f"The manager {why}.")
                return
            summary, tasks = review.parse_plan(self._full_log(log_path), team,
                                               manager if manager in team else next(iter(team)))
            for task in tasks:
                task["reviewer"] = p["reviewer"]      # the page warns if it's also the builder
            self.db.update_plan(pid, status="proposed", summary=summary, tasks_json=json.dumps(tasks),
                                feedback=None, note=None)
            self.notify(f"📋 Plan #{pid} is ready for you to approve ({len(tasks)} tasks).")
        except review.PlanError as e:
            self.db.update_plan(pid, status="failed", note=str(e))
        except Exception as e:  # noqa: BLE001
            log.error("plan %s failed:\n%s", pid, traceback.format_exc())
            self.db.update_plan(pid, status="failed", note=f"{type(e).__name__}: {e}"[:1500])
        finally:
            repo.remove()

    # ------------------------------------------------------ Joe's buttons

    def approve_plan(self, plan_id: int, choices: list[dict] | None = None) -> str | None:
        """Start an approved plan. `choices` may change each task's builder/reviewer."""
        with self.lock:
            p = self.db.plan(plan_id)
            if not p or p["status"] != "proposed":
                return "Only a proposed plan can be approved."
            tasks = p["tasks"]
            for i, task in enumerate(tasks):
                pick = (choices or [])[i] if i < len(choices or []) else {}
                for key in ("builder", "reviewer"):
                    if pick.get(key) in self.cfg.agents:
                        task[key] = pick[key]
            ids: list[int] = []
            for i, task in enumerate(tasks):
                body = task["details"] + (f"\n\nPart of plan #{plan_id}: {p['request'][:500]}")
                reviewer = task.get("reviewer") or p["reviewer"]
                after_steps = task.get("after", [i - 1] if i else [])
                after = [ids[j] for j in after_steps if 0 <= j < len(ids)]
                tid = self.db.create_task(task["title"], body, task["done_when"], task["builder"],
                                          reviewer, plan_id, i, status="waiting" if after else "queued",
                                          reviewer_model=(p.get("reviewer_model") or "")
                                          if reviewer == p["reviewer"] else "",
                                          after=after, auto_merge=bool(p.get("auto_merge")))
                ids.append(tid)
                waits = (f"; starts after step {', '.join(str(j + 1) for j in after_steps)}"
                         if after else "; starts right away")
                self.db.event(tid, "created", f"Plan #{plan_id}, step {i + 1} of {len(tasks)}: "
                                              f"{self.label(task['builder'])} builds, "
                                              f"{self.label(reviewer)} reviews{waits}")
            self.db.update_plan(plan_id, status="approved", tasks_json=json.dumps(tasks))
        self.wake.set()
        return None

    def revise_plan(self, plan_id: int, notes: str) -> str | None:
        with self.lock:
            p = self.db.plan(plan_id)
            if not p or p["status"] not in ("proposed", "failed"):
                return "Only a proposed or failed plan can be changed."
            self.db.update_plan(plan_id, status="planning", feedback=notes.strip() or "Try again.")
        self.wake.set()
        return None

    def reject_plan(self, plan_id: int) -> None:
        with self.lock:
            p = self.db.plan(plan_id)
            if p and p["status"] not in PLAN_FINAL and p["status"] != "approved":
                self.db.update_plan(plan_id, status="rejected")

    def cancel_plan(self, plan_id: int) -> None:
        p = self.db.plan(plan_id)
        if not p or p["status"] in PLAN_FINAL:
            return
        self.db.update_plan(plan_id, status="cancelled")
        for t in self.db.plan_tasks(plan_id):
            if t["status"] == "waiting":
                self._stop(t, "cancelled", "Plan cancelled", tell=False)
            elif t["status"] not in ("merged", "cancelled"):
                self.cancel(t["id"])

    def cancel(self, task_id: int) -> None:
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] in ("merged", "cancelled"):
                return
            self.db.update(task_id, cancel=1)
            if t["status"] not in RUNNING:     # running: the worker stops it within a second
                self._cancelled(t)

    def resume(self, task_id: int, instructions: str = "") -> None:
        """Continue a stopped task (or start a waiting plan step early),
        optionally with Joe's instructions for the builder."""
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] in RUNNING + ("merged", "cancelled", "queued"):
                return
            text = instructions.strip()
            if t["pr"]:
                feedback = f"INSTRUCTIONS FROM JOE:\n{text}" if text else (
                    t["feedback"] or f"REVIEW:\n{t['review_text'] or ''}\n\nFix what the review and CI ask for.")
                self.db.update(task_id, status="queued", round=t["round"] + 1, feedback=feedback,
                               cancel=0, note=None)
            else:
                body = t["body"] + (f"\n\nMore instructions from Joe:\n{text}" if text else "")
                self.db.update(task_id, status="queued", body=body, cancel=0, note=None)
            self.db.event(task_id, "continue", text or "Continue")
        self.wake.set()

    def merge(self, task_id: int, how: str = "merged from the hub") -> str | None:
        """Merge a ready PR. Returns an error message, or None."""
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] != "ready":
                return "Only a task marked ready can be merged here."
            try:
                sha = self.gh.merge_pr(t["pr"], f"[{t['agent']}] {t['title']} (#{t['pr']})")
            except Exception as e:  # noqa: BLE001 - e.g. a conflict GitHub found first
                return str(e)
            self._merged(t, sha, how)
        self._after_merge(t)
        return None


def log_text(cfg: Config, task: dict, limit: int = 20000) -> str:
    """The newest log of a task, for the page."""
    logs = sorted(Path(cfg.log_dir).glob(f"task-{task['id']}-*.log"),
                  key=lambda p: p.stat().st_mtime)
    return agents.tail(logs[-1], limit) if logs else ""


def plan_log(cfg: Config, plan_id: int, limit: int = 20000) -> str:
    path = Path(cfg.log_dir) / f"plan-{plan_id}.log"
    return agents.tail(path, limit) if path.exists() else ""
