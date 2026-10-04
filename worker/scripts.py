"""Scripted strategies for ScriptedBrain (offline demo + tests).

These replay what a good agent does on the invoice scenario, *reacting to observations*
(expired sessions, validation errors, user answers), so the harness's real behaviour
(retries, approvals, verification, evidence) runs end to end without an LLM. They only
know this one scenario; the general-purpose worker is LLMBrain.
"""
from __future__ import annotations

import json
import re
from datetime import datetime


def ref(snapshot: str, kind: str, label: str) -> str:
    m = re.search(rf'\[(e\d+)\] {kind} "{re.escape(label)}', snapshot)
    if not m:
        raise LookupError(f"no {kind} {label!r} in snapshot")
    return m.group(1)


def base_url(task_msg: str) -> str:
    # The harness appends the systems base URL after the user's request text.
    return re.findall(r"https?://[\w.:-]+", task_msg)[-1].rstrip("/.")


def iso(d: str) -> str:
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(d)


def invoice_worker(task_msg: str, *, date_mistake: bool = False, wrong_amount: bool = False):
    """Find the latest invoice from a vendor, record it in Ledgerly, confirm it."""
    base = base_url(task_msg)
    request = task_msg.split("Request from", 1)[-1]
    vendor = "Acme Corp" if "acme corp" in request.lower() else None
    if vendor is None and "acme" in request.lower():
        answer = yield ("'Acme' matches two vendors in vendors.csv, and recording the wrong one would be a real "
                        "error, so I'll ask.", "ask_user",
                        {"question": "Which vendor do you mean: Acme Corp (V-100) or Acme Logistics (V-200)?",
                         "options": ["Acme Corp", "Acme Logistics"]})
        vendor = "Acme Corp" if "corp" in answer.lower() else "Acme Logistics"
    if vendor != "Acme Corp":
        yield ("The offline scripted planner only knows the Acme Corp portal flow.", "finish",
               {"status": "blocked", "summary": "Offline demo mode only supports the Acme Corp invoice scenario. "
                "Run with an Anthropic API key for arbitrary tasks.", "results": {}, "evidence": [],
                "verification": "n/a"})
        return

    plan = ["Read the systems directory and AP procedure", "Look up the vendor ID",
            "Find the latest invoice at its source", "Extract amount, due date, PO",
            "Check Ledgerly for duplicates", "Record the payable in Ledgerly",
            "Re-open the saved payable and verify", "Report back with evidence"]
    yield ("Goal: get the newest Acme Corp invoice into Ledgerly correctly, then confirm. Planning first.",
           "update_plan", {"steps": [{"step": s, "status": "pending"} for s in plan]})
    yield ("Find out which systems hold what.", "read_file", {"path": "systems.md"})
    yield ("Check the AP procedure for rules I must follow.", "read_file", {"path": "ap_procedure.md"})
    obs = yield ("Look up the Ledgerly vendor ID.", "read_file", {"path": "vendors.csv"})
    vendor_id = re.search(rf"{vendor},(V-\d+)", obs).group(1)
    yield ("Store the vendor ID.", "remember", {"key": "vendor_id", "value": vendor_id, "source": "vendors.csv"})
    yield ("Check the AP inbox for the latest notice from Acme.", "browser_navigate", {"url": f"{base}/mail/?q=acme-corp"})

    yield ("Acme publishes invoices on its portal. Getting portal credentials.", "get_credentials", {"system": "acme_portal"})
    obs = yield ("Open the portal.", "browser_navigate", {"url": f"{base}/acme/"})
    yield ("Fill in the sign-in form.", "browser_fill", {"fields": [
        {"ref": ref(obs, "textbox", "Username"), "value": "ap@ourco.example"},
        {"ref": ref(obs, "password", "Password"), "value": "{{secret:acme_portal.password}}"}]})
    obs = yield ("Sign in.", "browser_click", {"ref": ref(obs, "button", "Sign in")})
    obs = yield ("Open the invoice list.", "browser_navigate", {"url": f"{base}/acme/invoices"})

    rows = re.findall(r'\[(e\d+)\] link "(INV-\d+)"\s*\|\s*(\d{2} \w{3} \d{4})', obs)
    eref, number, issued = max(rows, key=lambda r: datetime.strptime(r[2], "%d %b %Y"))
    yield (f"The list is not in date order; comparing issue dates, the latest is {number} ({issued}).",
           "remember", {"key": "invoice_number", "value": number, "source": f"{base}/acme/invoices (latest issue date {issued})"})
    obs = yield ("Open the invoice.", "browser_click", {"ref": eref})
    amount_txt = re.search(r"Total amount due\s*\|\s*(\w{3}) ([\d,.]+)", obs)
    currency, amount = amount_txt.group(1), amount_txt.group(2).replace(",", "")
    due = re.search(r"Payment due\s*\|\s*([^|\n]+)", obs).group(1).strip()
    po = re.search(r"Customer PO\s*\|\s*(PO-\d+)", obs).group(1)
    if wrong_amount:
        amount = str(float(amount) / 10)  # simulated extraction slip, for verifier tests
    for k, v in [("amount", amount), ("currency", currency), ("due_date_source", due), ("po_number", po)]:
        yield (f"Recording {k}.", "remember", {"key": k, "value": v, "source": f"Acme portal invoice {number}"})
    yield ("Save the source invoice as evidence.", "capture_evidence", {"label": f"Acme portal invoice {number}"})

    yield ("Now Ledgerly. Getting credentials.", "get_credentials", {"system": "ledgerly_erp"})
    obs = yield ("Open Ledgerly.", "browser_navigate", {"url": f"{base}/erp/"})
    obs = yield from _erp_login(obs)
    if number in obs:
        yield (f"{number} is already recorded; recording it again would create a duplicate.", "finish",
               {"status": "blocked", "summary": f"{number} is already in Ledgerly, so nothing was entered.",
                "results": {"invoice_number": number}, "evidence": [], "verification": "Checked payables list."})
        return
    obs = yield ("No duplicate in the payables list. Opening the new-payable form.", "browser_navigate",
                 {"url": f"{base}/erp/payables/new"})

    due_value = due if date_mistake else iso(due)
    for _attempt in range(4):
        fields = [("select", "Vendor", vendor_id), ("textbox", "Vendor invoice number", number),
                  ("textbox", "Amount", amount), ("select", "Currency", currency),
                  ("textbox", "Due date", due_value), ("textbox", "PO number", po)]
        yield ("Fill in the payable from the source invoice.", "browser_fill",
               {"fields": [{"ref": ref(obs, k, label), "value": v} for k, label, v in fields]})
        obs = yield ("Save the payable.", "browser_click", {"ref": ref(obs, "button", "Save payable")})
        if "session has expired" in obs.lower():
            obs = yield from _erp_login(obs, "Ledgerly says my session expired and the form was lost. Signing in again.")
            if "Record a vendor invoice" not in obs:
                obs = yield ("Back to the form.", "browser_navigate", {"url": f"{base}/erp/payables/new"})
            continue
        if "Due date" in obs and "invalid" in obs:
            due_value = iso(due)
            yield (f"Ledgerly rejected the date format. Converting '{due}' to {due_value}.", "remember",
                   {"key": "due_date", "value": due_value, "source": f"converted from '{due}'"})
            continue
        if obs.startswith("BLOCKED") or "declined" in obs:
            yield ("The user declined the entry, so I stop without saving anything.", "finish",
                   {"status": "blocked", "summary": "The approver declined recording this payable; nothing was saved.",
                    "results": {"invoice_number": number, "amount": f"{currency} {amount}"}, "evidence": [],
                    "verification": "No record was created."})
            return
        break

    pid = re.search(r"Payable (PAY-\d+) saved", obs).group(1)
    yield (f"Saved as {pid}.", "remember", {"key": "payable_id", "value": pid, "source": "Ledgerly confirmation"})
    obs = yield ("Re-read the saved record to check every field.", "browser_snapshot", {})
    yield ("Capture the saved record as evidence.", "capture_evidence", {"label": f"Ledgerly payable {pid}"})
    yield ("Everything checks out. Reporting back.", "finish", {
        "status": "completed",
        "summary": f"Recorded Acme Corp invoice {number} ({currency} {float(amount):,.2f}, due {iso(due)}) in "
                   f"Ledgerly as {pid}, after AP-manager approval. It is pending payment approval; no payment was released.",
        "results": {"vendor": f"{vendor} ({vendor_id})", "invoice_number": number, "amount": f"{amount}",
                    "currency": currency, "due_date": iso(due), "po_number": po, "ledgerly_payable_id": pid},
        "evidence": ["E1", "E2"],
        "verification": f"Opened {pid} in Ledgerly after saving and compared each field to the portal invoice.",
        "lessons_learned": ["Ledgerly due dates must be YYYY-MM-DD; Acme shows dates like '31 October 2026'.",
                            "Acme invoice list is not sorted by date; compare issue dates to find the latest."],
    })


