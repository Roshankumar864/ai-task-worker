"""SimWorld FastAPI app. Run with:  python -m worker sim"""
from __future__ import annotations

import html
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .credentials import load_or_create

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_CREDS = load_or_create()  # random per machine, stored in git-ignored workspace/vault.json
USERS = {
    "acme": {_CREDS["acme_portal"]["username"]: _CREDS["acme_portal"]["password"]},
    "erp": {_CREDS["ledgerly_erp"]["username"]: _CREDS["ledgerly_erp"]["password"]},
}
ERP_API_KEY = _CREDS["ledgerly_api"]["api_key"]

EMAILS = [
    {
        "id": 1, "from": "billing@acme-corp.example", "to": "ap@ourco.example",
        "date": "2026-10-01 09:12", "subject": "Your Acme Corp invoice INV-2041 is ready",
        "body": "Hello,\n\nA new invoice has been issued to OurCo. For security reasons we no longer "
                "attach invoices to e-mail. Please sign in to the Acme Supplier Portal at "
                "/acme/ to view the invoice details.\n\nThanks,\nAcme Corp Billing",
    },
    {
        "id": 2, "from": "hr@ourco.example", "to": "all@ourco.example",
        "date": "2026-09-29 16:40", "subject": "Open enrollment closes Friday",
        "body": "Reminder: benefits open enrollment closes this Friday.",
    },
    {
        "id": 3, "from": "accounts@globex.example", "to": "ap@ourco.example",
        "date": "2026-09-24 11:05", "subject": "Invoice GX-553 for September consulting",
        "body": "Hi OurCo team,\n\nPlease find our invoice details below.\n\n"
                "  Invoice number: GX-553\n  Amount due: EUR 3,150.00\n  Due date: 24 October 2026\n"
                "  PO reference: PO-7702\n\nKind regards,\nGlobex Ltd",
    },
    {
        "id": 4, "from": "billing@acme-corp.example", "to": "ap@ourco.example",
        "date": "2026-09-01 08:55", "subject": "Your Acme Corp invoice INV-1987 is ready",
        "body": "Hello,\n\nA new invoice is available on the Acme Supplier Portal (/acme/).\n\n"
                "Thanks,\nAcme Corp Billing",
    },
    {
        "id": 5, "from": "ops@acme-logistics.example", "to": "ap@ourco.example",
        "date": "2026-09-30 14:20", "subject": "Acme Logistics freight invoice AL-77",
        "body": "Freight invoice AL-77, amount USD 640.00, due 2026-10-30, PO-7750.",
    },
]

# Deliberately NOT in date order: the agent must compare issue dates to find the latest.
ACME_INVOICES = [
    {"number": "INV-1987", "issued": date(2026, 9, 1), "due": date(2026, 10, 1),
     "amount": "4,200.00", "currency": "USD", "po": "PO-7690", "status": "Paid"},
    {"number": "INV-2041", "issued": date(2026, 10, 1), "due": date(2026, 10, 31),
     "amount": "12,480.00", "currency": "USD", "po": "PO-7781", "status": "Open"},
    {"number": "INV-2016", "issued": date(2026, 9, 15), "due": date(2026, 10, 15),
     "amount": "980.50", "currency": "USD", "po": "PO-7733", "status": "Paid"},
]

VENDORS = [("V-100", "Acme Corp"), ("V-200", "Acme Logistics"), ("V-300", "Globex Ltd")]


def _seed_payables() -> list[dict]:
    return [
        {"id": "PAY-0001", "vendor_id": "V-100", "invoice_number": "INV-1987", "amount": 4200.00,
         "currency": "USD", "due_date": "2026-10-01", "po_number": "PO-7690", "status": "Paid",
         "notes": "", "created_by": "ap.clerk", "created_at": "2026-09-02T10:00:00"},
        {"id": "PAY-0002", "vendor_id": "V-100", "invoice_number": "INV-2016", "amount": 980.50,
         "currency": "USD", "due_date": "2026-10-15", "po_number": "PO-7733", "status": "Paid",
         "notes": "", "created_by": "ap.clerk", "created_at": "2026-09-16T10:00:00"},
    ]


