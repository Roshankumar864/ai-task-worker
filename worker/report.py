"""Render a run into a self-contained HTML report (runs/<id>/report.html)."""
from __future__ import annotations

import html
import json

STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b2130;--mut:#5d6578;--line:#e1e4ea;--ok:#177245;--okbg:#e5f5ec;
--bad:#a32020;--badbg:#fbe9e9;--warn:#8a5a00;--warnbg:#fff4dc;--acc:#3451b2;--code:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1e26;--ink:#e6e9ef;--mut:#9aa3b5;--line:#2b313d;
--ok:#5fd394;--okbg:#15301f;--bad:#ff8a8a;--badbg:#3a1a1a;--warn:#f3c262;--warnbg:#33290f;--acc:#8ea6ff;--code:#222733}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:12px}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.warn{background:var(--warnbg);color:var(--warn)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.mut{color:var(--mut)}pre{background:var(--code);padding:8px;border-radius:6px;white-space:pre-wrap;word-break:break-word;
max-height:260px;overflow:auto;margin:6px 0 0;font-size:12px}a{color:var(--acc)}
.ev{border-left:3px solid var(--line);padding:6px 10px;margin:6px 0}.ev.action{border-color:var(--acc)}
.ev.err{border-color:var(--bad)}.ev.gate{border-color:var(--warn)}.ev.verifier{margin-left:24px}
details summary{cursor:pointer}
"""


def _e(x) -> str:
    return html.escape(str(x))


def _event_html(ev: dict) -> str:
    t, actor = ev["type"], ev.get("actor", "worker")
    who = "🔎 verifier" if actor == "verifier" else "🤖 worker"
    cls = "ev" + (" verifier" if actor == "verifier" else "")
    head = f"<span class='mut'>{ev['t']:>6}s · {who}</span> "
    if t == "thought":
        body = ""
        if ev.get("thinking"):
            body += f"<details><summary class='mut'>reasoning</summary><pre>{_e(ev['thinking'])}</pre></details>"
        if ev.get("text"):
            body += f"<div>{_e(ev['text'])}</div>"
        return f"<div class='{cls}'>{head}<b>step {ev['step']}</b>{body}</div>" if body else ""
    if t == "action":
        return (f"<div class='{cls} action'>{head}<b>{_e(ev['tool'])}</b> "
                f"<code>{_e(json.dumps(ev['input'])[:300])}</code></div>")
    if t == "observation":
        c = cls + (" err" if ev.get("is_error") else "")
        return (f"<div class='{c}'>{head}{'❌ error' if ev.get('is_error') else '↳ result'}"
                f"<details><summary class='mut'>{_e(ev['content'][:110])}</summary><pre>{_e(ev['content'])}</pre>"
                "</details></div>")
    if t == "retry":
        return f"<div class='{cls} gate'>{head}🔁 retry {ev['attempt']} of <b>{_e(ev['tool'])}</b> after {ev['delay_s']}s: {_e(ev['error'])}</div>"
    if t == "policy":
        return f"<div class='{cls} gate'>{head}🛡️ policy: <b>{_e(ev['decision'])}</b> — {_e(ev['reason'])}</div>"
    if t in ("human_request", "approval", "human_response"):
        text = ev.get("question") or ev.get("comment") or ev.get("answer") or ""
        label = {"human_request": "🙋 asked the user", "approval": "✅ approved" if ev.get("approved") else "⛔ declined",
                 "human_response": "💬 user answered"}[t]
        return f"<div class='{cls} gate'>{head}{label}: {_e(text)}</div>"
    if t == "memory":
        return f"<div class='{cls}'>{head}🧠 remember <b>{_e(ev['key'])}</b> = {_e(ev['value'])} <span class='mut'>({_e(ev['source'])})</span></div>"
    if t == "plan":
        steps = "".join(f"<li>[{_e(s['status'])}] {_e(s['step'])}</li>" for s in ev["steps"])
        return f"<div class='{cls}'>{head}📋 plan<ol>{steps}</ol></div>"
    if t == "evidence":
        return f"<div class='{cls}'>{head}📎 evidence <a href='{_e(ev['file'])}'>{_e(ev['id'])}</a> {_e(ev['label'])}</div>"
    if t == "loop_guard":
        return f"<div class='{cls} err'>{head}⚠️ loop guard: {_e(ev['tool'])} failed {ev['repeats']}×</div>"
    if t in ("verification_result", "error"):
        return f"<div class='{cls} gate'>{head}{_e(t)}: {_e(ev.get('summary') or ev.get('error'))}</div>"
    return ""


def write_report(trace, task: str, result) -> None:
    ok = result.status == "completed" and result.verified is not False
    pill = ("ok", "Completed") if ok else ("warn", "Needs you") if result.status == "blocked" else ("bad", "Failed")
    vpill = {True: "<span class='pill ok'>Independently verified</span>",
             False: "<span class='pill bad'>Verification failed</span>",
             None: "<span class='pill warn'>Not verified</span>"}[result.verified]
    res_rows = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in result.results.items())
    ev_by_id = {e["id"]: e for e in trace.events if e["type"] == "evidence"}
    ev_rows = "".join(
        f"<tr><td><a href='{_e(ev_by_id[i]['file'])}'>{_e(i)}</a></td><td>{_e(ev_by_id[i]['label'])}</td>"
        f"<td class='mut'>{_e(ev_by_id[i]['url'])}</td></tr>" for i in result.evidence if i in ev_by_id)
    checks = "".join(f"<tr><td>{'✅' if c.get('ok') else '❌'}</td><td>{_e(c.get('claim'))}</td>"
                     f"<td>{_e(c.get('observed'))}</td></tr>" for c in (result.verification or {}).get("checks", []))
    mem = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v['value'])}</td><td class='mut'>{_e(v['source'])}</td></tr>"
                  for k, v in result.memory.items())
    timeline = "".join(_event_html(e) for e in trace.events)
    usage = ", ".join(f"{k}: {v:,}" for k, v in result.usage.items()) or "n/a"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run {_e(result.run_id)}</title><style>{STYLE}</style></head><body><main>
<h1>AI worker run report</h1><div class="mut">Run {_e(result.run_id)} · {result.steps} steps · tokens {usage}</div>
<div class="card"><h2>Task</h2><div>{_e(task)}</div></div>
<div class="card"><h2>Outcome &nbsp;<span class="pill {pill[0]}">{pill[1]}</span> {vpill}</h2><p>{_e(result.summary)}</p>
<table>{res_rows}</table></div>
<div class="card"><h2>Evidence</h2><table>{ev_rows or '<tr><td class="mut">none</td></tr>'}</table></div>
<div class="card"><h2>Independent verification</h2><p>{_e((result.verification or {}).get('summary', 'Not run.'))}</p>
<table>{checks}</table></div>
<div class="card"><h2>Working memory</h2><table>{mem or '<tr><td class="mut">empty</td></tr>'}</table></div>
<div class="card"><h2>Timeline</h2>{timeline}</div>
</main></body></html>"""
    (trace.dir / "report.html").write_text(doc, encoding="utf-8")
