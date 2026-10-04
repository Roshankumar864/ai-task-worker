"""SimWorld: a small, deterministic simulated company for the AI worker to operate on.

Three web apps share one FastAPI process:
  /mail   - Corp Mail, the shared accounts-payable inbox
  /acme   - Acme Corp's supplier portal (external vendor website, login required)
  /erp    - Ledgerly, the internal ERP / accounts-payable system (login + CSRF + API)

Faults (transient 503s, expired sessions) can be injected via /__admin/faults so the
agent's failure detection and recovery can be demonstrated and tested.
"""
