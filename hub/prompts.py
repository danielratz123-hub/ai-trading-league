"""What the hub tells an agent. The agent also reads AGENTS.md itself."""
from __future__ import annotations

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


def pr_body(task: dict, agent_label: str, summary: str, checks_ok: bool) -> str:
    warn = "" if checks_ok else (
        "\n> ⚠️ The hub's own run of the CI checks did **not** pass. See the CI result below.\n")
    return f"""Closes #{task['issue']}

**Built by:** {agent_label}, through the Agent Hub (task #{task['id']}).
{warn}
**Agent's summary**

{summary.strip()[-5000:] or '(the agent gave no summary)'}

---
The Agent Hub watches this PR: when Claude's review asks for fixes, it sends them
back to {agent_label}. Joe merges.
"""
