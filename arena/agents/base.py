"""The agentic loop shared by every model provider.

    briefing -> model -> tool calls -> results -> model -> ... -> submit_decision

The loop, budget accounting, retries and nudges live here, once, so every
agent plays by exactly the same rules. A provider adapter only translates:
it implements start / step / send_tool_results / send_user_text for its
API. Adding a third agent (e.g. Llama) = one new adapter class.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import SETTINGS
from ..tools.registry import ToolBox
from .decision import SUBMIT_SPEC, Checked


@dataclass
class Usage:
    input: int = 0          # uncached input tokens
    cached: int = 0         # input tokens read from the prompt cache
    cache_write: int = 0    # input tokens written to the cache
    output: int = 0         # output tokens (including reasoning/thinking)

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cached += other.cached
        self.cache_write += other.cache_write
        self.output += other.output


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict | None
    parse_error: str | None = None   # set when the model sent malformed JSON


@dataclass
class Step:
    """One model response, translated to a provider-neutral shape."""
    tool_calls: list[ToolCall]
    text: str
    usage: Usage
    web_searches: int = 0
    stop: str = "end"            # end | tool_use | max_tokens | refusal
    served_by: str | None = None # model that actually answered (refusal fallbacks)
    raw: Any = None              # provider response, for the transcript


@dataclass
class TurnOutcome:
    accepted: Checked | None
    decision: dict | None
    error: str | None
    attempts: int
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    web_searches: int = 0
    last_errors: list[str] = field(default_factory=list)   # from the last rejected submission


def stopped_reason(attempts: int, last_errors: list[str]) -> str:
    """Why a turn ended without a decision, in words a human can act on."""
    if attempts and last_errors:
        return ("Its last decision was rejected and it didn't resubmit: "
                + "; ".join(e.splitlines()[0] for e in last_errors)[:400])
    return "The model stopped without calling submit_decision."


class AgentAdapter(ABC):
    """One provider's API, wrapped. Holds the conversation state of one turn."""

    def __init__(self, agent_id: str, model: str):
        self.agent_id = agent_id
        self.model = model

    @abstractmethod
    def start(self, system: str, briefing: str, tools: list[dict], web_search: bool,
              max_web_searches: int) -> None:
        """Begin a new conversation. `tools` are provider-neutral specs."""

    @abstractmethod
    def step(self) -> Step:
        """Send the conversation so far and return the model's next response."""

    @abstractmethod
    def send_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        """Queue (call, output text, is_error) results for the next step."""

    @abstractmethod
    def send_user_text(self, text: str) -> None:
        """Queue a plain user message for the next step."""

    @abstractmethod
    def price_key(self) -> str:
        """Key into SETTINGS.prices_per_mtok."""

    def cost(self, usage: Usage, web_searches: int) -> float:
        p_in, p_cached, p_write, p_out = SETTINGS.prices_per_mtok.get(
            self.price_key(), SETTINGS.prices_per_mtok["claude-opus-5-5"])
        return ((usage.input * p_in + usage.cached * p_cached + usage.cache_write * p_write
                 + usage.output * p_out) / 1e6 + web_searches * SETTINGS.web_search_price_usd)


NUDGE = ("You haven't submitted a decision yet. Call the submit_decision tool now with your final "
         "portfolio (an empty slots list keeps everything in cash). {budget}")
RETRIES_LEFT = "\n\nFix the problems and call submit_decision again ({left} retr{y} left)."
NO_RETRIES = "\n\nNo retries left: your portfolio stays unchanged this week."

# log(role, content) stores one transcript entry
LogFn = Callable[[str, Any], None]
# submit(args, attempt_no) validates a decision
SubmitFn = Callable[[dict, int], Checked]


