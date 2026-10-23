from hub.agents import RunResult
from hub.db import DB
from hub.worker import Worker

from .fakes import Agents, Clock, FakeGitHub, FakeRepo, make_cfg

OK = "All good.\n**Verdict:** ✅ Ready to merge\n**Must fix:** none"
FIX = "**Verdict:** 🔧 Needs fixes\n**Must fix:**\n1. arena/x.py:12 handle missing SPY data"
STOP = "**Verdict:** ⛔ Stop, Joe must look\n**Must fix:** touches risk limits"


def setup(tmp_path, **cfg_kw):
    cfg = make_cfg(tmp_path, **cfg_kw)
    db = DB(":memory:")
    gh, ag, clock, sent = FakeGitHub(), Agents(), Clock(), []
    repos: dict = {}

    def factory(t):
        key = ("plan", t["plan"]) if "plan" in t else t["id"]
        repos.setdefault(key, FakeRepo(t, tmp_path))
        return repos[key]

    w = Worker(cfg, db, gh, repo_factory=factory, run_agent=ag.run_agent,
               run_checks=ag.run_checks, notify=sent.append, clock=clock)
    return w, db, gh, ag, clock, sent, repos


def start(db, agent="codex", reviewer="claude"):
    return db.create_task("Add SPY player", "Score SPY like the agents", "SPY on the scoreboard",
                          agent, reviewer)


def build_and_review(w):
    w.step()      # build + push + PR
    w.step()      # reviewer runs


