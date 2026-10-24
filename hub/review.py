"""Reading a reviewer's verdict, a manager's plan, and the CI result."""
from __future__ import annotations

import json
import re

CI_WORKFLOW = "CI"

OK, FIX, STOP, UNKNOWN = "ok", "fix", "stop", "unknown"
MAX_PLAN_TASKS = 6


def verdict(text: str) -> str:
    """The verdict line of a review -> ok / fix / stop / unknown."""
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


def review_section(output: str) -> str:
    """The review at the end of a reviewer's output: from its last
    "Verdict" line to the end. (Some CLIs echo the prompt first, which also
    mentions the format, so the LAST one is the answer.)"""
    hits = list(re.finditer(r"(\*\*)?verdict(\*\*)?\s*:", output, re.IGNORECASE))
    if not hits:
        return ""
    return output[hits[-1].start():].strip()[:8000]


def ci_state(runs: list[dict]) -> str | None:
    """Conclusion of the CI workflow for a commit ("success", "failure", ...),
    or None while it hasn't finished (or started)."""
    matches = [r for r in runs if r["name"] == CI_WORKFLOW]
    matches.sort(key=lambda r: r.get("event") != "pull_request")   # PR run first
    if not matches or matches[0]["status"] != "completed":
        return None
    return matches[0]["conclusion"] or "unknown"


def deploy_state(runs: list[dict]) -> str | None:
    """The deploy of a merge commit = the CI run of its push to main
    (lint, tests, then deploy to the VPS). "success" / "failure" / None
    while it runs or hasn't started."""
    pushes = [r for r in runs if r["name"] == CI_WORKFLOW and r.get("event") == "push"]
    if not pushes or pushes[0]["status"] != "completed":
        return None
    return "success" if pushes[0]["conclusion"] == "success" else "failure"


class PlanError(ValueError):
    pass


def parse_plan(output: str, agents: dict, fallback_builder: str) -> tuple[str, list[dict]]:
    """The manager's plan, between the LAST BEGIN_PLAN and END_PLAN lines.
    Returns (summary, tasks). Unknown builders fall back to the manager."""
    start = output.rfind("BEGIN_PLAN")
    end = output.find("END_PLAN", start)
    if start < 0 or end < 0:
        raise PlanError("The manager didn't write a plan (BEGIN_PLAN ... END_PLAN).")
    raw = output[start + len("BEGIN_PLAN"):end].strip().strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlanError(f"The plan isn't valid JSON ({e.msg}).") from e
    tasks_in = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks_in, list) or not tasks_in:
        raise PlanError("The plan has no tasks.")
    kept: list[tuple[int, dict]] = []          # (step number the manager used, task)
    for n, item in enumerate(tasks_in[:MAX_PLAN_TASKS], start=1):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        builder = str(item.get("builder", "")).strip().lower()
        kept.append((n, {
            "title": str(item["title"]).strip()[:200],
            "details": str(item.get("details", "")).strip()[:8000],
            "done_when": str(item.get("done_when", "")).strip()[:2000],
            "builder": builder if builder in agents else fallback_builder,
            "why": str(item.get("why", "")).strip()[:500],
            "_after": item.get("after", None),
        }))
    if not kept:
        raise PlanError("The plan has no usable tasks.")
    # "after": the earlier steps a step needs merged first. Missing = the step
    # just before it (the safe, one-by-one default). Renumbered to our order.
    position = {n: i for i, (n, _) in enumerate(kept)}
    tasks = []
    for i, (n, task) in enumerate(kept):
        raw = task.pop("_after")
        if raw is None:
            after = [i - 1] if i else []
        else:
            nums = raw if isinstance(raw, list) else [raw]
            after = sorted({position[int(x)] for x in nums
                            if str(x).lstrip("-").isdigit() and int(x) in position and position[int(x)] < i})
        task["after"] = after                  # 0-based indexes of earlier steps
        tasks.append(task)
    return str(data.get("summary", "")).strip()[:3000], tasks