def _erp_login(obs: str, thought: str = "Sign in to Ledgerly."):
    yield (thought, "browser_fill", {"fields": [
        {"ref": ref(obs, "textbox", "Username"), "value": "ap.clerk"},
        {"ref": ref(obs, "password", "Password"), "value": "{{secret:ledgerly_erp.password}}"}]})
    return (yield ("Submit sign-in.", "browser_click", {"ref": ref(obs, "button", "Sign in")}))


def invoice_verifier(msg: str):
    """Independent check: the ERP record (via API) must match the vendor's source invoice."""
    base = base_url(msg)
    report = json.loads(msg.split("final report:\n", 1)[1].split("\n\nSystems are under")[0])
    claims = report["results"]
    yield ("Get API credentials.", "get_credentials", {"system": "ledgerly_api"})
    obs = yield ("Query Ledgerly's API for the invoice.", "http_get", {
        "url": f"{base}/erp/api/payables?invoice_number={claims.get('invoice_number', '')}",
        "headers": {"X-API-Key": "{{secret:ledgerly_api.api_key}}"}})
    data = json.loads(obs.split("\n", 1)[1])
    rec = data["payables"][0] if data.get("count") == 1 else None

    yield ("Now read the source invoice on the vendor portal.", "get_credentials", {"system": "acme_portal"})
    page = yield ("Open the portal.", "browser_navigate", {"url": f"{base}/acme/"})
    if "Sign in" in page:
        yield ("Sign in (read-only).", "browser_fill", {"fields": [
            {"ref": ref(page, "textbox", "Username"), "value": "ap@ourco.example"},
            {"ref": ref(page, "password", "Password"), "value": "{{secret:acme_portal.password}}"}]})
        yield ("Submit sign-in.", "browser_click", {"ref": ref(page, "button", "Sign in")})
    src = yield ("Open the source invoice.", "browser_navigate",
                 {"url": f"{base}/acme/invoices/{claims.get('invoice_number', '')}"})
    m = re.search(r"Total amount due\s*\|\s*\w{3} ([\d,.]+)", src)
    src_amount = float(m.group(1).replace(",", "")) if m else None
    due_m = re.search(r"Payment due\s*\|\s*([^|\n]+)", src)
    src_due = iso(due_m.group(1)) if due_m else None

    checks = [
        {"claim": "Exactly one payable exists for the invoice", "observed": f"API count={data.get('count')}",
         "ok": rec is not None},
        {"claim": f"Payable ID is {claims.get('ledgerly_payable_id')}", "observed": str(rec and rec["id"]),
         "ok": bool(rec) and rec["id"] == claims.get("ledgerly_payable_id")},
        {"claim": "Recorded amount matches the vendor invoice", "observed": f"ERP {rec and rec['amount']} vs source {src_amount}",
         "ok": bool(rec) and src_amount is not None and abs(rec["amount"] - src_amount) < 0.005},
        {"claim": "Recorded due date matches the vendor invoice", "observed": f"ERP {rec and rec['due_date']} vs source {src_due}",
         "ok": bool(rec) and rec["due_date"] == src_due},
        {"claim": "Payment was not released", "observed": str(rec and rec["status"]),
         "ok": bool(rec) and rec["status"] != "Paid"},
    ]
    passed = all(c["ok"] for c in checks)
    yield ("Reporting verdict.", "submit_verdict", {
        "passed": passed, "checks": checks,
        "summary": "Ledgerly record matches the vendor's source invoice." if passed
        else "Ledgerly record does not match the source invoice."})
