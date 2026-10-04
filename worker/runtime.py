"""Shared wiring for the CLI and web console: paths, SimWorld startup, worker construction."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx

from .agent import Environment, TaskWorker
from .brain import LLMBrain, ScriptedBrain
from .human import HumanChannel
from .scripts import invoice_verifier, invoice_worker
from .trace import Trace
from simworld.credentials import load_or_create

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIM_URL = os.environ.get("SIM_URL", "http://127.0.0.1:8100")


def sim_is_up(url: str) -> bool:
    try:
        return httpx.get(url + "/", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_sim(url: str = DEFAULT_SIM_URL) -> None:
    """Start SimWorld in a background thread if nothing is listening at `url`."""
    if sim_is_up(url):
        return
    import uvicorn
    from simworld.app import app

    u = httpx.URL(url)
    server = uvicorn.Server(uvicorn.Config(app, host=u.host, port=u.port or 80, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if sim_is_up(url):
            return
        time.sleep(0.1)
    raise RuntimeError(f"SimWorld did not start at {url}")


def reset_sim(url: str = DEFAULT_SIM_URL) -> None:
    httpx.post(url + "/__admin/reset", timeout=5)


def environment(sim_url: str = DEFAULT_SIM_URL, approval_mode: str = "threshold") -> Environment:
    load_or_create(ROOT / "workspace" / "vault.json")  # generate local demo credentials on first run
    return Environment(sim_url=sim_url, workspace=ROOT / "workspace", runs_dir=ROOT / "runs",
                       approval_mode=approval_mode)


def build_worker(env: Environment, human: HumanChannel, offline: bool, verify: bool = True,
                 trace: Trace | None = None) -> TaskWorker:
    if offline:
        brain, verifier = ScriptedBrain(invoice_worker), ScriptedBrain(invoice_verifier)
    else:
        brain, verifier = LLMBrain(), LLMBrain(effort="medium")
    return TaskWorker(env, brain, human, verifier_brain=verifier if verify else None, verify=verify, trace=trace)


def new_trace(env: Environment) -> Trace:
    return Trace(env.runs_dir, secrets=env.vault().secret_values())