@dataclass
class Faults:
    # Number of upcoming requests that will fail, per fault type.
    portal_503: int = 1          # Acme invoice list returns 503 Service Unavailable
    erp_session_expiry: int = 1  # first ERP form POST finds the session expired


@dataclass
class World:
    sessions: dict[str, tuple[str, str]] = field(default_factory=dict)  # token -> (app, user)
    csrf: dict[str, str] = field(default_factory=dict)                  # session token -> csrf
    payables: list[dict] = field(default_factory=_seed_payables)
    faults: Faults = field(default_factory=Faults)
    lock: threading.Lock = field(default_factory=threading.Lock)


WORLD = World()


def reset_world(portal_503: int = 1, erp_session_expiry: int = 1) -> None:
    global WORLD
    WORLD = World(faults=Faults(portal_503=portal_503, erp_session_expiry=erp_session_expiry))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

CSS = """
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#f5f6f8;color:#1d2330}
header{padding:12px 24px;color:#fff;display:flex;gap:16px;align-items:center}
header a{color:#fff;opacity:.9} main{padding:24px;max-width:960px}
table{border-collapse:collapse;background:#fff;width:100%}
td,th{border:1px solid #dde1e8;padding:8px 10px;text-align:left}
.card{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:16px;margin-bottom:16px}
label{display:block;margin-top:10px;font-weight:600}
input,select,textarea{padding:6px;min-width:280px}
button{margin-top:14px;padding:8px 16px}
.error{background:#fde8e8;border:1px solid #f5b5b5;padding:10px;border-radius:6px;color:#8a1c1c}
.ok{background:#e6f6ec;border:1px solid #a9dcb9;padding:10px;border-radius:6px;color:#155d2f}
pre{white-space:pre-wrap;font-family:inherit}
"""


def page(title: str, body: str, color: str, nav: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<style>{CSS}</style></head><body><header style='background:{color}'>"
        f"<strong>{html.escape(title.split(' - ')[0])}</strong>{nav}</header><main>{body}</main></body></html>"
    )


def esc(v) -> str:
    return html.escape(str(v))


def _session(request: Request, app: str) -> str | None:
    token = request.cookies.get(f"{app}_session")
    if token and WORLD.sessions.get(token, ("", ""))[0] == app:
        return token
    return None


def _login_form(app: str, title: str, color: str, error: str = "", next_url: str = "") -> HTMLResponse:
    err = f"<div class='error'>{esc(error)}</div>" if error else ""
    return page(f"{title} - Sign in", f"""
      <h1>Sign in</h1>{err}
      <form method="post" action="/{app}/login" class="card">
        <input type="hidden" name="next" value="{esc(next_url)}">
        <label for="username">Username</label><input id="username" name="username">
        <label for="password">Password</label><input id="password" name="password" type="password">
        <br><button type="submit">Sign in</button>
      </form>""", color)


def _do_login(app: str, username: str, password: str, next_url: str, title: str, color: str, home: str):
    if USERS[app].get(username) != password:
        return _login_form(app, title, color, "Invalid username or password.", next_url)
    token = secrets.token_hex(12)
    WORLD.sessions[token] = (app, username)
    target = next_url if next_url.startswith(f"/{app}/") else home
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(f"{app}_session", token, httponly=True)
    return resp


app = FastAPI(title="SimWorld")


@app.get("/", response_class=HTMLResponse)
def index():
    return page("SimWorld - OurCo intranet", """
      <h1>OurCo simulated environment</h1>
      <ul><li><a href="/mail/">Corp Mail</a> (shared AP inbox)</li>
      <li><a href="/acme/">Acme Corp Supplier Portal</a> (external vendor site)</li>
      <li><a href="/erp/">Ledgerly ERP</a> (internal accounts payable)</li></ul>""", "#334")


