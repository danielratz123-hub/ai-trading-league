"""GPT adapter (OpenAI Responses API).

The Responses API keeps the conversation on OpenAI's side: each request
sends only the new input items plus `previous_response_id`. System
instructions are not carried over between responses, so they're resent
every time. Web search is OpenAI's built-in `web_search` tool.
"""
from __future__ import annotations

import json

from ..config import SETTINGS, require_env
from .base import AgentAdapter, Step, ToolCall, Usage


class GPTAgent(AgentAdapter):
    def __init__(self, agent_id: str, model: str, client=None):
        super().__init__(agent_id, model)
        if client is None:
            import openai
            client = openai.OpenAI(api_key=require_env("OPENAI_API_KEY"), max_retries=4, timeout=900)
        self.client = client

    def price_key(self) -> str:
        return "gpt"

    def start(self, system, briefing, tools, web_search, max_web_searches):
        self.instructions = system
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["parameters"], "strict": False} for t in tools]
        if web_search:
            self.tools.append({"type": "web_search"})
        self.pending: list[dict] = [{"role": "user", "content": briefing}]
        self.previous_id: str | None = None

    def step(self) -> Step:
        resp = self.client.responses.create(
            model=self.model,
            instructions=self.instructions,
            input=self.pending,
            previous_response_id=self.previous_id,
            tools=self.tools,
            reasoning={"effort": SETTINGS.reasoning_effort},
            max_output_tokens=SETTINGS.max_output_tokens,
            prompt_cache_key=f"arena-{self.agent_id}",
            store=True,   # needed for previous_response_id
        )
        self.previous_id = resp.id
        self.pending = []

        calls, searches, refused = [], 0, False
        for item in resp.output:
            if item.type == "function_call":
                try:
                    args = json.loads(item.arguments or "{}")
                    err = None if isinstance(args, dict) else "arguments were not a JSON object"
                except json.JSONDecodeError as e:
                    args, err = None, f"invalid JSON ({e})"
                calls.append(ToolCall(item.call_id, item.name, args if err is None else None, err))
            elif item.type == "web_search_call":
                searches += 1
            elif item.type == "message":
                refused |= any(getattr(c, "type", "") == "refusal" for c in item.content)

        u = resp.usage
        cached = (u.input_tokens_details.cached_tokens or 0) if u and u.input_tokens_details else 0
        usage = Usage(input=(u.input_tokens or 0) - cached, cached=cached, output=u.output_tokens or 0) if u else Usage()
        if refused:
            stop = "refusal"
        elif resp.status == "incomplete":
            stop = "max_tokens"
        else:
            stop = "tool_use" if calls else "end"
        return Step(calls, resp.output_text or "", usage, searches, stop, resp.model, _dump(resp))

    def send_tool_results(self, results):
        # The Responses API has no is_error flag; errors are marked in the text.
        self.pending += [{"type": "function_call_output", "call_id": call.id,
                          "output": text if not is_error or text.startswith("ERROR") else f"ERROR: {text}"}
                         for call, text, is_error in results]

    def send_user_text(self, text):
        self.pending.append({"role": "user", "content": text})


def _dump(resp) -> dict:
    try:
        return resp.to_dict()
    except Exception:  # noqa: BLE001
        return {"repr": repr(resp)[:2000]}
