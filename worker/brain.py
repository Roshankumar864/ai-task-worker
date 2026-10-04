"""The "brain" decides the next action. Two implementations share one interface:

LLMBrain      - a large language model via the Anthropic API (the real worker)
ScriptedBrain - a deterministic policy that replays a known-good strategy. It exists so the
                harness (retries, approvals, verification, evidence) can be tested and demoed
                without an API key. It is not "the AI"; it is a test double for it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Generator

import anthropic

MODEL = os.environ.get("WORKER_MODEL", "claude-opus-5-5")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class BrainTurn:
    content: list[Any]                 # assistant content to append to history, unchanged
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "tool_use"
    usage: dict[str, int] = field(default_factory=dict)


class BrainError(Exception):
    pass


class LLMBrain:
    def __init__(self, model: str = MODEL, effort: str = "high", max_tokens: int = 16000,
                 client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.model, self.effort, self.max_tokens = model, effort, max_tokens

    def step(self, system: str, messages: list, tools: list[dict]) -> BrainTurn:
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Tools + system are stable across turns -> cached; top-level cache_control
                # additionally caches the growing conversation prefix on each turn.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                cache_control={"type": "ephemeral"},
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise BrainError("Anthropic authentication failed - set ANTHROPIC_API_KEY or run `ant auth login`.") from e
        except anthropic.BadRequestError as e:
            raise BrainError(f"Bad request to the LLM API: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise BrainError("Rate limited by the LLM API after retries.") from e
        except anthropic.APIStatusError as e:
            raise BrainError(f"LLM API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise BrainError("Could not reach the LLM API.") from e

        turn = BrainTurn(content=resp.content, stop_reason=resp.stop_reason or "",
                         usage={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens,
                                "cache_read": resp.usage.cache_read_input_tokens or 0})
        for b in resp.content:
            if b.type == "text":
                turn.text += b.text
            elif b.type == "thinking":
                turn.thinking += b.thinking or ""
            elif b.type == "tool_use":
                turn.tool_calls.append(ToolCall(b.id, b.name, dict(b.input)))
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise BrainError(f"The model declined this task ({getattr(details, 'category', None) or 'policy'}).")
        return turn


# --- Scripted test double --------------------------------------------------------------

Script = Callable[[str], Generator[tuple[str, str, dict], str, None]]


class ScriptedBrain:
    """Drives a generator script: it yields (thought, tool_name, input) and is sent the tool result."""

    def __init__(self, script: Script):
        self.script = script
        self.gen = None
        self.n = 0

    def step(self, system: str, messages: list, tools: list[dict]) -> BrainTurn:
        last = messages[-1]["content"]
        if self.gen is None or len(messages) == 1:   # a new conversation restarts the script
            task = last if isinstance(last, str) else last[0]["text"]
            self.gen = self.script(task)
            thought, name, args = next(self.gen)
        else:
            observation = "\n".join(_block_text(b) for b in last) if isinstance(last, list) else last
            try:
                thought, name, args = self.gen.send(observation)
            except StopIteration:
                return BrainTurn(content=[{"type": "text", "text": "Done."}], text="Done.", stop_reason="end_turn")
        self.n += 1
        call_id = f"toolu_scripted_{self.n:03d}"
        content = [{"type": "text", "text": thought},
                   {"type": "tool_use", "id": call_id, "name": name, "input": args}]
        return BrainTurn(content=content, text=thought, tool_calls=[ToolCall(call_id, name, args)])


def _block_text(b: Any) -> str:
    if isinstance(b, dict):
        c = b.get("content", b.get("text", ""))
        return c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c)
    return str(b)