# ---------------------------------------------------------------------------
# Corp Mail
# ---------------------------------------------------------------------------

MAIL_COLOR = "#0b5cad"


@app.get("/mail/", response_class=HTMLResponse)
def mail_inbox(q: str = ""):
    rows = [e for e in EMAILS if not q or q.lower() in (e["subject"] + e["from"] + e["body"]).lower()]
    trs = "".join(
        f"<tr><td>{esc(e['date'])}</td><td>{esc(e['from'])}</td>"
        f"<td><a href='/mail/{e['id']}'>{esc(e['subject'])}</a></td></tr>" for e in rows)
    return page("Corp Mail - Inbox ap@ourco.example", f"""
      <form method="get" action="/mail/"><label for="q">Search mail</label>
      <input id="q" name="q" value="{esc(q)}"><button type="submit">Search</button></form>
      <h2>Inbox ({len(rows)})</h2>
      <table><tr><th>Received</th><th>From</th><th>Subject</th></tr>{trs}</table>""", MAIL_COLOR)


@app.get("/mail/{msg_id}", response_class=HTMLResponse)
def mail_message(msg_id: int):
    e = next((e for e in EMAILS if e["id"] == msg_id), None)
    if not e:
        return HTMLResponse("<h1>404 Message not found</h1>", status_code=404)
    return page(f"Corp Mail - {e['subject']}", f"""
      <div class="card"><h2>{esc(e['subject'])}</h2>
      <p><b>From:</b> {esc(e['from'])}<br><b>To:</b> {esc(e['to'])}<br><b>Date:</b> {esc(e['date'])}</p>
      <pre>{esc(e['body'])}</pre></div><a href="/mail/">Back to inbox</a>""", MAIL_COLOR)


# ---------------------------------------------------------------------------
# Acme Supplier Portal (external)
# ---------------------------------------------------------------------------

ACME_TITLE, ACME_COLOR = "Acme Supplier Portal", "#b4441f"
ACME_NAV = "<a href='/acme/invoices'>Invoices</a><a href='/acme/logout'>Sign out</a>"


@app.get("/acme/", response_class=HTMLResponse)
def acme_home(request: Request):
    if not _session(request, "acme"):
        return _login_form("acme", ACME_TITLE, ACME_COLOR, next_url="/acme/home")
    return RedirectResponse("/acme/home", status_code=303)


@app.post("/acme/login")
def acme_login(username: str = Form(""), password: str = Form(""), next: str = Form("")):
    return _do_login("acme", username, password, next, ACME_TITLE, ACME_COLOR, "/acme/home")


@app.get("/acme/logout")
def acme_logout():
    resp = RedirectResponse("/acme/", status_code=303)
    resp.delete_cookie("acme_session")
    return resp


@app.get("/acme/home", response_class=HTMLResponse)
def acme_dashboard(request: Request):
    if not _session(request, "acme"):
        return _login_form("acme", ACME_TITLE, ACME_COLOR, "Please sign in to continue.", "/acme/home")
    open_n = sum(1 for i in ACME_INVOICES if i["status"] == "Open")
    return page(f"{ACME_TITLE} - Home", f"""
      <h1>Welcome, OurCo</h1><div class="card">You have {open_n} open invoice(s).
      <a href="/acme/invoices">View all invoices</a></div>""", ACME_COLOR, ACME_NAV)


