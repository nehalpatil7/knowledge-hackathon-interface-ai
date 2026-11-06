"""Show the top-scored prose candidates for a question (why did it pick that paragraph?).

    python tools/debug_passages.py "How many branches does Dupaco have?"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dev_run import load  # noqa: E402
from knowledge.query import parse_question
ks = load(); e = ks.engine
for qs in sys.argv[1:]:
    q = parse_question(qs); snap = e.snapshot_for(None)
    print("\nQ:", qs, "| intent", q.intent, q.answer_type, q.count_noun, "| subj", q.subject, "| proper", q.proper)
    for s, cov, ok, u in e.passage_candidates(q, snap)[:8]:
        print(f"  {s:.2f} cov={cov:.2f} ok={ok} {u.page.category} {u.page.url[22:80]} :: {u.obj.text[:140]}")
