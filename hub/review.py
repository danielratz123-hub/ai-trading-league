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
    tasks = []
    for item in tasks_in[:MAX_PLAN_TASKS]:
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        builder = str(item.get("builder", "")).strip().lower()
        tasks.append({
            "title": str(item["title"]).strip()[:200],
            "details": str(item.get("details", "")).strip()[:8000],
            "done_when": str(item.get("done_when", "")).strip()[:2000],
            "builder": builder if builder in agents else fallback_builder,
            "why": str(item.get("why", "")).strip()[:500],
        })
    if not tasks:
        raise PlanError("The plan has no usable tasks.")
    return str(data.get("summary", "")).strip()[:3000], tasks
