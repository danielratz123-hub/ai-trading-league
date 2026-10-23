"""Reading Claude's automatic review (.github/workflows/claude-review.yml)
and the CI result for the PR's latest commit."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .github import parse_time

REVIEW_WORKFLOW = "Claude review"
CI_WORKFLOW = "CI"
REVIEWER_LOGINS = ("claude[bot]", "claude")

OK, FIX, STOP, UNKNOWN = "ok", "fix", "stop", "unknown"


def verdict(text: str) -> str:
    """The verdict line of a review summary -> ok / fix / stop / unknown."""
    m = re.search(r"verdict[:*\s]*([^\n]*)", text, re.IGNORECASE)
    if not m:
        return UNKNOWN
    line = m.group(1).lower()
    if "⛔" in line or "stop" in line:
        return STOP
    if "🔧" in line or "needs fix" in line:
        return FIX
    if "✅" in line or "ready to merge" in line:
        return OK
    return UNKNOWN


def latest_summary(comments: list[dict], since: float) -> dict | None:
    """Newest review summary by Claude posted after `since` (the hub's push)."""
    found = None
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        if login in REVIEWER_LOGINS and parse_time(c.get("created_at")) >= since - 5 \
                and re.search(r"verdict", c.get("body", ""), re.IGNORECASE):
            found = c
    return found


def inline_since(comments: list[dict], since: float) -> list[str]:
    out = []
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        if login in REVIEWER_LOGINS and parse_time(c.get("created_at")) >= since - 5:
            line = c.get("line") or c.get("original_line") or "?"
            out.append(f"- {c.get('path', '?')}:{line}: {c.get('body', '').strip()}")
    return out


@dataclass
class ChecksState:
    done: bool            # both CI and the review run have finished
    ci: str | None        # conclusion of CI ("success", "failure", ...) or None
    review: str | None    # conclusion of the review run, or None


def checks_state(runs: list[dict]) -> ChecksState:
    def pick(name: str) -> dict | None:
        # PR runs first: the push run of CI has the same name.
        matches = [r for r in runs if r["name"] == name]
        matches.sort(key=lambda r: r.get("event") != "pull_request")
        return matches[0] if matches else None

    ci, rv = pick(CI_WORKFLOW), pick(REVIEW_WORKFLOW)
    ci_c = ci["conclusion"] if ci and ci["status"] == "completed" else None
    rv_c = rv["conclusion"] if rv and rv["status"] == "completed" else None
    return ChecksState(done=ci_c is not None and rv_c is not None, ci=ci_c, review=rv_c)
