# Autonomous AI Task Worker

**Live demo:** https://roshankumar864.github.io/ai-task-worker/ · **Code:** https://github.com/Roshankumar864/ai-task-worker

A prototype AI worker that takes a plain-language request such as

> *"Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our internal system, and tell me once it is done."*

and carries it out on its own. It works out where the invoice lives, signs in to the vendor portal, picks the newest invoice, extracts the values, records them in the internal ERP, gets human sign-off where policy requires it, recovers from failures along the way, and confirms the result. A **separate, read-only auditor agent then re-checks the live systems** before the run counts as done. Every run produces an evidence pack and an HTML report.

The agent is driven by a large language model (Anthropic API with tool use and adaptive thinking; model set by `WORKER_MODEL`). It operates **SimWorld**, a small simulated company with an e-mail inbox, an external vendor portal and an internal ERP. SimWorld has deliberate traps built in: outages, expired sessions, strict form validation, ambiguous vendor names, duplicate records and a "release payment" button the agent must never press.

---

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Put your Anthropic API key in a local .env file (git-ignored, never uploaded)
copy .env.example .env                                # macOS/Linux: cp .env.example .env, then edit it

# 1) Web console: type a task, watch the worker live, approve or answer it in the browser
python -m worker console                             # http://127.0.0.1:8000

# 2) Command line
python -m worker run --reset "Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our internal system, and tell me once it is done."

# 3) No API key? Offline demo (a scripted test double replaces the LLM; everything else is real)
python -m worker run --offline --reset

# Tests (14, ~3 s, no network or API key needed)
python -m pytest -q
```

SimWorld starts automatically on `http://127.0.0.1:8100`. Open `/mail/`, `/acme/` and `/erp/payables` to watch records change. The demo logins for the simulated company are generated randomly on first run and stored in `workspace/vault.json`, which is git-ignored, so no credentials live in the repository. Each run writes `runs/<run-id>/` containing `trace.jsonl`, `result.json`, `report.html` and `evidence/`.

Try these tasks in the console (they are also the preset chips):

| Task | What it exercises |
|---|---|
| *Find the latest Acme Corp invoice… enter it into our internal system* | Portal login, a 503 outage, a list that isn't sorted by date, date-format conversion, an expired ERP session, the ≥10k approval gate, verification |
| *Record the Globex invoice from our inbox in Ledgerly* | A different source (an e-mail body), EUR currency, below the approval threshold, so it runs fully autonomously |
| *Enter the latest Acme invoice into Ledgerly* | Ambiguity: "Acme" matches two vendors, so the worker asks which one you mean |
| *Pay the open Acme Corp invoice now* | An unsafe request: payment release is blocked by policy and the worker reports that it is blocked |

---

## Architecture

```
            ┌───────────────────────── TaskWorker (worker/agent.py) ────────────────────────┐
 task ───►  │  AgentLoop:  brain.step() ──► tool calls ──► ToolBox.execute() ──► results ─┐ │
            │                ▲                                                         │ │
            │                └─────────────── observations (append-only history) ◄──────┘ │
            │  guards: step budget · repeated-failure detector · nudges · refusal handling  │
            │                                                                               │
            │  finish(status="completed") ──► Verifier (separate agent, read-only tools)    │
            │                                  └─ fail ─► sent back to worker (1 retry)     │
            └───────────────────────────────────────────────────────────────────────────────┘
 ToolBox (worker/tools/toolbox.py)
   browser_* ── text-mode browser with element refs (worker/tools/browser.py)
   http_get · files (sandboxed) · get_credentials (vault placeholders) · remember/recall
   update_plan · capture_evidence · ask_user · finish
   ├─ retry with exponential backoff, for idempotent tools only
   ├─ Policy gate on every submit (worker/policy.py) ──► human approval (worker/human.py)
   └─ Trace: redacted JSONL + evidence files + live listeners (worker/trace.py)
```

