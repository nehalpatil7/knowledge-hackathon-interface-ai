"""Submission entry point: build() -> object with ingest / answer / conflicts.

See NOTES.md for how the system decides what is current. No API keys, no network,
standard library only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_harness = next((d / "harness" for d in HERE.parents if (d / "harness" / "contract.py").exists()), None)
if _harness:
    sys.path.insert(0, str(_harness))

try:
    from contract import Answer, Conflict, Source  # the scorer's own dataclasses
except ImportError:  # pragma: no cover - lets the package run without the harness on the path
    from dataclasses import dataclass, field

    @dataclass
    class Source:
        url: str
        snapshot_id: Optional[str] = None
        quote: Optional[str] = None

    @dataclass
    class Answer:
        text: str
        sources: list = field(default_factory=list)
        confident: bool = True
        conflict_note: Optional[str] = None

    @dataclass
    class Conflict:
        topic: str
        values: list
        sources: list
        resolution: Optional[str] = None
        reason: Optional[str] = None

from knowledge.engine import KnowledgeEngine  # noqa: E402


class DupacoKnowledge:
    def __init__(self):
        self.engine = KnowledgeEngine()

    def ingest(self, snapshot) -> None:
        self.engine.ingest(snapshot.snapshot_id, snapshot.taken_on, snapshot.pages)

    def answer(self, question: str, as_of: Optional[str] = None):
        r = self.engine.answer(question, as_of)
        return Answer(text=r.text,
                      sources=[Source(url=e.url, snapshot_id=e.snapshot_id, quote=e.quote or None) for e in r.evidence],
                      confident=r.confident, conflict_note=r.conflict_note)

    def conflicts(self):
        out = []
        try:
            found = self.engine.conflicts()
        except Exception:
            found = []
        for c in found:
            out.append(Conflict(topic=c["topic"], values=c["values"],
                                sources=[Source(url=u, snapshot_id=s) for u, s in c["sources"]],
                                resolution=c.get("resolution"), reason=c.get("reason")))
        return out


def build():
    return DupacoKnowledge()