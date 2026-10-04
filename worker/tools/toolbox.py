"""The tools the agent can use, plus the execution layer around them.

Execution layer responsibilities (the model never has to think about these):
  * transient failures (5xx, timeouts) are retried with exponential backoff
  * every browser submission / API call is checked against the safety policy
  * approvals are requested from the human and remembered per exact action
  * secrets are injected late and redacted from everything the model or trace sees
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from ..human import HumanChannel, HumanRequest
from ..memory import WorkingMemory
from ..policy import Policy, PolicyBlocked
from ..trace import Trace
from ..vault import Vault
from .browser import Browser, BrowserError, SubmitRequest, TransientError

FILES_DENYLIST = {"vault.json", "lessons.json"}
IDEMPOTENT_TOOLS = {"browser_navigate", "browser_back", "browser_snapshot", "http_get"}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    final: dict | None = None   # set by terminal tools (finish / submit_verdict)


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


S = {"type": "string"}

TOOL_SPECS: dict[str, dict] = {
    "update_plan": {
        "description": "Record or revise your plan: the ordered steps you intend to take toward the user's goal, "
                       "each with a status. Call it at the start and whenever the plan changes (e.g. after a failure).",
        "input_schema": _schema({"steps": {"type": "array", "items": _schema({
            "step": S, "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed", "skipped"]}})}}),
    },
    "browser_navigate": {
        "description": "Open a URL in the browser and return a text snapshot of the page. Interactive elements are "
                       "labelled with refs like [e4]; refs are only valid until the next navigation.",
        "input_schema": _schema({"url": {"type": "string", "description": "Absolute URL, or a path relative to the current page."}}),
    },
    "browser_click": {
        "description": "Click a link or button by ref. Clicking a submit button sends its form; state-changing "
                       "submissions are checked against company policy and may require human approval.",
        "input_schema": _schema({"ref": {"type": "string", "description": "Element ref, e.g. e12"}}),
    },
    "browser_fill": {
        "description": "Set the values of one or more form fields (textboxes, password fields, selects, textareas) on the "
                       "current page. For selects, pass the option value or its visible text. For secrets pass the "
                       "placeholder from get_credentials verbatim, e.g. {{secret:system.password}}.",
        "input_schema": _schema({"fields": {"type": "array", "items": _schema({"ref": S, "value": S})}}),
    },
    "browser_snapshot": {
        "description": "Re-read the current page (including values you have filled in).",
        "input_schema": _schema({}),
    },
    "browser_back": {"description": "Go back to the previous page.", "input_schema": _schema({})},
    "capture_evidence": {
        "description": "Save the current browser page as evidence (HTML + snapshot) and get an evidence ID to cite in "
                       "your final answer. Capture the source of every important value and the final confirmation.",
        "input_schema": _schema({"label": {"type": "string", "description": "Short description, e.g. 'Invoice INV-1 on vendor portal'"}}),
    },
    "http_get": {
        "description": "Make a read-only HTTP GET request to an internal JSON API and return the response body. "
                       "Header values may contain secret placeholders from get_credentials.",
        "input_schema": _schema({"url": S, "headers": {"type": "object", "additionalProperties": {"type": "string"}}},
                                required=["url"]),
    },
    "list_files": {
        "description": "List files in the shared company workspace (reference docs, vendor master data, outputs).",
        "input_schema": _schema({"path": {"type": "string", "description": "Directory relative to the workspace root; '.' for root."}}),
    },
    "read_file": {
        "description": "Read a text file from the company workspace.",
        "input_schema": _schema({"path": S}),
    },
    "write_file": {
        "description": "Write a text file into the workspace 'output/' directory (e.g. a report or export).",
        "input_schema": _schema({"path": {"type": "string", "description": "Path under output/, e.g. output/summary.md"}, "content": S}),
    },
    "get_credentials": {
        "description": "Get login details for a company system. Secret values are returned as placeholders that the "
                       "browser and http tools resolve; you never see the real secret.",
        "input_schema": _schema({"system": {"type": "string", "description": "System key, e.g. ledgerly_erp. An unknown key lists the known ones."}}),
    },
    "remember": {
        "description": "Store a fact you discovered in working memory, with where it came from. Store every value you "
                       "will need later or report to the user (amounts, dates, IDs, decisions).",
        "input_schema": _schema({"key": S, "value": S, "source": {"type": "string", "description": "Where the fact came from (URL, file, evidence ID, user)."}}),
    },
    "recall": {"description": "Show everything in working memory.", "input_schema": _schema({})},
    "ask_user": {
        "description": "Ask the user a question and wait for the answer. Use it when the request is ambiguous in a way "
                       "that changes the outcome, when information is missing and cannot be found, or when you are "
                       "unsure an action is safe or wanted. Do not use it for things you can find out yourself.",
        "input_schema": _schema({
            "question": S,
            "options": {"type": "array", "items": S, "description": "Suggested answers, if any."}},
            required=["question"]),
    },
    "finish": {
        "description": "End the task. Report honestly: status 'completed' only if the user's goal was achieved and you "
                       "observed proof of it; 'blocked' if you need something from the user; 'failed' otherwise. "
                       "The harness will independently verify a 'completed' claim against the live systems.",
        "input_schema": _schema({
            "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "summary": {"type": "string", "description": "2-5 sentences for the user: what was done and the outcome."},
            "results": {"type": "object", "description": "Key outputs as flat key/value pairs (e.g. invoice_number, amount, record_id).",
                        "additionalProperties": {"type": "string"}},
            "evidence": {"type": "array", "items": S, "description": "Evidence IDs (E1, E2...) proving the results."},
            "verification": {"type": "string", "description": "How you confirmed the outcome (what you re-read after acting)."},
            "lessons_learned": {"type": "array", "items": S,
                                "description": "Optional short, reusable lessons about these systems for future runs."}},
            required=["status", "summary", "results", "evidence", "verification"]),
    },
    "submit_verdict": {
        "description": "Verifier only: report whether the worker's claimed outcome is true in the live systems.",
        "input_schema": _schema({
            "passed": {"type": "boolean"},
            "checks": {"type": "array", "items": _schema({"claim": S, "observed": S, "ok": {"type": "boolean"}})},
            "summary": S}),
    },
}

WORKER_TOOLS = ["update_plan", "browser_navigate", "browser_click", "browser_fill", "browser_snapshot", "browser_back",
                "capture_evidence", "http_get", "list_files", "read_file", "write_file", "get_credentials",
                "remember", "recall", "ask_user", "finish"]
VERIFIER_TOOLS = ["browser_navigate", "browser_click", "browser_fill", "browser_snapshot", "http_get",
                  "list_files", "read_file", "get_credentials", "submit_verdict"]


def tool_definitions(names: list[str]) -> list[dict]:
    return [{"name": n, **TOOL_SPECS[n]} for n in names]


@dataclass
class ToolBox:
    names: list[str]
    browser_client: httpx.Client
    workspace: Path
    vault: Vault
    policy: Policy
    human: HumanChannel
    trace: Trace
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    max_retries: int = 3
    backoff: float = 0.5
    sleep: Callable[[float], None] = time.sleep
    step: int = 0
    plan: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.approved: set[str] = set()
        self.evidence_ids: set[str] = set()
        self.browser = Browser(self.browser_client, allowed_hosts=self.policy.allowed_hosts,
                               secret_resolver=self.vault.resolve, on_submit=self._guard_submit)

    @property
    def definitions(self) -> list[dict]:
        return tool_definitions(self.names)

    # -- dispatch ---------------------------------------------------------------

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name not in self.names:
            return ToolResult(f"Unknown tool {name!r}. Available: {self.names}", True)
        handler = getattr(self, f"_t_{name}")
        attempt = 0
        while True:
            attempt += 1
            try:
                return handler(**args)
            except TransientError as e:
                if name not in IDEMPOTENT_TOOLS:
                    # Never blindly replay a submission: it may have gone through before the error.
                    return ToolResult(f"Transient failure: {e}\nThe action may or may not have taken effect. "
                                      "Check the target system's current state before trying it again.", True)
                if attempt > self.max_retries:
                    return ToolResult(f"Transient failure persisted after {self.max_retries} retries: {e}\n"
                                      "Consider an alternative route to the same information, or ask the user.", True)
                delay = self.backoff * 2 ** (attempt - 1)
                self.trace.emit("retry", tool=name, attempt=attempt, delay_s=delay, error=str(e))
                self.sleep(delay)
            except PolicyBlocked as e:
                self.trace.emit("policy", tool=name, decision="blocked", reason=str(e))
                return ToolResult(f"BLOCKED: {e}", True)
            except (BrowserError, KeyError, ValueError, FileNotFoundError, PermissionError) as e:
                return ToolResult(f"Error: {e}", True)
            except TypeError as e:  # malformed tool arguments
                return ToolResult(f"Invalid arguments for {name}: {e}", True)

    # -- safety gate -------------------------------------------------------------

    def _guard_submit(self, req: SubmitRequest) -> None:
        a = self.policy.assess_submit(req)
        self.trace.emit("policy", tool="browser_submit", decision="needs_approval" if a.needs_approval else "allowed",
                        risk=a.risk, reason=a.reason, url=req.url, fields=req.display_fields)
        if not a.needs_approval:
            return
        fingerprint = json.dumps([req.method, req.url, req.display_fields], sort_keys=True)
        if fingerprint in self.approved:
            self.trace.emit("approval", approved=True, reused=True, reason="Identical action already approved.")
            return
        hr = HumanRequest("approval", f"The agent wants to '{req.button_label}' on {a.system or req.url}.\n"
                                      f"Reason approval is needed: {a.reason}",
                          ["yes", "no"], {"action": f"{req.method} {req.url}", **req.display_fields})
        self.trace.emit("human_request", kind="approval", question=hr.question, details=hr.details)
        approved, comment = self.human.approve(hr)
        self.trace.emit("approval", approved=approved, comment=comment)
        if not approved:
            raise PolicyBlocked(f"The user declined this action. Their response: {comment!r}. Do not retry it; "
                                "adjust according to their response or finish with status 'blocked'.")
        self.approved.add(fingerprint)

    # -- tool implementations ------------------------------------------------------

    def _t_update_plan(self, steps: list[dict]) -> ToolResult:
        self.plan = steps
        self.trace.emit("plan", steps=steps)
        return ToolResult("Plan recorded.")

    def _t_browser_navigate(self, url: str) -> ToolResult:
        return ToolResult(self.browser.navigate(url))

    def _t_browser_click(self, ref: str) -> ToolResult:
        return ToolResult(self.browser.click(ref))

    def _t_browser_fill(self, fields: list[dict]) -> ToolResult:
        out = [self.browser.fill(f["ref"], f["value"]) for f in fields]
        return ToolResult("\n".join(out))

    def _t_browser_snapshot(self) -> ToolResult:
        if not self.browser.url:
            return ToolResult("No page is open yet. Use browser_navigate first.", True)
        return ToolResult(self.browser.snapshot())

    def _t_browser_back(self) -> ToolResult:
        return ToolResult(self.browser.back())

    def _t_capture_evidence(self, label: str) -> ToolResult:
        if not self.browser.url:
            return ToolResult("No page is open to capture.", True)
        eid = self.trace.save_evidence(label, self.browser.url, self.browser.snapshot(), self.browser.html)
        self.evidence_ids.add(eid)
        return ToolResult(f"Saved evidence {eid}: {label} ({self.browser.url})")

    def _t_http_get(self, url: str, headers: dict[str, str] | None = None) -> ToolResult:
        if self.browser.url:
            from urllib.parse import urljoin
            url = urljoin(self.browser.url, url)
        self.browser._check_host(url)
        resolved = {k: self.vault.resolve(v)[0] for k, v in (headers or {}).items()}
        try:
            resp = self.browser_client.get(url, headers=resolved)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransientError(str(e)) from e
        if resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code} from {url}")
        body = resp.text[:6000]
        return ToolResult(f"HTTP {resp.status_code}\n{body}", resp.status_code >= 400)

    def _safe_path(self, path: str) -> Path:
        p = (self.workspace / path).resolve()
        if self.workspace.resolve() not in (p, *p.parents):
            raise PermissionError(f"{path!r} is outside the workspace.")
        if p.name in FILES_DENYLIST:
            raise PermissionError(f"{p.name} is protected and cannot be read directly.")
        return p

    def _t_list_files(self, path: str = ".") -> ToolResult:
        p = self._safe_path(path)
        items = sorted(x.relative_to(self.workspace).as_posix() + ("/" if x.is_dir() else "")
                       for x in p.iterdir() if x.name not in FILES_DENYLIST)
        return ToolResult("\n".join(items) or "(empty)")

    def _t_read_file(self, path: str) -> ToolResult:
        text = self._safe_path(path).read_text(encoding="utf-8")
        return ToolResult(text[:8000])

    def _t_write_file(self, path: str, content: str) -> ToolResult:
        p = self._safe_path(path)
        if (self.workspace / "output").resolve() not in p.parents:
            raise PermissionError("Files can only be written under output/.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.trace.emit("file_written", path=path, chars=len(content))
        return ToolResult(f"Wrote {len(content)} characters to {path}.")

    def _t_get_credentials(self, system: str) -> ToolResult:
        return ToolResult(json.dumps(self.vault.describe(system), indent=2))

    def _t_remember(self, key: str, value: str, source: str) -> ToolResult:
        self.memory.remember(key, value, source, self.step)
        self.trace.emit("memory", key=key, value=value, source=source)
        return ToolResult(f"Remembered. Working memory now:\n{self.memory.render()}")

    def _t_recall(self) -> ToolResult:
        return ToolResult(self.memory.render())

    def _t_ask_user(self, question: str, options: list[str] | None = None) -> ToolResult:
        hr = HumanRequest("clarification", question, options or [], {})
        self.trace.emit("human_request", kind="clarification", question=question, options=options or [])
        answer = self.human.ask(hr)
        self.trace.emit("human_response", answer=answer)
        self.memory.remember(f"user_answer_{len(self.memory.facts) + 1}", answer, f"user, asked: {question}", self.step)
        return ToolResult(f"User answered: {answer}")

    def _t_finish(self, **report) -> ToolResult:
        unknown = [e for e in report.get("evidence", []) if e not in self.evidence_ids]
        if report.get("status") == "completed" and unknown:
            return ToolResult(f"Evidence IDs {unknown} do not exist. Captured evidence: {sorted(self.evidence_ids)}", True)
        return ToolResult("Final report received.", final=report)

    def _t_submit_verdict(self, **verdict) -> ToolResult:
        return ToolResult("Verdict received.", final=verdict)