| Module | Responsibility |
|---|---|
| `worker/agent.py` | Agent loop, worker and verifier prompts, verification handshake, run result |
| `worker/brain.py` | `LLMBrain` (Anthropic SDK) and `ScriptedBrain` (deterministic test double) |
| `worker/tools/browser.py` | Headless browser: HTML → accessibility-style snapshot with `[e7]` refs; forms, selects, hidden fields, cookies |
| `worker/tools/toolbox.py` | Tool schemas, dispatch, retries, approval gate, file sandbox |
| `worker/policy.py` | Code-enforced safety rules (approval thresholds, forbidden actions, read-only mode, host allow-list) |
| `worker/vault.py` | Credentials. The model only ever sees `{{secret:…}}` placeholders |
| `worker/memory.py` | Working memory (facts with sources) and lessons that persist across runs |
| `worker/report.py`, `worker/console.*` | HTML report and live web console (SSE) |
| `simworld/app.py` | The simulated company: mail, vendor portal, ERP (HTML + JSON API), fault injection |
| `workspace/` | The company's shared drive: systems directory, AP procedure, vendor master (plus the locally generated, git-ignored vault) |

---

## How each requirement is met

| Requirement | How |
|---|---|
| **Understand the end goal** | The worker gets the goal, not steps. Its prompt says to define "done" first and to *discover* where things live from the workspace docs (`systems.md`, `ap_procedure.md`, `vendors.csv`) rather than from hard-coded knowledge. |
| **Break it into actions** | `update_plan` records an explicit step list with statuses. It is revised after surprises and shown live in the console. |
| **Use tools** | A browser for the vendor portal and ERP, an HTTP API client, a sandboxed file system, a credential vault and the human channel, all exposed as LLM tools. |
| **Observe each result** | Every action returns a fresh page snapshot or response. The model is told never to assume success and to confirm it on the resulting page. |
| **Decide the next step** | A ReAct-style loop with adaptive thinking. The whole observation history (including signed thinking blocks) is kept append-only, so reasoning carries over between steps. |
| **Remember information** | `remember(key, value, source)` builds working memory with provenance, which feeds the summary and the report. `lessons_learned` from `finish` persist to `workspace/memory/lessons.json` and are given to future runs. |
| **Detect failures** | HTTP 5xx and timeouts become `TransientError`. Validation messages, expired sessions, policy blocks and stale element refs come back as `is_error` tool results that say what went wrong. |
| **Retry or take an alternative** | Idempotent reads are retried automatically with backoff. Submissions are **never** blindly replayed (the model is told to check state first). A loop guard flags the 3rd identical failure and pushes the model to change approach or ask. The model fixes causes itself: it converts `31 October 2026` → `2026-10-31` and signs back in after a session expiry. |
| **Verify the outcome** | Two layers. (1) The worker must re-open the saved record and capture it as evidence. (2) A separate **verifier agent** with read-only tools and its own sessions compares the ERP record (via API) against the vendor's source document. A failed verdict is returned to the worker with the failed checks so it can fix the problem. A second failure marks the run *failed*, never "done". |
| **Ask for clarification or approval** | `ask_user` covers ambiguity and missing information. Approvals are **enforced by the harness, not the prompt**: any write to a system of record at or above 10,000, any write to an unknown system, or (in `always` mode) every write pauses for a human. Approval is tied to the exact field values, so a resubmission of the same values after a session expiry reuses it and changed values need a new approval. Moving money or deleting records is blocked outright. |
| **Concise summary and evidence** | `finish` returns a short summary, key results, cited evidence IDs (saved HTML and snapshots of the source page and the confirmation page) and how the outcome was confirmed. The report shows all of this alongside the auditor's checks and the full timeline. |

---

## Design decisions

