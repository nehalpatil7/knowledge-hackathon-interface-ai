"""Print every contradiction conflicts() reports (topic, competing values, winner, reason).

    python tools/dump_conflicts.py [--json out.json]
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dev_run import load  # noqa: E402

ks = load()
cs = ks.conflicts()
for c in cs:
    print(f"- {c.topic}\n    values: {c.values}  ->  {c.resolution}\n    why: {c.reason}")
print(f"\n{len(cs)} conflicts")
if "--json" in sys.argv:
    out = sys.argv[sys.argv.index("--json") + 1]
    Path(out).write_text(json.dumps([asdict(c) for c in cs], indent=2))
