"""Scripted stand-ins for the model adapters: no network, no cost."""
from __future__ import annotations

from arena.agents.base import AgentAdapter, Step, ToolCall, Usage


class ScriptedAgent(AgentAdapter):
    """Plays a fixed script. Each step is a list of (tool_name, args) calls,
    or a string (a text-only reply with no tool call). Records everything it
    was sent so tests can inspect it."""

    def __init__(self, agent_id: str, script: list, usage: Usage | None = None, web_searches: int = 0):
        super().__init__(agent_id, f"fake-{agent_id}")
        self.script = list(script)
        self.usage = usage or Usage(input=1000, output=200)
        self.web_searches = web_searches
        self.received: list = []
        self.n = 0

    def price_key(self):
        return "claude-opus-5-5"

    def start(self, system, briefing, tools, web_search, max_web_searches):
        self.system, self.briefing, self.tools, self.web_search = system, briefing, tools, web_search

    def step(self) -> Step:
        self.n += 1
        if not self.script:
            return Step([], "I'm done.", self.usage)
        item = self.script.pop(0)
        if isinstance(item, str):
            return Step([], item, self.usage)
        calls = [ToolCall(f"call_{self.n}_{i}", name, args) for i, (name, args) in enumerate(item)]
        return Step(calls, "", self.usage, self.web_searches, "tool_use")

    def send_tool_results(self, results):
        self.received.append([(c.name, text, err) for c, text, err in results])

    def send_user_text(self, text):
        self.received.append(text)


def submit(slots, view="view", notes="notes"):
    return ("submit_decision", {"market_view": view, "slots": slots, "notes_to_self": notes})


def manual(slot, alloc, positions, name="s", risk_orders=None):
    d = {"slot": slot, "name": name, "type": "manual", "allocation": alloc, "positions": positions,
         "reason": "test"}
    if risk_orders:
        d["risk_orders"] = risk_orders
    return d


class Factory:
    """agent_id -> list of scripts (one per week, consumed in order)."""

    def __init__(self, scripts: dict[str, list[list]]):
        self.scripts = {a: list(s) for a, s in scripts.items()}
        self.made: dict[str, list[ScriptedAgent]] = {}

    def __call__(self, agent_id: str) -> ScriptedAgent:
        queue = self.scripts.get(agent_id) or [[]]
        script = queue.pop(0) if len(queue) > 1 else queue[0]
        agent = ScriptedAgent(agent_id, script)
        self.made.setdefault(agent_id, []).append(agent)
        return agent