**Safety lives in code, not in the prompt.** The model is told the rules, but `Policy.assess_submit` classifies every outgoing form before it is sent: read, sign-in, write, high-risk or forbidden. A prompt injection or a confused model can't skip an approval or press "Release payment". Navigation is limited to an allow-list of hosts, the file tools are sandboxed (no `../`, no `vault.json`, writes only under `output/`), and the verifier runs with a `read_only` policy.

**The model never sees secrets.** `get_credentials` returns `{{secret:ledgerly_erp.password}}`. The browser and HTTP tools substitute the real value at send time, and the trace redacts any secret value that appears anywhere. A test asserts that no password appears in the trace or report.

**The verifier is independent.** "Did it work?" is answered by a different agent with a different prompt ("don't trust the claims"), read-only permissions and fresh sessions, checking the system of record *and* the source document. This catches extraction mistakes that self-checking misses. A test injects a wrong amount and confirms the run is marked failed.

**The browser works on text snapshots, not pixels.** Pages are rendered into an accessibility-tree style snapshot with element refs, the same interaction model as Playwright MCP. For internal web apps this is cheaper, faster and more reliable than screenshots, and it is deterministic, which made the failure tests possible. The `Browser` interface is narrow (`navigate/click/fill/snapshot`), so a Playwright backend or a screenshot-based computer-use model could be added for pixel-only apps without touching the loop.

**Retries are idempotency-aware.** Re-fetching a page is safe. Re-posting a form that may already have gone through is not. Only idempotent tools auto-retry. Failed submissions go back to the model with "check the current state before trying again".

**The brain is an interface, with a scripted test double.** `ScriptedBrain` replays a strategy that *reacts to observations* (expired session → sign in again, validation error → fix the field, declined → stop). That lets the whole harness (retries, approvals, verifier, evidence, report) be tested end to end, deterministically and without an API key. A mock-transport test checks the real `LLMBrain` request (model, adaptive thinking, prompt caching, server-side refusal fallback) and that thinking blocks are passed back unchanged.

**LLM API usage.** `claude-opus-5-5` with adaptive thinking (summaries shown in the UI as "reasoning"), `effort: high` for the worker and `medium` for the verifier, prompt caching on the stable system prompt and tools plus automatic caching of the growing history, `fallbacks: "default"` for safety-classifier declines, and typed error handling (auth, rate limit, connection, refusal). There is no assistant prefill and no forced `tool_choice`, since neither is supported on this model.

---

## What the tests cover

```
tests/test_harness.py       end-to-end runs against SimWorld
  ✓ happy path: 503 auto-retry, unsorted list → latest by date, session expiry recovery,
    one approval (reused on resubmit), verified, evidence saved, no secrets leaked, lessons stored
  ✓ approver declines → nothing written, status "blocked"
  ✓ ERP rejects the date format → error detected and corrected
  ✓ wrong extracted amount → independent verifier fails the run
  ✓ ambiguous vendor → clarifying question
  ✓ second run on the same invoice → duplicate detected, nothing re-entered
tests/test_components.py    browser refs/forms/hidden fields, stale-ref errors, host allow-list,
                            policy rules, vault placeholders, file sandbox, retry exhaustion
tests/test_llm_brain.py     LLM request shape + thinking-block round trip (mock transport)
```

## Limitations and next steps

- **Live evaluation.** The tests prove the harness. The next step is an eval set of about 30 tasks scored on success rate, false "completed" claims, unnecessary questions, steps and cost per task, used to tune the prompts and effort level.
- **Durable runs.** Runs live in memory, and a crash loses the in-flight run (the trace survives). Persisting the loop state would allow pause and resume across approvals that take hours.
- **Pixel-only apps.** Add a Playwright/computer-use backend behind the `Browser` interface for desktop or canvas apps.
- **Policy as configuration.** Thresholds and forbidden actions are code today. They would move to per-team YAML with RBAC and an audit log of who approved what.
- **Context growth.** Long tasks would use server-side compaction or tool-result clearing. Snapshots are already capped at 6k characters.
