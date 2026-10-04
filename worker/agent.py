"""The agent loop: observe -> decide -> act -> observe, until the goal is verified or blocked.

    TaskWorker.run(task)
      ├─ brain.step()            decide the next action(s) from everything observed so far
      ├─ toolbox.execute()       act (with retries, policy gate, approvals, secret handling)
      ├─ loop guards             step budget, repeated-failure detection, nudges
      └─ on finish(completed) -> Verifier: a separate, read-only agent re-checks the live
                                 systems; a failed verdict is sent back to the worker once
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import httpx

from .brain import BrainError
from .human import HumanChannel, ScriptedHuman
from .memory import LessonStore
from .policy import Policy
from .tools.toolbox import VERIFIER_TOOLS, WORKER_TOOLS, ToolBox, ToolResult
from .trace import Trace
from .vault import Vault

WORKER_SYSTEM = """\
You are an autonomous operations worker at OurCo. A colleague gives you a goal in plain language and you \
achieve it by operating the company's systems through your tools, the way a careful, experienced employee would.

How you work:
- Work out the end goal and what "done" means before acting. Find out where information lives (the workspace \
reference files describe the company's systems and procedures) instead of guessing URLs or values.
- Keep a short plan with update_plan and revise it when something unexpected happens.
- After every action, read the observation and decide the next step from what actually happened. Never assume an \
action worked: confirm it on the resulting page.
- Use remember for every fact you will need later or will report, with its source. Use capture_evidence on the \
pages that prove the source values and the final outcome.
- When something fails, read the error and fix the cause (wrong format, expired session, wrong element, missing \
field) or take a reasonable alternative route. Do not repeat a failing action unchanged. Page loads that fail \
transiently are retried for you automatically.
- Use ask_user when the request is ambiguous in a way that changes the outcome, when something you need cannot be \
found, or when an action might be unsafe or unwanted. Do not ask about things you can find out yourself.
- Company policy is enforced by the harness: some actions pause for human approval, and some are forbidden. If the \
user declines an action, respect that and do not try to work around it.
- Never fabricate data. Every value you enter or report must come from something you observed or from the user.
- Before finishing as "completed", re-read the target system to confirm the outcome (open the saved record), and \
capture it as evidence. An independent auditor will check your claims against the live systems.
- End by calling finish with a short, concrete summary for the user.
"""

VERIFIER_SYSTEM = """\
You are an independent auditor. Another agent claims to have completed a task. Your job is to check, in the live \
company systems, whether the user's goal was actually achieved and whether the reported values are correct. \
Do not trust the claims: re-observe the systems yourself (you are read-only and cannot change anything). Check \
both that the target record exists with the right values, and that those values match the original source. \
Finish by calling submit_verdict with one check per important claim.
"""


@dataclass
class Environment:
    sim_url: str
    workspace: Path
    runs_dir: Path
    client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=15)
    approval_threshold: float = 10_000.0
    approval_mode: str = "threshold"
    retry_backoff: float = 0.5

    def policy(self, read_only: bool = False) -> Policy:
        host = httpx.URL(self.sim_url).netloc.decode()
        return Policy(allowed_hosts={host}, approval_threshold=self.approval_threshold,
                      approval_mode=self.approval_mode, read_only=read_only)

    def vault(self) -> Vault:
        return Vault(self.workspace / "vault.json")


@dataclass
class RunResult:
    run_id: str
    status: str                       # completed | blocked | failed
    verified: bool | None
    summary: str
    results: dict
    evidence: list[str]
    verification: dict | None
    memory: dict
    steps: int
    run_dir: Path
    usage: dict = field(default_factory=dict)


class AgentLoop:
    """Shared tool-use loop used by both the worker and the verifier."""

    def __init__(self, brain, toolbox: ToolBox, system: str, trace, max_steps: int):
        self.brain, self.toolbox, self.system, self.trace, self.max_steps = brain, toolbox, system, trace, max_steps
        self.messages: list[dict] = []
        self.failures: Counter = Counter()
        self.usage: Counter = Counter()
        self.steps = 0

    def run(self, first_message: str,
            on_final: Callable[[dict], ToolResult | None] = lambda r: None) -> dict | None:
        """Runs until a terminal tool returns a final report accepted by `on_final`.

        on_final may return a ToolResult to reject the report (e.g. failed verification); the
        rejection is sent back to the model and the loop continues.
        """
        self.messages.append({"role": "user", "content": first_message})
        nudges = 0
        while self.steps < self.max_steps:
            self.steps += 1
            self.toolbox.step = self.steps
            turn = self.brain.step(self.system, self.messages, self.toolbox.definitions)
            self.usage.update(turn.usage)
            self.trace.emit("thought", step=self.steps, text=turn.text, thinking=turn.thinking[:2000],
                            usage=turn.usage)
            self.messages.append({"role": "assistant", "content": turn.content})

            if not turn.tool_calls:
                nudges += 1
                if nudges > 2:
                    return None
                hint = ("Your last response was cut off (max_tokens); continue with smaller steps."
                        if turn.stop_reason == "max_tokens" else
                        "Keep working with your tools. When you are finished, report via the terminal tool.")
                self.messages.append({"role": "user", "content": hint})
                continue

            results, final = [], None
            for call in turn.tool_calls:
                self.trace.emit("action", step=self.steps, tool=call.name, input=call.input)
                if final is not None:
                    res = ToolResult("Skipped: the task was already finished in this turn.", True)
                else:
                    res = self.toolbox.execute(call.name, call.input)
                    if res.final is not None:
                        rejection = on_final(res.final)
                        if rejection is None:
                            final = res.final
                        else:
                            res = rejection
                    res = self._loop_guard(call.name, call.input, res)
                self.trace.emit("observation", step=self.steps, tool=call.name, is_error=res.is_error,
                                content=res.content[:4000])
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": res.content,
                                **({"is_error": True} if res.is_error else {})})
            # All results for one assistant turn go back in a single user message.
            self.messages.append({"role": "user", "content": results})
            if final is not None:
                return final
        return None

    def _loop_guard(self, name: str, args: dict, res: ToolResult) -> ToolResult:
        if not res.is_error:
            return res
        key = (name, json.dumps(args, sort_keys=True))
        self.failures[key] += 1
        if self.failures[key] >= 3:
            self.trace.emit("loop_guard", tool=name, repeats=self.failures[key])
            res.content += (f"\n\nNOTE: this exact action has now failed {self.failures[key]} times. Stop repeating "
                            "it. Change your approach, or ask the user / finish as blocked.")
        return res


class TaskWorker:
    def __init__(self, env: Environment, brain, human: HumanChannel, verifier_brain=None,
                 max_steps: int = 45, verify: bool = True, trace: Trace | None = None,
                 requester: str = "a colleague in Accounts Payable"):
        self.env, self.brain, self.human = env, brain, human
        self.verifier_brain = verifier_brain
        self.verify, self.max_steps, self.requester = verify, max_steps, requester
        vault = env.vault()
        self.trace = trace or Trace(env.runs_dir, secrets=vault.secret_values())
        self.lessons = LessonStore(env.workspace / "memory" / "lessons.json")

    def _toolbox(self, names: list[str], actor: str, read_only: bool, human: HumanChannel) -> ToolBox:
        return ToolBox(names=names, browser_client=self.env.client_factory(), workspace=self.env.workspace,
                       vault=self.env.vault(), policy=self.env.policy(read_only=read_only), human=human,
                       trace=self.trace.for_actor(actor), backoff=self.env.retry_backoff)

    def run(self, task: str) -> RunResult:
        t = self.trace
        t.emit("run_started", actor="worker", task=task, sim_url=self.env.sim_url,
               brain=type(self.brain).__name__, lessons=self.lessons.lessons)
        toolbox = self._toolbox(WORKER_TOOLS, "worker", read_only=False, human=self.human)
        loop = AgentLoop(self.brain, toolbox, WORKER_SYSTEM, t.for_actor("worker"), self.max_steps)
        verdicts: list[dict] = []

        def on_final(report: dict) -> ToolResult | None:
            if report.get("status") != "completed" or not self.verify:
                return None
            verdict = self._verify(task, report)
            verdicts.append(verdict)
            if verdict.get("passed") or len(verdicts) >= 2:
                return None
            failed = [c for c in verdict.get("checks", []) if not c.get("ok")]
            return ToolResult("INDEPENDENT VERIFICATION FAILED - the task is not done yet.\n"
                              f"Auditor summary: {verdict.get('summary')}\nFailed checks: {json.dumps(failed, indent=2)}\n"
                              "Investigate, correct the problem in the system, then call finish again.", True)

        lessons = "\n".join(f"- {x}" for x in self.lessons.lessons) or "- (none yet)"
        first = (f"Today is {date.today().isoformat()}. Request from {self.requester}:\n\n{task}\n\n"
                 f"Company systems are web apps under {self.env.sim_url}. The workspace (list_files) holds reference "
                 f"docs about the systems and procedures.\n\nLessons recorded by earlier runs:\n{lessons}")
        try:
            report = loop.run(first, on_final)
        except BrainError as e:
            t.emit("error", actor="worker", error=str(e))
            report = {"status": "failed", "summary": f"The worker stopped: {e}", "results": {}, "evidence": [],
                      "verification": ""}
        if report is None:
            report = {"status": "failed", "results": {}, "evidence": [], "verification": "",
                      "summary": f"Stopped after {loop.steps} steps without reaching a verified result."}

        verdict = verdicts[-1] if verdicts else None
        verified = verdict.get("passed") if verdict else None
        if report.get("status") == "completed" and verdict is not None and not verified:
            report["status"] = "failed"
            report["summary"] = "The worker reported success, but independent verification failed. " + report["summary"]
        added = self.lessons.add(report.get("lessons_learned", []))
        result = RunResult(t.run_id, report["status"], verified, report.get("summary", ""), report.get("results", {}),
                           report.get("evidence", []), verdict, toolbox.memory.as_dict(), loop.steps, t.dir,
                           dict(loop.usage))
        t.emit("run_finished", actor="worker", status=result.status, verified=verified, summary=result.summary,
               results=result.results, evidence=result.evidence, verification_note=report.get("verification", ""),
               lessons_added=added, steps=loop.steps, usage=dict(loop.usage))
        (t.dir / "result.json").write_text(json.dumps({**result.__dict__, "run_dir": str(t.dir)}, indent=2, default=str), encoding="utf-8")
        from .report import write_report
        write_report(t, task, result)
        return result

    def _verify(self, task: str, report: dict) -> dict:
        vt = self.trace.for_actor("verifier")
        vt.emit("verification_started", claims=report.get("results", {}))
        if self.verifier_brain is None:
            return {"passed": True, "checks": [], "summary": "No verifier configured."}
        toolbox = self._toolbox(VERIFIER_TOOLS, "verifier", read_only=True, human=ScriptedHuman(approve=False))
        loop = AgentLoop(self.verifier_brain, toolbox, VERIFIER_SYSTEM, vt, max_steps=20)
        msg = (f"Original user request:\n{task}\n\nThe worker's final report:\n{json.dumps(report, indent=2)}\n\n"
               f"Systems are under {self.env.sim_url}; reference docs are in the workspace.")
        try:
            verdict = loop.run(msg) or {"passed": False, "checks": [], "summary": "Verifier did not reach a verdict."}
        except BrainError as e:
            verdict = {"passed": False, "checks": [], "summary": f"Verifier error: {e}"}
        vt.emit("verification_result", **verdict)
        return verdict
