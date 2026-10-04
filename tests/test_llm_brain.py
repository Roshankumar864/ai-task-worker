"""LLMBrain against a mock Messages API: request shape, response parsing, history round-trip."""
import json

import anthropic
import httpx2

from worker.brain import LLMBrain

SEEN: list[dict] = []


def _response(content, stop="tool_use"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80}}


def handler(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    SEEN.append({"headers": dict(request.headers), "body": body})
    if len(SEEN) == 1:
        return httpx2.Response(200, json=_response([
            {"type": "thinking", "thinking": "Check the docs first.", "signature": "sig123"},
            {"type": "text", "text": "Reading the systems directory."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "systems.md"}}]))
    return httpx2.Response(200, json=_response([{"type": "text", "text": "done"}], "end_turn"))


def test_request_shape_and_history_roundtrip():
    client = anthropic.Anthropic(api_key="test", base_url="http://mock.api",
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    brain = LLMBrain(client=client)
    tools = [{"name": "read_file", "description": "Read", "input_schema": {"type": "object", "properties": {}}}]
    messages = [{"role": "user", "content": "do the task"}]

    turn = brain.step("system prompt", messages, tools)
    assert [c.name for c in turn.tool_calls] == ["read_file"] and turn.tool_calls[0].input == {"path": "systems.md"}
    assert turn.thinking == "Check the docs first." and turn.usage["cache_read"] == 80

    req = SEEN[0]
    assert req["body"]["model"] == "claude-opus-5-5"
    assert req["body"]["thinking"]["type"] == "adaptive"
    assert req["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"]["anthropic-beta"]
    assert req["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}

    # The assistant turn (with its signed thinking block) goes back unchanged.
    messages.append({"role": "assistant", "content": turn.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]})
    turn2 = brain.step("system prompt", messages, tools)
    assert turn2.stop_reason == "end_turn" and not turn2.tool_calls
    replayed = SEEN[1]["body"]["messages"][1]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "Check the docs first.", "signature": "sig123"}
    assert replayed[2]["type"] == "tool_use" and replayed[2]["id"] == "toolu_1"
