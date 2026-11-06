"""Ingest the snapshots once and answer questions from the command line.

    python tools/dev_run.py "What is the 6-month CD rate?" "What was it in May 2026?@2026-05-31"

(`question@YYYY-MM-DD` passes an as_of date.)  Run from the repository root.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "submission"))

from run_eval import load_snapshots  # noqa: E402


def load(snapshots: Path = ROOT / "snapshots"):
    import solution
    ks = solution.build()
    t = time.time()
    for s in load_snapshots(snapshots):
        ks.ingest(s)
    print(f"ingest {time.time() - t:.1f}s", file=sys.stderr)
    return ks


if __name__ == "__main__":
    ks = load()
    for q in sys.argv[1:]:
        as_of = None
        if "@" in q:
            q, as_of = q.split("@")
        t = time.time()
        a = ks.answer(q, as_of)
        print(f"\nQ: {q}  [{time.time() - t:.2f}s] confident={a.confident}\n{a.text}")
        print(f"  sources={[s.url for s in a.sources]}")
        if a.conflict_note:
            print(f"  conflict_note={a.conflict_note}")
