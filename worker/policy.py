"""Safety policy, enforced in code rather than left to the model's judgement.

The model is told about the rules, but the harness does not rely on it following them:
every form submission and API call is classified here before it is sent, and anything
that changes a system of record above the risk threshold is held for human approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .tools.browser import SubmitRequest

AMOUNT_FIELD_RE = re.compile(r"amount|total|sum|value", re.I)
HIGH_RISK_RE = re.compile(r"\b(pay|payment|release|wire|transfer|delete|remove|void|refund)\b", re.I)


class PolicyBlocked(Exception):
    """Raised to stop an action. The message is shown to the model as the tool error."""


@dataclass
class Assessment:
    risk: str               # read | auth | write | high
    needs_approval: bool
    reason: str
    system: str = ""
    amount: float | None = None


@dataclass
class Policy:
    allowed_hosts: set[str]
    # URL path prefix -> human name. Writes here change business records.
    systems_of_record: dict[str, str] = field(default_factory=lambda: {"/erp/": "Ledgerly ERP"})
    approval_threshold: float = 10_000.0
    approval_mode: str = "threshold"   # threshold | always | never (never = fully autonomous)
    # Actions the agent may never take on its own, even with approval (e.g. moving money).
    forbidden: re.Pattern = HIGH_RISK_RE
    read_only: bool = False            # the verifier runs with read_only=True

    def assess_submit(self, req: SubmitRequest) -> Assessment:
        path = urlparse(req.url).path
        system = next((name for prefix, name in self.systems_of_record.items() if path.startswith(prefix)), "")
        if req.method == "GET":
            return Assessment("read", False, "Read-only form (GET).", system)
        if any("password" in k.lower() for k in req.fields) or path.endswith("/login"):
            return Assessment("auth", False, "Sign-in form.", system)
        if self.read_only:
            raise PolicyBlocked("This agent is read-only (verification mode): state-changing submissions "
                                "are not allowed.")
        if self.forbidden.search(req.button_label) or self.forbidden.search(path):
            raise PolicyBlocked(f"Action '{req.button_label}' ({req.method} {path}) moves money or deletes "
                                "records. That is outside this worker's mandate and is blocked by policy; "
                                "a human must do it.")
        amount = _amount(req.fields)
        if not system:
            return Assessment("write", self.approval_mode != "never",
                              "State-changing submission to a system that is not a known system of record.",
                              urlparse(req.url).netloc, amount)
        if self.approval_mode == "always":
            return Assessment("write", True, f"Policy requires approval for every write to {system}.", system, amount)
        if self.approval_mode == "threshold" and amount is not None and amount >= self.approval_threshold:
            return Assessment("high", True, f"Amount {amount:,.2f} is at or above the approval threshold "
                                            f"of {self.approval_threshold:,.2f}.", system, amount)
        return Assessment("write", False, f"Write to {system} within autonomous limits.", system, amount)


def _amount(fields: dict[str, str]) -> float | None:
    for k, v in fields.items():
        if AMOUNT_FIELD_RE.search(k):
            try:
                return float(str(v).replace(",", "").strip())
            except ValueError:
                return None
    return None
