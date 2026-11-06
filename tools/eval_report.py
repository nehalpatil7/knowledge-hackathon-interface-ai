"""Score an eval file with the official item scorer and print every miss with the answer text.

    python tools/eval_report.py eval/heldout_eval.jsonl [-v]

Run from the repository root (expects ./snapshots or ./snapshots_zip).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dev_run import load  # noqa: E402  (also puts harness/ and submission/ on the path)
from run_eval import to_answer  # noqa: E402
from scoring import score_item  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "eval/public_eval.jsonl"
verbose = "-v" in sys.argv
ks = load()
items = [json.loads(l) for l in open(path) if l.strip()]
tot = wsum = 0
for it in items:
    t = time.time()
    a = to_answer(ks.answer(it["question"], it.get("as_of")))
    dt = time.time() - t
    r = score_item(it, a)
    tot += r["score"] * r["weight"]; wsum += r["weight"]
    bad = r["score"] < 0.999 and not (r["correctness"] >= 0.999 and r["source_hit"] == 0.0 and not verbose)
    if bad or verbose:
        print(f"\n[{it['id']}] {it['category']} score={r['score']} corr={r['correctness']} src={r['source_hit']} {dt:.2f}s")
        print("Q:", it["question"], "| as_of", it.get("as_of"))
        print("EXPECT:", it.get("expected_answer"), "| must", it.get("must_include"), "| not", it.get("must_not_include"))
        print("GOT:", a.text[:700].replace("\n", " / "), "| conf", a.confident)
        print("SRC:", [s.url for s in a.sources][:4], "| want", it.get("expected_sources"))
        if r["trap_hits"]: print("TRAP:", r["trap_hits"])
print(f"\nQA score {tot/wsum:.3f}  ({len(items)} items)")
