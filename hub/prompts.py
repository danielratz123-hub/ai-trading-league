"""What the hub tells an agent. The agent also reads AGENTS.md itself."""
from __future__ import annotations

import json

from .review import MAX_PLAN_TASKS

CHECKS = """  ruff check --select F arena hub
  env -u CODEX_HOME python -m pytest -q -p no:cacheprovider arena/tests hub/tests"""

COMMON = f"""Rules for this run:
- Work only in this folder. Do NOT push, open PRs, or change git remotes or branches:
  the Agent Hub does that after you finish.
- Never read, print or create .env files, keys, tokens or anything under data/.
- Before you finish, run the same checks as CI and make sure both pass:
{CHECKS}
- You may commit. Anything left uncommitted is committed for you.
- End with a short summary in plain, simple English: what you changed, how you
  tested it, and anything Joe must do on the VPS after merging (or "nothing").
  It goes into the pull request, and Joe reads it on his phone."""


def build(agent_label: str, task: dict) -> str:
    return f"""You are {agent_label}, a builder on the AI Trading League repo. This folder is
your own copy of the repo with branch `{task['branch']}` checked out.
Read AGENTS.md first and follow every rule in it.

TASK (GitHub issue #{task['issue']}): {task['title']}

{task['body'].strip() or '(no further description)'}

Done when: {task['done_when'].strip() or 'the task above works, with tests'}

{COMMON}
"""


def fix(agent_label: str, task: dict, feedback: str, max_rounds: int) -> str:
    return f"""You are {agent_label}, a builder on the AI Trading League repo. This folder is
your copy of the repo with your branch `{task['branch']}` checked out; it is the
branch of pull request #{task['pr']} (task: {task['title']}).
Read AGENTS.md and follow every rule in it.

This is fix round {task['round']} of at most {max_rounds}. Please address this feedback:

{feedback.strip()}

Fix every "Must fix" point. If you disagree with a point, don't change it: explain
why in your final summary instead.

{COMMON}
"""


def checks_failed(output_tail: str) -> str:
    return ("The CI checks failed on your change. Fix the code (not the tests, unless a "
            "test itself is wrong) so both checks pass. Last part of the output:\n\n"
            f"```\n{output_tail[-4000:]}\n```")


def pr_body(task: dict, agent_label: str, reviewer_label: str, summary: str, checks_ok: bool) -> str:
    warn = "" if checks_ok else (
        "\n> ⚠️ The hub's own run of the CI checks did **not** pass. See the CI result below.\n")
    return f"""Closes #{task['issue']}

**Built by:** {agent_label}, through the Agent Hub (task #{task['id']}).
{warn}
**Agent's summary**

{summary.strip()[-5000:] or '(the agent gave no summary)'}

---
The Agent Hub watches this PR: **{reviewer_label}** reviews it, and when the review asks
for fixes the hub sends them back to {agent_label}. Joe merges.
"""


REVIEW_FORMAT = """**Verdict:** ✅ Ready to merge  /  🔧 Needs fixes  /  ⛔ Stop, Joe must look
**Must fix:** numbered list, each with file and line (or "none")
**Should fix:** short list (or "none")
**After merging, Joe must:** anything to do on the VPS (or "nothing")"""


def review(reviewer_label: str, builder_label: str, task: dict, base: str) -> str:
    previous = ""
    if task.get("review_text") and task["round"]:
        previous = ("\nYour previous review of this PR said:\n\n"
                    f"{task['review_text'].strip()[-3000:]}\n\n"
                    "Check that those Must-fix points were fixed, and don't repeat fixed ones.\n")
    self_note = ("\nNote: you also built this change. Review it as strictly as someone else's work.\n"
                 if reviewer_label == builder_label else "")
    return f"""You are {reviewer_label}, the REVIEWER of pull request #{task['pr']} on the AI Trading
League repo. This folder is a copy of the PR's branch `{task['branch']}`.
Do NOT change, commit or push anything. Only read files and run read-only commands
(git diff, git log, the tests).
{self_note}
The change was built by {builder_label} for this task (GitHub issue #{task['issue']}):
{task['title']}

{task['body'].strip() or '(no further description)'}

Done when: {task['done_when'].strip() or 'the task above works, with tests'}

See the change with:  git diff origin/{base}...HEAD
You may run the CI checks:
{CHECKS}

Review it against AGENTS.md (especially "Hard rules" and "Code Review Rules") and the
"Done when" above.
{previous}
Finish your answer with the review in exactly this format, in plain simple English
(Joe reads it on his phone). Pick ONE verdict:

{REVIEW_FORMAT}

Use ⛔ for anything touching secrets, deploys, risk limits or real orders.
"""


def plan(manager_label: str, request: str, agents: dict, previous: dict | None = None) -> str:
    builders = ", ".join(f"`{k}` ({v['label']})" for k, v in agents.items())
    redo = ""
    if previous and previous.get("feedback"):
        redo = ("\nYou proposed a plan before. Joe asked for changes:\n\n"
                f"{previous['feedback'].strip()}\n\nYour previous plan was:\n\n"
                f"{json.dumps({'summary': previous.get('summary'), 'tasks': previous.get('tasks')}, indent=2)[:6000]}\n\n"
                "Make a new plan that follows Joe's notes.\n")
    return f"""You are {manager_label}, the MANAGER of a small team of coding agents on the
AI Trading League repo. This folder is a copy of `main`. Do NOT change any files: you only plan.
Read AGENTS.md, README.md and ROADMAP.md, and look at the code you need to plan well.

Joe's request:
{request.strip()}
{redo}
Turn it into a plan of 1 to {MAX_PLAN_TASKS} small tasks. Each task becomes one pull request (keep them
small, see AGENTS.md). They are built one after another, in your order, and each task starts
from `main` after the one before it is merged, so later tasks can build on earlier ones.
For each task pick the builder you think fits best: {builders}.
Write titles, details and "done when" in plain simple English, specific enough that the
builder needs nothing else: name the files and functions to change when you know them.

End your answer with the plan as JSON between a line BEGIN_PLAN and a line END_PLAN:

BEGIN_PLAN
{{"summary": "one or two sentences for Joe",
  "tasks": [{{"title": "...", "details": "...", "done_when": "...", "builder": "codex", "why": "why this builder"}}]}}
END_PLAN
"""
