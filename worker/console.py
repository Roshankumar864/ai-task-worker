"""Web console: submit a task, watch the worker live, answer its questions and approvals."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import runtime
from .human import QueueHuman

app = FastAPI(title="AI Task Worker console")
RUNS: dict[str, dict] = {}
_runs_dir = runtime.ROOT / "runs"
_runs_dir.mkdir(exist_ok=True)
app.mount("/runs", StaticFiles(directory=_runs_dir), name="runs")


class RunRequest(BaseModel):
    task: str
    offline: bool = False
    approval_mode: str = "threshold"
    reset: bool = False


class Answer(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "console.html").read_text(encoding="utf-8").replace(
        "__SIM_URL__", runtime.DEFAULT_SIM_URL)


@app.post("/api/runs")
def start_run(req: RunRequest):
    if req.reset:
        runtime.reset_sim()
    env = runtime.environment(approval_mode=req.approval_mode)
    trace = runtime.new_trace(env)
    human = QueueHuman(notify=lambda r: None)
    worker = runtime.build_worker(env, human, offline=req.offline, trace=trace)
    RUNS[trace.run_id] = {"trace": trace, "human": human, "done": False}

    def go():
        try:
            worker.run(req.task)
        except Exception as e:  # surface crashes in the UI instead of hanging the stream
            trace.emit("error", actor="worker", error=f"{type(e).__name__}: {e}")
            trace.emit("run_finished", actor="worker", status="failed", verified=None, summary=str(e),
                       results={}, evidence=[], steps=0)
        finally:
            RUNS[trace.run_id]["done"] = True

    threading.Thread(target=go, daemon=True).start()
    return {"run_id": trace.run_id}


@app.get("/api/runs/{run_id}/events")
def stream(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404)

    def gen():
        i = 0
        while True:
            events = run["trace"].events
            while i < len(events):
                yield f"data: {json.dumps(events[i], default=str)}\n\n"
                i += 1
            if run["done"] and i >= len(run["trace"].events):
                yield "event: end\ndata: {}\n\n"
                return
            time.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/answer")
def answer(run_id: str, body: Answer):
    run = RUNS.get(run_id)
    if not run or run["human"].pending is None:
        raise HTTPException(409, "No question is pending for this run.")
    run["human"].answer(body.text)
    return {"ok": True}


@app.post("/api/reset")
def reset():
    runtime.reset_sim()
    return {"ok": True}