def run_turn(adapter: AgentAdapter, toolbox: ToolBox, system: str, briefing: str,
             submit: SubmitFn, log: LogFn) -> TurnOutcome:
    budget = toolbox.budget
    out = TurnOutcome(None, None, None, 0)
    hard_limit = budget.max_spend_usd * SETTINGS.hard_spend_multiplier
    tools = toolbox.specs() + [SUBMIT_SPEC]
    adapter.start(system, briefing, tools, toolbox.web_search_enabled, budget.max_calls)
    log("system", system)
    log("user", briefing)
    nudges = calls_after_budget = 0

    def fail(msg: str) -> TurnOutcome:
        out.error = msg
        log("engine", {"turn_failed": msg})
        return out

    while True:
        if out.cost_usd >= hard_limit:
            return fail(f"Stopped: API spend ${out.cost_usd:.3f} hit the hard limit ${hard_limit:.2f}.")
        if budget.exhausted:
            calls_after_budget += 1
            if calls_after_budget > SETTINGS.max_calls_after_budget:
                return fail("Research budget exhausted and no valid decision was submitted.")
        try:
            step = adapter.step()
        except Exception as e:  # noqa: BLE001 - SDKs raise many types; the turn just fails
            return fail(f"API error ({type(e).__name__}): {str(e)[:500]}")

        out.model_calls += 1
        cost = adapter.cost(step.usage, step.web_searches)
        out.usage.add(step.usage)
        out.cost_usd += cost
        out.web_searches += step.web_searches
        budget.spend_usd += cost
        budget.calls += step.web_searches      # web searches use the same 40-call budget
        log("assistant", {"text": step.text, "stop": step.stop, "served_by": step.served_by,
                          "cost_usd": round(cost, 5), "raw": step.raw})

        if step.stop == "refusal":
            return fail("The model declined the request (safety refusal).")
        if not step.tool_calls:
            if nudges >= SETTINGS.max_nudges:
                return fail(stopped_reason(out.attempts, out.last_errors))
            nudges += 1
            note = " Your last response hit the output limit; be more concise." if step.stop == "max_tokens" else ""
            adapter.send_user_text(NUDGE.format(budget=f"(Budget: {budget.status()}.){note}"))
            log("user", "nudge")
            continue

        results: list[tuple[ToolCall, str, bool]] = []
        for call in step.tool_calls:
            if call.parse_error:
                results.append((call, f"ERROR: could not parse your arguments: {call.parse_error}", True))
                continue
            if call.name == SUBMIT_SPEC["name"]:
                if out.accepted is not None:
                    results.append((call, "Ignored: a decision was already accepted.", True))
                    continue
                out.attempts += 1
                checked = submit(call.args or {}, out.attempts)
                if checked.ok:
                    out.accepted, out.decision = checked, call.args
                    results.append((call, checked.message, False))
                else:
                    out.last_errors = checked.errors
                    left = SETTINGS.max_decision_retries + 1 - out.attempts
                    tail = RETRIES_LEFT.format(left=left, y="y" if left == 1 else "ies") if left > 0 else NO_RETRIES
                    results.append((call, checked.message + tail, True))
            else:
                out.tool_calls += 1
                text = toolbox.call(call.name, call.args)
                results.append((call, text, text.startswith('{"error"')))
        log("tool", [{"id": c.id, "name": c.name, "args": c.args, "result": r[:20_000], "is_error": e}
                     for c, r, e in results])

        if out.accepted is not None:
            return out
        if out.attempts > SETTINGS.max_decision_retries:
            return fail("Decision failed validation after all retries: "
                        + "; ".join(e.splitlines()[0] for e in out.last_errors)[:400])
        adapter.send_tool_results(results)


def to_json(obj: Any) -> str:
    """Best-effort JSON for transcripts (SDK objects -> dicts)."""
    def default(o):
        for attr in ("model_dump", "to_dict"):
            if hasattr(o, attr):
                try:
                    return getattr(o, attr)()
                except Exception:  # noqa: BLE001
                    pass
        return str(o)
    return json.dumps(obj, default=default)