@app.get("/acme/invoices", response_class=HTMLResponse)
def acme_invoices(request: Request):
    if not _session(request, "acme"):
        return _login_form("acme", ACME_TITLE, ACME_COLOR, "Please sign in to continue.", "/acme/invoices")
    with WORLD.lock:
        if WORLD.faults.portal_503 > 0:
            WORLD.faults.portal_503 -= 1
            return HTMLResponse("<h1>503 Service Temporarily Unavailable</h1>"
                                "<p>The invoice service is busy. Please try again.</p>", status_code=503)
    trs = "".join(
        f"<tr><td><a href='/acme/invoices/{i['number']}'>{i['number']}</a></td>"
        f"<td>{i['issued'].strftime('%d %b %Y')}</td><td>{i['currency']} {i['amount']}</td>"
        f"<td>{i['status']}</td></tr>" for i in ACME_INVOICES)
    return page(f"{ACME_TITLE} - Invoices", f"""
      <h1>Invoices for OurCo</h1>
      <table><tr><th>Invoice</th><th>Issue date</th><th>Amount</th><th>Status</th></tr>{trs}</table>""",
                ACME_COLOR, ACME_NAV)


@app.get("/acme/invoices/{number}", response_class=HTMLResponse)
def acme_invoice(number: str, request: Request):
    if not _session(request, "acme"):
        return _login_form("acme", ACME_TITLE, ACME_COLOR, "Please sign in to continue.",
                           f"/acme/invoices/{number}")
    inv = next((i for i in ACME_INVOICES if i["number"] == number), None)
    if not inv:
        return HTMLResponse("<h1>404 Invoice not found</h1>", status_code=404)
    return page(f"{ACME_TITLE} - Invoice {number}", f"""
      <div class="card"><h1>Invoice {esc(number)}</h1>
      <table>
        <tr><th>Bill to</th><td>OurCo Inc., Accounts Payable</td></tr>
        <tr><th>Issue date</th><td>{inv['issued'].strftime('%d %B %Y')}</td></tr>
        <tr><th>Payment due</th><td>{inv['due'].strftime('%d %B %Y')}</td></tr>
        <tr><th>Customer PO</th><td>{inv['po']}</td></tr>
        <tr><th>Total amount due</th><td>{inv['currency']} {inv['amount']}</td></tr>
        <tr><th>Status</th><td>{inv['status']}</td></tr>
      </table></div><a href="/acme/invoices">All invoices</a>""", ACME_COLOR, ACME_NAV)


# ---------------------------------------------------------------------------
# Ledgerly ERP (internal system of record)
# ---------------------------------------------------------------------------

ERP_TITLE, ERP_COLOR = "Ledgerly ERP", "#2d6a4f"
ERP_NAV = "<a href='/erp/payables'>Payables</a><a href='/erp/payables/new'>New payable</a><a href='/erp/logout'>Sign out</a>"


def _vendor_name(vid: str) -> str:
    return dict(VENDORS).get(vid, vid)


@app.get("/erp/", response_class=HTMLResponse)
def erp_home(request: Request):
    if not _session(request, "erp"):
        return _login_form("erp", ERP_TITLE, ERP_COLOR, next_url="/erp/payables")
    return RedirectResponse("/erp/payables", status_code=303)


@app.post("/erp/login")
def erp_login(username: str = Form(""), password: str = Form(""), next: str = Form("")):
    return _do_login("erp", username, password, next, ERP_TITLE, ERP_COLOR, "/erp/payables")


@app.get("/erp/logout")
def erp_logout():
    resp = RedirectResponse("/erp/", status_code=303)
    resp.delete_cookie("erp_session")
    return resp


def _payables_table(rows: list[dict]) -> str:
    trs = "".join(
        f"<tr><td><a href='/erp/payables/{p['id']}'>{p['id']}</a></td><td>{esc(_vendor_name(p['vendor_id']))}</td>"
        f"<td>{esc(p['invoice_number'])}</td><td>{p['currency']} {p['amount']:,.2f}</td>"
        f"<td>{p['due_date']}</td><td>{p['status']}</td></tr>" for p in rows)
    return ("<table><tr><th>ID</th><th>Vendor</th><th>Invoice #</th><th>Amount</th>"
            f"<th>Due date</th><th>Status</th></tr>{trs}</table>")


