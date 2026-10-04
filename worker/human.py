"""Channels for reaching the human: clarifying questions and action approvals."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass
class HumanRequest:
    kind: str                 # clarification | approval
    question: str
    options: list[str]
    details: dict


class HumanChannel:
    def ask(self, req: HumanRequest) -> str:
        raise NotImplementedError

    def approve(self, req: HumanRequest) -> tuple[bool, str]:
        answer = self.ask(req).strip()
        approved = answer.lower().split(" ")[0] in {"y", "yes", "approve", "approved", "ok"}
        return approved, answer


class ConsoleHuman(HumanChannel):
    def ask(self, req: HumanRequest) -> str:
        print("\n" + "=" * 70)
        print(f"  AGENT NEEDS YOUR {'APPROVAL' if req.kind == 'approval' else 'INPUT'}")
        print("=" * 70)
        print(req.question)
        for k, v in req.details.items():
            print(f"    {k}: {v}")
        if req.options:
            print("Options: " + " / ".join(req.options))
        prompt = "Approve? [yes/no + optional comment] > " if req.kind == "approval" else "> "
        return input(prompt)


class ScriptedHuman(HumanChannel):
    """Answers from a fixed policy. Used in tests and in `--auto-approve` demo runs."""

    def __init__(self, approve: bool = True, answers: Callable[[HumanRequest], str] | list[str] | None = None):
        self.approve_all = approve
        self.answers = answers
        self.requests: list[HumanRequest] = []

    def ask(self, req: HumanRequest) -> str:
        self.requests.append(req)
        if req.kind == "approval":
            return "yes (auto-approved)" if self.approve_all else "no - declined by test policy"
        if callable(self.answers):
            return self.answers(req)
        if self.answers:
            return self.answers.pop(0)
        return req.options[0] if req.options else "Use your best judgement."


class QueueHuman(HumanChannel):
    """Blocks the agent thread until the web console posts an answer."""

    def __init__(self, notify: Callable[[HumanRequest], None], timeout: float = 900):
        self.notify = notify
        self.timeout = timeout
        self._event = threading.Event()
        self._answer = ""
        self.pending: HumanRequest | None = None

    def ask(self, req: HumanRequest) -> str:
        self._event.clear()
        self.pending = req
        self.notify(req)
        if not self._event.wait(self.timeout):
            self.pending = None
            return "no - no response from the user before the timeout"
        self.pending = None
        return self._answer

    def answer(self, text: str) -> None:
        self._answer = text
        self._event.set()
