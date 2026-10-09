"""Claude adapter (Anthropic Messages API).

Notes on the API that shape this code:
  * The conversation is append-only. We never edit earlier messages because
    Claude's thinking blocks are tied to the exact history they were made
    in; editing it would invalidate them (and the prompt cache).
  * tool_choice stays "auto" (current Opus rejects forced tool use); the
    system prompt tells the model to finish with submit_decision, and the
    shared loop nudges it if it doesn't.
  * Web search is Anthropic's server-side tool: it runs inside the request.
    If the server-side loop pauses ("pause_turn") we just send the
    conversation again and it resumes.
"""
from __future__ import annotations

from ..config import SETTINGS, require_env
from .base import AgentAdapter, Step, ToolCall, Usage

MAX_PAUSE_RESUMES = 5


class ClaudeAgent(AgentAdapter):
    def __init__(self, agent_id: str, model: str, client=None):
        super().__init__(agent_id, model)
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"), max_retries=4)
        self.client = client
        self.messages: list[dict] = []

    def price_key(self) -> str:
        return self.model

    def start(self, system, briefing, tools, web_search, max_web_searches):
        self.system = [{"type": "text", "text": system}]
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                      for t in tools]
        if web_search:
            # max_uses is per request and the tool list must stay identical
            # all turn (prompt cache + thinking blocks), so it's set once to
            # the whole budget; the shared loop enforces the real budget.
            self.tools.append({"type": "web_search_20260209", "name": "web_search",
                               "max_uses": max_web_searches})
        self.messages = [{"role": "user", "content": briefing}]

    def _request(self):
        kwargs = dict(
            model=self.model,
            max_tokens=SETTINGS.max_output_tokens,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            output_config={"effort": SETTINGS.reasoning_effort},
            cache_control={"type": "ephemeral"},   # cache the growing conversation
        )
        if SETTINGS.claude_refusal_fallback:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        # Streaming avoids HTTP timeouts on long thinking-heavy responses.
        with self.client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def step(self) -> Step:
        usage, searches, raws = Usage(), 0, []
        for _ in range(MAX_PAUSE_RESUMES + 1):
            resp = self._request()
            u = resp.usage
            usage.add(Usage(u.input_tokens or 0, getattr(u, "cache_read_input_tokens", 0) or 0,
                            getattr(u, "cache_creation_input_tokens", 0) or 0, u.output_tokens or 0))
            stu = getattr(u, "server_tool_use", None)
            searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
            # Append the full content (thinking, tool_use, search results,
            # fallback blocks...) exactly as returned.
            self.messages.append({"role": "assistant", "content": resp.content})
            raws.append(_dump(resp))
            if resp.stop_reason != "pause_turn":
                break

        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else None,
                          None if isinstance(b.input, dict) else "arguments were not a JSON object")
                 for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        stop = {"refusal": "refusal", "max_tokens": "max_tokens",
                "tool_use": "tool_use"}.get(resp.stop_reason, "end")
        return Step(calls, text, usage, searches, stop, getattr(resp, "model", None), raws)

    def send_tool_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error}
            for call, text, is_error in results]})

    def send_user_text(self, text):
        self.messages.append({"role": "user", "content": text})


def _dump(resp) -> dict:
    try:
        return resp.to_dict()
    except Exception:  # noqa: BLE001
        return {"repr": repr(resp)[:2000]}