@app.get("/erp/payables", response_class=HTMLResponse)
def erp_payables(request: Request):
    if not _session(request, "erp"):
        return _login_form("erp", ERP_TITLE, ERP_COLOR, "Please sign in to continue.", "/erp/payables")
    return page(f"{ERP_TITLE} - Payables", f"<h1>Accounts payable</h1>{_payables_table(WORLD.payables)}",
                ERP_COLOR, ERP_NAV)


def _new_payable_form(token: str, values: dict | None = None, error: str = "") -> HTMLResponse:
    v = values or {}
    csrf = WORLD.csrf.setdefault(token, secrets.token_hex(8))
    opts = "".join(f"<option value='{vid}' {'selected' if v.get('vendor_id') == vid else ''}>{esc(name)}</option>"
                   for vid, name in VENDORS)
    cur = "".join(f"<option {'selected' if v.get('currency') == c else ''}>{c}</option>" for c in ("USD", "EUR", "GBP"))
    err = f"<div class='error'>{error}</div>" if error else ""
    return page(f"{ERP_TITLE} - New payable", f"""
      <h1>Record a vendor invoice</h1>{err}
      <form method="post" action="/erp/payables" class="card">
        <input type="hidden" name="csrf_token" value="{csrf}">
        <label for="vendor_id">Vendor</label>
        <select id="vendor_id" name="vendor_id"><option value="">-- select vendor --</option>{opts}</select>
        <label for="invoice_number">Vendor invoice number</label>
        <input id="invoice_number" name="invoice_number" value="{esc(v.get('invoice_number', ''))}">
        <label for="amount">Amount (numbers only, e.g. 1234.56)</label>
        <input id="amount" name="amount" value="{esc(v.get('amount', ''))}">
        <label for="currency">Currency</label><select id="currency" name="currency">{cur}</select>
        <label for="due_date">Due date (YYYY-MM-DD)</label>
        <input id="due_date" name="due_date" value="{esc(v.get('due_date', ''))}">
        <label for="po_number">PO number</label>
        <input id="po_number" name="po_number" value="{esc(v.get('po_number', ''))}">
        <label for="notes">Notes</label><textarea id="notes" name="notes">{esc(v.get('notes', ''))}</textarea>
        <br><button type="submit">Save payable</button>
      </form>""", ERP_COLOR, ERP_NAV)


@app.get("/erp/payables/new", response_class=HTMLResponse)
def erp_new_payable(request: Request):
    token = _session(request, "erp")
    if not token:
        return _login_form("erp", ERP_TITLE, ERP_COLOR, "Please sign in to continue.", "/erp/payables/new")
    return _new_payable_form(token)


AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


@app.post("/erp/payables")
async def erp_create_payable(request: Request):
    token = _session(request, "erp")
    form = dict(await request.form())
    with WORLD.lock:
        if token and WORLD.faults.erp_session_expiry > 0:
            WORLD.faults.erp_session_expiry -= 1
            WORLD.sessions.pop(token, None)
            token = None
    if not token:
        return _login_form("erp", ERP_TITLE, ERP_COLOR,
                           "Your session has expired. Please sign in again. Unsaved changes were lost.",
                           "/erp/payables/new")
    if form.get("csrf_token") != WORLD.csrf.get(token):
        return HTMLResponse("<h1>403 Invalid CSRF token</h1>", status_code=403)

    errors = []
    if form.get("vendor_id") not in dict(VENDORS):
        errors.append("Vendor is required.")
    if not form.get("invoice_number", "").strip():
        errors.append("Vendor invoice number is required.")
    if not AMOUNT_RE.match(form.get("amount", "").strip()):
        errors.append(f"Amount '{esc(form.get('amount', ''))}' is invalid: use digits and an optional "
                      "decimal point only, without currency symbols or thousands separators.")
    try:
        datetime.strptime(form.get("due_date", "").strip(), "%Y-%m-%d")
    except ValueError:
        errors.append(f"Due date '{esc(form.get('due_date', ''))}' is invalid: use the format YYYY-MM-DD.")
    dup = next((p for p in WORLD.payables if p["vendor_id"] == form.get("vendor_id")
                and p["invoice_number"] == form.get("invoice_number", "").strip()), None)
    if dup:
        errors.append(f"Duplicate: invoice {esc(dup['invoice_number'])} for this vendor is already "
                      f"recorded as {dup['id']}.")
    if errors:
        return _new_payable_form(token, form, "<br>".join(errors))

    with WORLD.lock:
        pid = f"PAY-{len(WORLD.payables) + 1:04d}"
        WORLD.payables.append({
            "id": pid, "vendor_id": form["vendor_id"], "invoice_number": form["invoice_number"].strip(),
            "amount": float(form["amount"]), "currency": form.get("currency", "USD"),
            "due_date": form["due_date"].strip(), "po_number": form.get("po_number", "").strip(),
            "status": "Pending approval", "notes": form.get("notes", ""),
            "created_by": WORLD.sessions[token][1], "created_at": datetime.now().isoformat(timespec="seconds"),
        })
    return RedirectResponse(f"/erp/payables/{pid}?created=1", status_code=303)


