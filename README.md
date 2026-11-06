# Knowledge Hackathon: what's true *now*

My submission for the interface.ai build assignment. It answers credit-union member questions from five crawls of a website. It gives the current, correctly tiered value with a source, says "I don't know" when the site doesn't say, and lists where the site contradicts itself.

- **Submission folder:** `submission/` (`solution.py` → `build()`)
- **Approach, rules for "current", trade-offs:** [NOTES.md](NOTES.md)
- **API keys:** none. Standard library only (Python 3.11+).

```bash
# 1. put the participant kit's snapshot zips next to this file (they are not redistributed here)
cp -r /path/to/hackathon-knowledge-kit-participant/snapshots_zip .

# 2. score it with the provided harness (unzips the snapshots into ./snapshots on first run)
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl

# 3. unit tests (no snapshots needed)
python -m unittest discover -s tests -v
```

| Path | What |
|---|---|
| `submission/solution.py` | contract adapter: `ingest` / `answer` / `conflicts` |
| `submission/knowledge/parse.py` | markdown → table rows (caption, effective date), key-value facts, passages, links, article date |
| `submission/knowledge/store.py` | page authority classes, per-snapshot BM25 indexes |
| `submission/knowledge/query.py` | intent, term/model-year/balance qualifiers, answer type, dates |
| `submission/knowledge/engine.py` | answering: stale-table superseding, tier selection, prose and link answers, refusals |
| `submission/knowledge/conflicts.py` | stale copies, changes across snapshots, drifting press-release facts |
| `eval/` | the 40 practice questions plus my paraphrase set and held-out set |
| `tools/` | `eval_report.py` (shows every miss), `dump_conflicts.py`, `dev_run.py`, `debug_passages.py` |
| `harness/` | the organisers' scorer, unchanged |
