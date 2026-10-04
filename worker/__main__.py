"""Command line entry point.

    python -m worker run "Find the latest invoice from Acme Corp ..."   # LLM-powered worker
    python -m worker run --offline "..."                                 # scripted demo, no API key
    python -m worker console                                             # web UI on :8000
    python -m worker sim                                                 # SimWorld only, on :8100
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .human import ConsoleHuman, ScriptedHuman

DEMO_TASK = ("Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our "
             "internal system, and tell me once it is done.")

ICONS = {"plan": "📋", "action": "▶", "retry": "🔁", "policy": "🛡 ", "approval": "✅", "human_request": "🙋",
         "human_response": "💬", "memory": "🧠", "evidence": "📎", "loop_guard": "⚠ ", "error": "❌",
         "verification_started": "🔎", "verification_result": "🔎"}


def print_event(ev: dict) -> None:
    t, who = ev["type"], ("  [verifier] " if ev.get("actor") == "verifier" else "")
    if t == "thought" and ev.get("text"):
        print(f"\n{who}💭 {ev['text'].strip()[:400]}")
    elif t == "action":
        print(f"{who}{ICONS[t]} {ev['tool']} {json.dumps(ev['input'])[:160]}")
    elif t == "observation":
        first = ev["content"].strip().splitlines()[0][:140] if ev["content"].strip() else ""
        print(f"{who}   {'❌' if ev['is_error'] else '↳'} {first}")
    elif t == "plan":
        print(f"{who}📋 plan: " + " → ".join(s["step"] for s in ev["steps"]))
    elif t == "retry":
        print(f"{who}🔁 transient error, retry {ev['attempt']} in {ev['delay_s']}s: {ev['error'][:100]}")
    elif t == "policy" and ev.get("decision") != "allowed":
        print(f"{who}🛡  policy {ev['decision']}: {ev['reason']}")
    elif t == "approval":
        print(f"{who}{'✅ approved' if ev['approved'] else '⛔ declined'}{' (reused)' if ev.get('reused') else ''}")
    elif t == "memory":
        print(f"{who}🧠 {ev['key']} = {ev['value']}")
    elif t == "evidence":
        print(f"{who}📎 evidence {ev['id']}: {ev['label']}")
    elif t == "verification_started":
        print("\n🔎 Independent verification (read-only auditor agent)...")
    elif t == "verification_result":
        print(f"🔎 verdict: {'PASSED' if ev.get('passed') else 'FAILED'} - {ev.get('summary')}")
    elif t in ("loop_guard", "error"):
        print(f"{who}{ICONS[t]} {ev.get('error') or ev.get('tool')}")


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a git-ignored .env file without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(prog="python -m worker", description="Autonomous AI task worker")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one task")
    r.add_argument("task", nargs="?", default=DEMO_TASK)
    r.add_argument("--offline", action="store_true", help="use the scripted brain (no API key needed)")
    r.add_argument("--auto-approve", action="store_true", help="approve every gated action automatically")
    r.add_argument("--approval-mode", choices=["threshold", "always", "never"], default="threshold")
    r.add_argument("--no-verify", action="store_true", help="skip the independent verifier")
    r.add_argument("--reset", action="store_true", help="reset SimWorld state before running")
    r.add_argument("--sim-url", default=None)
    c = sub.add_parser("console", help="web console")
    c.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("sim", help="run SimWorld only")
    s.add_argument("--port", type=int, default=8100)
    args = p.parse_args(argv)

    from . import runtime

    if args.cmd == "sim":
        import uvicorn
        from simworld.app import app
        uvicorn.run(app, host="127.0.0.1", port=args.port)
        return 0
    if args.cmd == "console":
        import uvicorn
        from .console import app
        runtime.ensure_sim()
        print(f"Console: http://127.0.0.1:{args.port}   SimWorld: {runtime.DEFAULT_SIM_URL}")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    sim_url = args.sim_url or runtime.DEFAULT_SIM_URL
    runtime.ensure_sim(sim_url)
    if args.reset:
        runtime.reset_sim(sim_url)
    env = runtime.environment(sim_url, args.approval_mode)
    human = ScriptedHuman(approve=True) if args.auto_approve else ConsoleHuman()
    trace = runtime.new_trace(env)
    trace.listeners.append(print_event)
    worker = runtime.build_worker(env, human, offline=args.offline, verify=not args.no_verify, trace=trace)
    print(f"Task: {args.task}\nBrain: {'scripted (offline demo)' if args.offline else 'LLM'}  ·  run {trace.run_id}")
    result = worker.run(args.task)

    print("\n" + "=" * 70)
    badge = {True: "verified ✔", False: "VERIFICATION FAILED", None: "not verified"}[result.verified]
    print(f"RESULT: {result.status.upper()} ({badge}) in {result.steps} steps")
    print(result.summary)
    for k, v in result.results.items():
        print(f"  {k}: {v}")
    print(f"Evidence: {', '.join(result.evidence) or 'none'}")
    print(f"Report:   {result.run_dir / 'report.html'}")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