def test_happy_path_to_ready(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    t = db.get(tid)
    assert t["status"] == "reviewing" and t["review_sha"] is None
    assert t["issue"] == 101 and gh.issues[0][2] == ["codex"]
    assert "Who reviews it\nclaude" in gh.issues[0][1]
    assert t["branch"] == "hub/codex-101-add-spy-player"
    assert repos[tid].calls[0] == ("fresh", "hub/codex-101-add-spy-player")
    pr = gh.prs[t["pr"]]
    assert "Closes #101" in pr["body"] and "**Claude** reviews it" in pr["body"]
    assert ag.calls[0] == ("build", "codex")

    w.step()                                   # the chosen reviewer (Claude) reviews
    t = db.get(tid)
    assert ag.calls[-1] == ("review", "claude")
    assert t["review_sha"] == t["head_sha"] and t["review_verdict"] == "ok"
    assert ("reset", t["head_sha"]) in repos[tid].calls     # reviewer can't change the branch
    assert "Review by Claude" in gh.comments[-1][1] and "Ready to merge" in gh.comments[-1][1]

    w.step()                                   # CI not finished yet
    assert db.get(tid)["status"] == "reviewing"
    gh.ci("success")
    w.step()
    assert db.get(tid)["status"] == "ready"
    assert any("ready to merge" in s for s in sent)


def test_reviewer_choice_is_used(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    start(db, agent="claude", reviewer="gemini")
    build_and_review(w)
    assert ag.calls == [("build", "claude"), ("review", "gemini")]
    assert "Review by Gemini" in gh.comments[-1][1]


def test_self_review_is_allowed_with_a_warning(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    start(db, agent="codex", reviewer="codex")
    build_and_review(w)
    assert ag.calls == [("build", "codex"), ("review", "codex")]
    assert "reviewed its own work" in gh.comments[-1][1]
    assert "you also built this change" in ag.prompts[-1]


def test_needs_fixes_runs_a_fix_round_with_the_review(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    ag.reviews = [FIX, OK]
    build_and_review(w)
    gh.ci("success")
    w.step()                                    # reads verdict -> queues round 1 -> builder fixes
    t = db.get(tid)
    assert t["round"] == 1 and t["status"] == "reviewing"
    assert ("resume", t["branch"]) in repos[tid].calls
    fix_prompt = [p for p in ag.prompts if "fix round" in p][-1]
    assert "handle missing SPY data" in fix_prompt and "REVIEW BY CLAUDE" in fix_prompt
    w.step()                                    # second review sees its first one
    assert "Your previous review" in ag.prompts[-1]
    gh.ci("success")
    w.step()
    assert db.get(tid)["status"] == "ready"


def test_stops_after_max_rounds(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, max_rounds=1)
    tid = start(db)
    ag.reviews = [FIX, FIX]
    build_and_review(w)
    gh.ci("success")
    w.step()                                    # round 1 build
    w.step()                                    # review round 1
    w.step()
    t = db.get(tid)
    assert t["status"] == "needs_joe" and "1 fix rounds" in t["note"]
    assert any("needs you" in s for s in sent)


def test_stop_verdict_and_ci_failure(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    ag.reviews = [STOP]
    build_and_review(w)
    gh.ci("success")
    w.step()
    assert db.get(tid)["status"] == "needs_joe"

    w2, db2, gh2, ag2, *_ = setup(tmp_path / "b")
    tid2 = start(db2, "gemini")
    build_and_review(w2)
    gh2.ci("failure")
    w2.step()                                   # ✅ but CI red -> fix round
    assert db2.get(tid2)["round"] == 1
    assert "CI FAILED" in [p for p in ag2.prompts if "fix round" in p][-1]


def test_review_without_a_verdict_needs_joe(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    ag.reviews = ["I looked at it, seems ok"]
    build_and_review(w)
    assert db.get(tid)["status"] == "needs_joe"


def test_forbidden_files_are_never_pushed(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    orig = w.repo_factory

    def factory(t):
        r = orig(t)
        r.files = ["arena/x.py", ".env"]
        return r
    w.repo_factory = factory
    w.step()
    t = db.get(tid)
    assert t["status"] == "needs_joe" and ".env" in t["note"]
    assert not any(c[0] == "push" for c in repos[tid].calls) and not gh.prs


def test_a_secret_in_the_diff_is_never_pushed(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, token="ghp_very_secret_token_123")
    tid = start(db)
    orig = w.repo_factory

    def factory(t):
        r = orig(t)
        r.diff = "+TOKEN = 'ghp_very_secret_token_123'"
        return r
    w.repo_factory = factory
    w.step()
    assert db.get(tid)["status"] == "needs_joe" and not gh.prs


def test_failing_checks_go_back_to_the_builder_twice(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    ag.check_results = [RunResult(False, 1, "1 failed")] * 3
    tid = start(db)
    w.step()
    t = db.get(tid)
    assert [c[0] for c in ag.calls] == ["build", "build", "build"]
    assert "CI checks failed" in ag.prompts[1]
    assert t["status"] == "reviewing" and "own test run failed" in t["note"]
    assert "did **not** pass" in gh.prs[t["pr"]]["body"]


def test_agent_error_needs_joe(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    ag.agent_results = [RunResult(False, 1, "not logged in")]
    tid = start(db)
    w.step()
    assert db.get(tid)["status"] == "needs_joe" and not gh.prs


def test_cancel_closes_the_pr(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    w.cancel(tid)
    t = db.get(tid)
    assert t["status"] == "cancelled" and gh.closed == [t["pr"]]


def test_merge_only_when_ready(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    build_and_review(w)
    assert w.merge(tid) is not None and not gh.merged
    gh.ci("success")
    w.step()
    assert w.merge(tid) is None
    assert db.get(tid)["status"] == "merged" and gh.merged == [db.get(tid)["pr"]]


def test_new_commit_from_joe_gets_a_new_review(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    build_and_review(w)
    gh.prs[db.get(tid)["pr"]]["head_sha"] = "joes-commit"
    repos[tid].head_override = "joes-commit"
    w.step()                                    # sees the new head, then reviews it
    t = db.get(tid)
    assert t["head_sha"] == "joes-commit" and t["review_sha"] == "joes-commit"
    assert ag.calls.count(("review", "claude")) == 2


def test_continue_with_instructions(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    ag.reviews = [STOP]
    build_and_review(w)
    gh.ci("success")
    w.step()
    w.resume(tid, "Use the existing benchmark table instead")
    t = db.get(tid)
    assert t["status"] == "queued" and t["round"] == 1
    w.step()
    assert "INSTRUCTIONS FROM JOE" in [p for p in ag.prompts if "fix round" in p][-1]


def test_restart_mid_run_needs_joe(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    db.update(tid, status="building")
    pid = db.create_plan("Do things please", "claude", "claude")
    db.update_plan(pid, status="managing")
    w.recover()
    assert db.get(tid)["status"] == "needs_joe"
    assert db.plan(pid)["status"] == "planning"


def test_ci_timeout(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, review_timeout=60)
    tid = start(db)
    build_and_review(w)
    clock.t += 61
    w.step()
    assert db.get(tid)["status"] == "needs_joe"


# ------------------------------------------------------------------ plans

def test_manager_plans_then_joe_approves_and_steps_run_in_order(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    pid = db.create_plan("Add an SPY benchmark and show it", "gemini", "claude")
    w.step()                                    # manager runs
    p = db.plan(pid)
    assert ag.calls == [("manager", "gemini")]
    assert p["status"] == "proposed" and p["summary"] == "Two steps."
    assert [t["builder"] for t in p["tasks"]] == ["codex", "gemini"]   # unknown builder -> manager
    assert all(t["reviewer"] == "claude" for t in p["tasks"])
    assert repos[("plan", pid)].calls[-1] == ("remove",)
    assert any("ready for you to approve" in s for s in sent)
    assert not db.list()                        # nothing runs before Joe approves

    assert w.approve_plan(pid, [{"builder": "claude"}, {"reviewer": "codex"}]) is None
    tasks = db.plan_tasks(pid)
    assert [t["status"] for t in tasks] == ["queued", "waiting"]
    assert [(t["agent"], t["reviewer"]) for t in tasks] == [("claude", "claude"), ("gemini", "codex")]
    assert db.plan(pid)["status"] == "approved"

    build_and_review(w)
    gh.ci("success")
    w.step()                                    # step 1 ready
    assert db.get(tasks[0]["id"])["status"] == "ready"
    assert db.get(tasks[1]["id"])["status"] == "waiting"
    w.merge(tasks[0]["id"])
    assert db.get(tasks[1]["id"])["status"] == "queued"

    build_and_review(w)
    gh.ci("success")
    w.step()
    gh.prs[db.get(tasks[1]["id"])["pr"]]["merged"] = True   # Joe merges on GitHub
    w.step()
    assert db.plan(pid)["status"] == "done"
    assert any("Plan #1 is done" in s for s in sent)


def test_ask_the_manager_for_changes(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    pid = db.create_plan("Add an SPY benchmark", "claude", "codex")
    w.step()
    assert w.revise_plan(pid, "Make it one task") is None
    assert db.plan(pid)["status"] == "planning"
    w.step()
    assert "Make it one task" in ag.prompts[-1] and "Your previous plan was" in ag.prompts[-1]
    assert db.plan(pid)["status"] == "proposed"


def test_bad_plan_fails_and_reject_and_cancel(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    ag.plan_output = "I think we should do stuff."
    pid = db.create_plan("Add an SPY benchmark", "claude", "claude")
    w.step()
    assert db.plan(pid)["status"] == "failed" and "BEGIN_PLAN" in db.plan(pid)["note"]

    ag.plan_output = __import__("hub.tests.fakes", fromlist=["PLAN_OUTPUT"]).PLAN_OUTPUT
    pid2 = db.create_plan("Another one", "claude", "claude")
    w.step()
    w.reject_plan(pid2)
    assert db.plan(pid2)["status"] == "rejected"
    assert w.approve_plan(pid2) is not None

    pid3 = db.create_plan("Third", "claude", "claude")
    w.step()
    w.approve_plan(pid3)
    w.step()                                    # step 1 builds, PR open
    w.cancel_plan(pid3)
    assert {t["status"] for t in db.plan_tasks(pid3)} == {"cancelled"}
    assert db.plan(pid3)["status"] == "cancelled"