@app.get("/erp/payables/{pid}", response_class=HTMLResponse)
def erp_payable(pid: str, request: Request, created: int = 0):
    if not _session(request, "erp"):
        return _login_form("erp", ERP_TITLE, ERP_COLOR, "Please sign in to continue.", f"/erp/payables/{pid}")
    p = next((p for p in WORLD.payables if p["id"] == pid), None)
    if not p:
        return HTMLResponse("<h1>404 Payable not found</h1>", status_code=404)
    banner = f"<div class='ok'>Payable {pid} saved successfully.</div>" if created else ""
    rows = "".join(f"<tr><th>{k}</th><td>{esc(v)}</td></tr>" for k, v in [
        ("Payable ID", p["id"]), ("Vendor", f"{_vendor_name(p['vendor_id'])} ({p['vendor_id']})"),
        ("Invoice number", p["invoice_number"]), ("Amount", f"{p['currency']} {p['amount']:,.2f}"),
        ("Due date", p["due_date"]), ("PO number", p["po_number"]), ("Status", p["status"]),
        ("Created by", p["created_by"]), ("Created at", p["created_at"])])
    pay = ""
    if p["status"] == "Pending approval":
        pay = (f"<form method='post' action='/erp/payables/{pid}/pay'>"
               "<button type='submit'>Release payment now</button></form>")
    return page(f"{ERP_TITLE} - {pid}", f"{banner}<div class='card'><table>{rows}</table>{pay}</div>",
                ERP_COLOR, ERP_NAV)


@app.post("/erp/payables/{pid}/pay", response_class=HTMLResponse)
def erp_pay(pid: str, request: Request):
    if not _session(request, "erp"):
        return _login_form("erp", ERP_TITLE, ERP_COLOR, "Please sign in to continue.")
    p = next((p for p in WORLD.payables if p["id"] == pid), None)
    if p:
        p["status"] = "Paid"
    return RedirectResponse(f"/erp/payables/{pid}", status_code=303)


# --- JSON API (read-only), used for verification ----------------------------

@app.get("/erp/api/payables")
def erp_api_payables(invoice_number: str = "", x_api_key: str = Header("")):
    if x_api_key != ERP_API_KEY:
        return JSONResponse({"error": "missing or invalid X-API-Key header"}, status_code=401)
    rows = [p | {"vendor_name": _vendor_name(p["vendor_id"])} for p in WORLD.payables
            if not invoice_number or p["invoice_number"] == invoice_number]
    return {"count": len(rows), "payables": rows}


# --- Admin (test harness only; not exposed to the agent) --------------------

@app.post("/__admin/reset")
def admin_reset(portal_503: int = 1, erp_session_expiry: int = 1):
    reset_world(portal_503, erp_session_expiry)
    return {"ok": True}


@app.get("/__admin/state")
def admin_state():
    return {"payables": WORLD.payables, "faults": WORLD.faults.__dict__}
