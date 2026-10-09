"""The Claude and GPT adapters, against fake SDK clients that record every
request. Checks what we send (fairness settings, append-only history,
tool-result shapes) and how responses are translated."""
import copy
import json
from types import SimpleNamespace as NS

from arena.agents.base import ToolCall
from arena.agents.claude import ClaudeAgent
from arena.agents.gpt import GPTAgent
from arena.config import SETTINGS

TOOLS = [{"name": "market_overview", "description": "d", "parameters": {"type": "object", "properties": {}}}]


# ---------------------------------------------------------------- Claude


class FakeAnthropic:
    """client.beta.messages.stream(**kw) -> context manager -> get_final_message()."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        resp = self.responses.pop(0)

        class Ctx:
            def __enter__(s):
                return NS(get_final_message=lambda: resp)

            def __exit__(s, *a):
                return False
        return Ctx()


def claude_msg(content, stop, searches=0):
    return NS(content=content, stop_reason=stop, model="claude-opus-5-5",
              usage=NS(input_tokens=100, cache_read_input_tokens=50, cache_creation_input_tokens=10,
                       output_tokens=20, server_tool_use=NS(web_search_requests=searches)),
              to_dict=lambda: {"stop": stop})


def block(**kw):
    return NS(**kw)


def test_claude_request_settings_and_tool_loop():
    resp1 = claude_msg([block(type="thinking", thinking=""),
                        block(type="tool_use", id="tu_1", name="market_overview", input={})], "tool_use")
    resp2 = claude_msg([block(type="text", text="done")], "end_turn")
    client = FakeAnthropic([resp1, resp2])
    a = ClaudeAgent("claude", "claude-opus-5-5", client=client)
    a.start("SYSTEM", "BRIEFING", TOOLS, web_search=True, max_web_searches=40)

    step = a.step()
    assert [c.name for c in step.tool_calls] == ["market_overview"]
    assert step.usage.input == 100 and step.usage.cached == 50 and step.usage.cache_write == 10
    a.send_tool_results([(step.tool_calls[0], "RESULT", False)])
    a.step()

    req = client.requests[0]
    assert "temperature" not in req and "tool_choice" not in req          # current Opus rejects both
    assert req["output_config"] == {"effort": SETTINGS.reasoning_effort}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert {"type": "web_search_20260209", "name": "web_search", "max_uses": 40} in req["tools"]
    if SETTINGS.claude_refusal_fallback:
        assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    # Tool result goes back as a tool_result block with matching id
    second = client.requests[1]["messages"]
    assert second[-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "RESULT", "is_error": False}]}


def test_claude_history_is_append_only_and_tools_never_change():
    responses = [claude_msg([block(type="tool_use", id=f"t{i}", name="market_overview", input={})], "tool_use")
                 for i in range(3)]
    client = FakeAnthropic(responses)
    a = ClaudeAgent("claude", "claude-opus-5-5", client=client)
    a.start("S", "B", TOOLS, web_search=False, max_web_searches=40)
    for _ in range(3):
        st = a.step()
        a.send_tool_results([(st.tool_calls[0], "r", False)])
    reqs = client.requests
    for prev, nxt in zip(reqs, reqs[1:]):
        # Earlier messages are never edited: each request extends the previous one.
        assert nxt["messages"][:len(prev["messages"])] == prev["messages"]
        assert nxt["tools"] == prev["tools"] and nxt["system"] == prev["system"]
    assert not any(t.get("type", "").startswith("web_search") for t in reqs[0]["tools"])


def test_claude_pause_turn_resumes_without_extra_user_message():
    paused = claude_msg([block(type="server_tool_use", id="s1", name="web_search", input={})], "pause_turn", 1)
    final = claude_msg([block(type="tool_use", id="t1", name="market_overview", input={})], "tool_use", 2)
    client = FakeAnthropic([paused, final])
    a = ClaudeAgent("claude", "claude-opus-5-5", client=client)
    a.start("S", "B", TOOLS, web_search=True, max_web_searches=40)
    step = a.step()
    assert len(client.requests) == 2
    assert client.requests[1]["messages"][-1]["role"] == "assistant"      # resumed as-is
    assert step.web_searches == 3 and step.usage.input == 200
    assert [c.id for c in step.tool_calls] == ["t1"]


def test_claude_refusal_is_reported():
    client = FakeAnthropic([claude_msg([], "refusal")])
    a = ClaudeAgent("claude", "claude-opus-5-5", client=client)
    a.start("S", "B", TOOLS, False, 40)
    assert a.step().stop == "refusal"


# ---------------------------------------------------------------- GPT


class FakeOpenAI:
    """client.responses.create(**kw) -> next canned response."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.requests = []
        self.responses = NS(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return self.queue.pop(0)


def gpt_resp(rid, output, status="completed", text=""):
    return NS(id=rid, output=output, status=status, output_text=text, model="gpt-x",
              usage=NS(input_tokens=1000, output_tokens=300, input_tokens_details=NS(cached_tokens=400)),
              to_dict=lambda: {"id": rid})


def test_gpt_request_settings_and_tool_loop():
    r1 = gpt_resp("resp_1", [NS(type="reasoning"), NS(type="web_search_call"),
                             NS(type="function_call", call_id="c1", name="market_overview", arguments="{}")])
    r2 = gpt_resp("resp_2", [NS(type="message", content=[NS(type="output_text", text="ok")])], text="ok")
    client = FakeOpenAI([r1, r2])
    a = GPTAgent("gpt", "gpt-x", client=client)
    a.start("SYSTEM", "BRIEFING", TOOLS, web_search=True, max_web_searches=40)
    step = a.step()
    assert step.web_searches == 1 and step.usage.cached == 400 and step.usage.input == 600
    a.send_tool_results([(step.tool_calls[0], "RESULT", False)])
    step2 = a.step()
    assert step2.stop == "end" and step2.text == "ok"

    req1, req2 = client.requests
    assert "temperature" not in req1
    assert req1["reasoning"] == {"effort": SETTINGS.reasoning_effort}
    assert req1["instructions"] == "SYSTEM" and req2["instructions"] == "SYSTEM"   # resent each time
    assert {"type": "web_search"} in req1["tools"]
    assert req1["previous_response_id"] is None and req2["previous_response_id"] == "resp_1"
    assert req2["input"] == [{"type": "function_call_output", "call_id": "c1", "output": "RESULT"}]


def test_gpt_malformed_arguments_become_a_parse_error():
    r = gpt_resp("x", [NS(type="function_call", call_id="c1", name="screen", arguments="{not json")])
    a = GPTAgent("gpt", "gpt-x", client=FakeOpenAI([r]))
    a.start("S", "B", TOOLS, False, 40)
    call = a.step().tool_calls[0]
    assert call.args is None and "invalid JSON" in call.parse_error


def test_gpt_errors_are_marked_in_output_text():
    a = GPTAgent("gpt", "gpt-x", client=FakeOpenAI([]))
    a.start("S", "B", TOOLS, False, 40)
    a.send_tool_results([(ToolCall("c1", "x", {}), "bad input", True)])
    assert a.pending[-1]["output"] == "ERROR: bad input"


def test_same_tool_definitions_for_both_providers():
    ca = ClaudeAgent("claude", "m", client=FakeAnthropic([]))
    ga = GPTAgent("gpt", "m", client=FakeOpenAI([]))
    ca.start("S", "B", TOOLS, False, 40)
    ga.start("S", "B", TOOLS, False, 40)
    as_claude = [(t["name"], t["description"], json.dumps(t["input_schema"])) for t in ca.tools]
    as_gpt = [(t["name"], t["description"], json.dumps(t["parameters"])) for t in ga.tools]
    assert as_claude == as_gpt
