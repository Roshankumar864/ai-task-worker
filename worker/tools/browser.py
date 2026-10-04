"""A headless, text-mode web browser for the agent.

Pages are rendered into a compact "accessibility snapshot": readable text plus numbered
element references ([e3] link, [e7] textbox, ...) that the model acts on. This is the same
interaction model as Playwright MCP / computer-use snapshots, but deterministic and cheap,
which suits a prototype operating on internal web apps.

Every form submission passes through `on_submit`, a hook the harness uses to enforce the
safety policy (human approval for writes to systems of record) before anything is sent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "form", "table", "header",
              "main", "section", "ul", "ol", "pre", "br", "label"}
MAX_SNAPSHOT_CHARS = 6000


class BrowserError(Exception):
    """An action could not be performed (bad ref, unknown option, blocked URL...)."""


class TransientError(BrowserError):
    """A failure that is likely to succeed on retry (5xx, timeout, connection reset)."""


@dataclass
class Element:
    ref: str
    kind: str              # link | textbox | password | select | textarea | button | checkbox
    label: str
    name: str = ""
    href: str = ""
    form: str = ""         # form ref the control belongs to
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, text)


@dataclass
class Form:
    ref: str
    method: str
    action: str
    values: dict[str, str] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)


@dataclass
class SubmitRequest:
    """What the browser is about to send; given to the policy hook."""
    method: str
    url: str
    fields: dict[str, str]          # visible + hidden fields (secrets already resolved)
    display_fields: dict[str, str]  # fields safe to show a human/LLM (secrets masked, hidden dropped)
    button_label: str
    page_title: str


class Browser:
    def __init__(self, client: httpx.Client, allowed_hosts: set[str] | None = None,
                 secret_resolver: Callable[[str], tuple[str, bool]] | None = None,
                 on_submit: Callable[[SubmitRequest], None] | None = None):
        self.client = client
        self.allowed_hosts = allowed_hosts
        self.secret_resolver = secret_resolver or (lambda v: (v, False))
        self.on_submit = on_submit
        self.url = ""
        self.status = 0
        self.title = ""
        self.html = ""
        self.elements: dict[str, Element] = {}
        self.forms: dict[str, Form] = {}
        self.secret_fields: set[tuple[str, str]] = set()  # (form ref, field name) holding secrets
        self.history: list[str] = []

    # -- navigation --------------------------------------------------------------

    def navigate(self, url: str) -> str:
        url = urljoin(self.url or "", url) if self.url else url
        self._check_host(url)
        return self._request("GET", url)

    def back(self) -> str:
        if len(self.history) < 2:
            raise BrowserError("No previous page in history.")
        self.history.pop()
        return self._request("GET", self.history.pop())

    def click(self, ref: str) -> str:
        el = self._element(ref)
        if el.kind == "link":
            return self.navigate(el.href)
        if el.kind == "button":
            return self._submit(el)
        if el.kind == "checkbox":
            form = self.forms[el.form]
            form.values[el.name] = "" if form.values.get(el.name) else "on"
            return f"Toggled checkbox {ref} -> {'checked' if form.values[el.name] else 'unchecked'}"
        raise BrowserError(f"{ref} is a {el.kind}; use browser_fill to enter a value.")

    def fill(self, ref: str, value: str) -> str:
        el = self._element(ref)
        if el.kind not in ("textbox", "password", "textarea", "select"):
            raise BrowserError(f"{ref} is a {el.kind}, which cannot be filled.")
        form = self.forms[el.form]
        resolved, is_secret = self.secret_resolver(value)
        if el.kind == "select":
            match = next((v for v, t in el.options if value.strip().lower() in (v.lower(), t.lower())), None)
            if match is None:
                opts = ", ".join(f"{v!r} ({t})" for v, t in el.options)
                raise BrowserError(f"No option {value!r} in {ref}. Available options: {opts}")
            resolved = match
        form.values[el.name] = resolved
        key = (el.form, el.name)
        if is_secret:
            self.secret_fields.add(key)
        else:
            self.secret_fields.discard(key)
        shown = "â€¢â€¢â€¢â€¢â€¢â€¢ (secret)" if is_secret else repr(resolved)
        return f"Set {ref} ({el.label or el.name}) = {shown}"

    # -- internals ---------------------------------------------------------------

    def _element(self, ref: str) -> Element:
        el = self.elements.get(ref.strip().strip("[]"))
        if not el:
            raise BrowserError(f"No element {ref!r} on the current page. Take a fresh snapshot; refs change "
                               "after every navigation.")
        return el

    def _check_host(self, url: str) -> None:
        host = urlparse(url).netloc
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise BrowserError(f"Navigation to {host!r} is blocked by policy. Allowed hosts: "
                               f"{sorted(self.allowed_hosts)}")

    def _submit(self, button: Element) -> str:
        form = self.forms.get(button.form)
        if not form:
            raise BrowserError(f"{button.ref} is not inside a form.")
        url = urljoin(self.url, form.action or self.url)
        self._check_host(url)
        display = {k: ("â€¢â€¢â€¢â€¢â€¢â€¢" if (form.ref, k) in self.secret_fields else v)
                   for k, v in form.values.items() if k not in form.hidden}
        req = SubmitRequest(form.method, url, dict(form.values), display, button.label, self.title)
        if self.on_submit:
            self.on_submit(req)  # may raise PolicyBlocked
        if form.method == "GET":
            return self._request("GET", url, params=form.values)
        return self._request("POST", url, data=form.values)

    def _request(self, method: str, url: str, **kw) -> str:
        try:
            resp = self.client.request(method, url, follow_redirects=True, **kw)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransientError(f"{type(e).__name__} while loading {url}: {e}") from e
        self._load(str(resp.url), resp.status_code, resp.text)
        if resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code} from {url}: {self._plain_text()[:200]}")
        return self.snapshot()

    def _load(self, url: str, status: int, html: str) -> None:
        self.url, self.status, self.html = url, status, html
        self.history.append(url)
        self.elements, self.forms, self.secret_fields = {}, {}, set()

    def _plain_text(self) -> str:
        return re.sub(r"\s+", " ", BeautifulSoup(self.html, "html.parser").get_text(" ")).strip()

    # -- snapshot rendering ---------------------------------------------------------

    def snapshot(self) -> str:
        soup = BeautifulSoup(self.html, "html.parser")
        self.title = soup.title.get_text(strip=True) if soup.title else ""
        # Re-snapshotting the same page keeps whatever the agent already typed into forms.
        prev_values = {ref: f.values for ref, f in self.forms.items()}
        self.elements, self.forms = {}, {}
        labels = {lab.get("for"): lab.get_text(" ", strip=True) for lab in soup.find_all("label") if lab.get("for")}
        counter = {"e": 0, "f": 0}
        lines: list[str] = []
        buf: list[str] = []

        def flush():
            text = re.sub(r"[ \t]+", " ", "".join(buf))
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
            buf.clear()

        def new_ref(prefix="e"):
            counter[prefix] += 1
            return f"{prefix}{counter[prefix]}"

        def walk(node, form_ref: str = ""):
            for child in node.children:
                if isinstance(child, NavigableString):
                    if child.parent.name not in ("script", "style", "title", "option", "label", "textarea"):
                        buf.append(str(child))
                    continue
                if not isinstance(child, Tag):
                    continue
                tag = child.name
                if tag in ("script", "style", "head"):
                    continue
                if tag == "label" and child.get("for"):
                    continue  # rendered as the control's label instead
                if tag == "form":
                    flush()
                    ref = new_ref("f")
                    self.forms[ref] = Form(ref, (child.get("method") or "GET").upper(), child.get("action") or "",
                                           values=dict(prev_values.get(ref, {})))
                    lines.append(f"form [{ref}] {self.forms[ref].method} {self.forms[ref].action or '(this page)'}")
                    walk(child, ref)
                    flush()
                    lines.append(f"end form [{ref}]")
                    continue
                if tag == "a" and child.get("href"):
                    ref = new_ref()
                    text = child.get_text(" ", strip=True)
                    self.elements[ref] = Element(ref, "link", text, href=child["href"])
                    buf.append(f' [{ref}] link "{text}" ')
                    continue
                if tag in ("input", "select", "textarea", "button"):
                    self._render_control(child, form_ref, labels, new_ref, buf)
                    continue
                if tag in ("td", "th"):
                    buf.append(" | ")
                if tag in BLOCK_TAGS:
                    flush()
                if tag in ("h1", "h2", "h3"):
                    buf.append("#" * int(tag[1]) + " ")
                walk(child, form_ref)
                if tag in BLOCK_TAGS:
                    flush()

        walk(soup.body or soup)
        flush()
        body = "\n".join(lines)
        if len(body) > MAX_SNAPSHOT_CHARS:
            body = body[:MAX_SNAPSHOT_CHARS] + "\n... (truncated)"
        return f"URL: {self.url}\nHTTP status: {self.status}\nTitle: {self.title}\n---\n{body}"

    def _render_control(self, tag: Tag, form_ref: str, labels: dict, new_ref, buf: list[str]) -> None:
        name = tag.get("name", "")
        label = labels.get(tag.get("id"), "") or tag.get("placeholder", "") or name
        form = self.forms.get(form_ref)
        if tag.name == "input" and tag.get("type") == "hidden":
            if form and name:
                form.values[name] = tag.get("value", "")
                form.hidden.add(name)
            return
        if tag.name == "button" or (tag.name == "input" and tag.get("type") in ("submit", "button")):
            ref = new_ref()
            text = tag.get_text(" ", strip=True) or tag.get("value", "Submit")
            self.elements[ref] = Element(ref, "button", text, form=form_ref)
            buf.append(f' [{ref}] button "{text}" ')
            return
        if not form:
            return
        ref = new_ref()
        if tag.name == "select":
            options = [(o.get("value", o.get_text(strip=True)), o.get_text(strip=True)) for o in tag.find_all("option")]
            sel = tag.find("option", selected=True) or (tag.find("option") if options else None)
            value = sel.get("value", sel.get_text(strip=True)) if sel else ""
            form.values.setdefault(name, value)
            self.elements[ref] = Element(ref, "select", label, name=name, form=form_ref, options=options)
            opts = "; ".join(f"{v}={t}" if v != t else v for v, t in options if v)
            buf.append(f'\n  [{ref}] select "{label}" value="{form.values[name]}" options: {opts}\n')
            return
        kind = {"password": "password", "checkbox": "checkbox"}.get(tag.get("type", ""), "textbox")
        if tag.name == "textarea":
            kind, value = "textarea", tag.get_text()
        elif kind == "checkbox":
            value = "on" if tag.has_attr("checked") else ""
        else:
            value = tag.get("value", "")
        form.values.setdefault(name, value)
        self.elements[ref] = Element(ref, kind, label, name=name, form=form_ref)
        shown = "â€¢â€¢â€¢â€¢â€¢â€¢" if kind == "password" and form.values[name] else form.values[name]
        buf.append(f'\n  [{ref}] {kind} "{label}" value="{shown}"\n')
