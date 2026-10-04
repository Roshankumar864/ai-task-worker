"""Two kinds of memory.

WorkingMemory - facts discovered during a run (invoice amount, record IDs...), each with
its source, so the final summary can cite where every value came from.

LessonStore - short procedural lessons that persist across runs ("Ledgerly wants dates
as YYYY-MM-DD"). They are offered to future runs so the worker stops repeating mistakes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Fact:
    key: str
    value: str
    source: str
    step: int


class WorkingMemory:
    def __init__(self):
        self.facts: dict[str, Fact] = {}

    def remember(self, key: str, value: str, source: str, step: int) -> Fact:
        self.facts[key] = Fact(key, value, source, step)
        return self.facts[key]

    def render(self) -> str:
        if not self.facts:
            return "(working memory is empty)"
        return "\n".join(f"- {f.key} = {f.value}  [source: {f.source}]" for f in self.facts.values())

    def as_dict(self) -> dict:
        return {k: asdict(f) for k, f in self.facts.items()}


class LessonStore:
    MAX = 20

    def __init__(self, path: Path):
        self.path = path
        self.lessons: list[str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def add(self, lessons: list[str]) -> list[str]:
        added = []
        for lesson in lessons:
            lesson = lesson.strip()
            if lesson and lesson.lower() not in {x.lower() for x in self.lessons}:
                self.lessons.append(lesson)
                added.append(lesson)
        self.lessons = self.lessons[-self.MAX:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.lessons, indent=2), encoding="utf-8")
        return added
