"""Run trace: every thought, action, observation, retry and decision, with evidence files.

Written as JSONL as it happens (so a crashed run still leaves a record) and fanned out to
listeners (the web console streams it live). Secret values are redacted before anything
is stored or shown.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class Trace:
    def __init__(self, runs_dir: Path, secrets: list[str] | None = None, run_id: str | None = None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.dir = runs_dir / self.run_id
        (self.dir / "evidence").mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in (secrets or []) if len(s) >= 4]
        self.events: list[dict] = []
        self.listeners: list[Callable[[dict], None]] = []
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._evidence_n = 0

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for s in self.secrets:
                obj = obj.replace(s, "••••••")
            return obj
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact(v) for v in obj]
        return obj

    def emit(self, type_: str, **data) -> dict:
        with self._lock:
            event = {"seq": len(self.events), "t": round(time.time() - self.t0, 2), "type": type_,
                     **self.redact(data)}
            self.events.append(event)
            with open(self.dir / "trace.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                pass
        return event

    def for_actor(self, actor: str) -> "ActorTrace":
        return ActorTrace(self, actor)

    def save_evidence(self, label: str, url: str, snapshot: str, html: str, actor: str = "worker") -> str:
        with self._lock:
            self._evidence_n += 1
            eid = f"E{self._evidence_n}"
        safe = "".join(c if c.isalnum() else "-" for c in label.lower())[:40]
        base = self.dir / "evidence" / f"{eid}-{safe}"
        base.with_suffix(".html").write_text(self.redact(html), encoding="utf-8")
        base.with_suffix(".txt").write_text(self.redact(snapshot), encoding="utf-8")
        self.emit("evidence", actor=actor, id=eid, label=label, url=url, file=f"evidence/{base.name}.html",
                  snapshot=self.redact(snapshot)[:3000])
        return eid


class ActorTrace:
    """Tags every event with who produced it (worker or verifier)."""

    def __init__(self, trace: Trace, actor: str):
        self.trace, self.actor = trace, actor

    def emit(self, type_: str, **data) -> dict:
        return self.trace.emit(type_, actor=self.actor, **data)

    def save_evidence(self, label: str, url: str, snapshot: str, html: str) -> str:
        return self.trace.save_evidence(label, url, snapshot, html, actor=self.actor)
