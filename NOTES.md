# NOTES: a knowledge system that knows what's true now

**API keys: none.** The system needs no network access and no paid API. It uses only the Python 3.11 standard library, so `requirements.txt` is empty. Ingesting all five snapshots takes 4–12 seconds depending on the machine (the limit is 30 minutes). A typical answer takes 10–400 ms, and every answer is deterministic.

| Eval (official `harness/run_eval.py`, Python 3.11) | Questions | Final score | Stale-trap hits | p95 latency |
|---|---|---|---|---|
| `eval/public_eval.jsonl` (the 40 practice questions) | 40 | **0.892** | 0 | 0.38 s |
| `eval/paraphrase_eval.jsonl` (my rewordings of the practice questions) | 56 | **0.984** | 0 | 0.42 s |
| `eval/heldout_eval.jsonl` (my own new questions, checked against the snapshots by hand) | 87 | **0.992** | 0 | 0.29 s |
| `eval/synthetic_eval.jsonl` (generated from the official rate sheets, see below) | 348 | **0.986** | 0 | 0.11 s |
| `harness/selftest` (the harness's toy credit union, a *different* site), including `conflicts()` (F1 1.0) | 5 | **1.000** | 0 | – |

Repeat consistency is 1.0 on every set. There are 19 unit tests (`python -m unittest discover -s tests`), and they need no snapshots.

The starter scored 0.093. I wrote the held-out and paraphrase sets after reading the practice set, so that tuning against 40 questions wouldn't be mistaken for generalisation. Many fixes came from misses in those sets, not from the practice questions.

**The synthetic set** (`tools/make_synthetic_eval.py`) is a larger stand-in for the hidden set. It doesn't import the solution: it reads the official rate sheets of all five snapshots with its own small table reader, finds 122 rate facts, 25 of which changed between crawls, and generates:
- 2–3 phrasings per fact, scored as paraphrase groups
- trap values from other tiers and from older snapshots, 60 stale-trap questions in all
- 40 as-of questions
- 30 refusal questions: products and terms the site doesn't publish, plus random out-of-scope questions (weather, "my balance", rate predictions, other banks)
- prose facts and casually typed questions ("yo whats the 12 mo cd apy")

It's about three-quarters rate questions, so its 0.986 is an upper bound. The practice set is the more realistic signal.

## How it works

```
ingest(snapshot)                       answer(question, as_of)
  │ parse each page body once (cache)    │ parse question: intent, qualifiers, answer type, date
  │   tables → rows with caption,        │ pick the snapshot for as_of (or a month named in the question)
  │     header, "effective as of" date   │ ├─ rate / fee → table rows ──► stale tables dropped ──► tier pick
  │   "Label: $X" lines → rows too       │ ├─ link      → anchors + page titles, weighted by authority
  │   prose → passages, links, dates     │ └─ facts     → passages ──► newest statement ──► sentence pick
  │ classify page → authority            │ refuse when the subject isn't on the site
  │ BM25 indexes per snapshot            ▼
  │ precompute conflicts                 text + sources + confident + conflict_note
```

The code is in `submission/knowledge/`: `parse.py`, `store.py`, `query.py`, `engine.py` and `conflicts.py`.

**Everything is a row.** Rates and fees are table rows. Each row keeps the caption above its table ("Special Certificates"), its column headers and the "Rates are effective as of MM/DD/YYYY" line printed under it. One-line facts such as "6-month certificate APY: 3.50%" or "Domestic wire $25.00 each" also become rows. That way all rows get the same handling for tiers, staleness and conflicts.

**Qualifiers are filters, not keywords.** Each question is parsed for qualifiers:
- term in months or years ("five-year CD" → 60 months)
- vehicle model year (2025 falls in the "2024-27 Model Year" range)
- balance or loan amount (picks the narrowest tier that contains it)
- flags: jumbo, IRA or Roth/Traditional, business vs personal, refinance, intro rate
- measure asked for: APY, dividend rate, APR or minimum

A row whose term or range contradicts the question is discarded rather than down-ranked. When the question names a measure, only that column is shown, so "What's the APY?" never prints the dividend rate next to it. List questions return every row of the best table, plus matching tables on other authoritative pages. For example, "What credit cards do you offer?" also returns the business cards.

## How I decide what is current

1. **The snapshot.** Without `as_of`, the system answers from the newest snapshot. With `as_of`, it uses the latest snapshot taken on or before that date. A month named in the question ("in May 2026") counts as an `as_of`. A page missing from a crawl is carried forward from the last crawl that had it, at reduced authority. The harness's own self-test expects this.
2. **Within a snapshot, a stale copy loses to its source.** Two tables describe the same product when their row labels match with the numbers masked out: "2023-26 Model Year | 60 Months" matches "2024-27 Model Year | 60 Months". When that happens, one table supersedes the other:
   - If the authority gap is large, the more authoritative page wins. The authority order is: rate sheet (`/rates`, `/business-rates`) > product page > PDF document > landing/promo page = press release > blog > calculator > tag/listing page.
   - Otherwise the table with the newer "effective as of" date wins.

   Superseded tables are never used to answer. This is how the calculator's 5.34% loses to the rate sheet's 5.39%, and why `homebuying-guidance` (effective 04/22) can't override `/rates` (effective 09/25).
3. **Expired promotions are not current.** Text such as "offer ended 2025-09-30", where the date is earlier than the snapshot date, removes that row or passage.
4. **Repeated press-release facts use the newest dated statement.** The "About Dupaco" boilerplate says 19, 20, 23, 24 or 22 branches depending on the year. The system reads each article's date and answers from the newest one, which says 22. The same rule turns "fourth time Forbes recognized Dupaco" into "fifth". For "how many" questions, a count that the site repeats across many pages beats a one-off number in a blog post, such as the 100,000 branches nationwide.
5. **The newest article in a series wins.** When several dated articles on the same topic match about equally, for example the three "Forbes ranks Dupaco" releases, the most recent one answers.

## Conflicts

`conflicts()` reports three kinds of contradiction, each with the competing values, the winner and the reason. In total it reports 53.

- **Stale copies in the newest snapshot.** Each stale row is paired with the authoritative row it copies, using the same masked label, the same term and the closest model-year range or amount. Example: "60 Months auto APR: 5.34% on `/switch-and-earn` and the calculator; 5.39% on `/rates` (rate sheet outranks landing page; effective 08/31/2026 is newer than 01/01/2026)."
- **Changes across snapshots** in official tables. Example: "10 Month Fixed Jumbo Certificate Special APY: 3.90% → 4.00%; newest snapshot is current." Rows are tracked across crawls even when the site's markdown formatting changed. A label that has two different values in the same crawl is ambiguous and is not reported.
- **Drifting press-release facts**: branches, counties, members, assets, employees and "Nth time Forbes…". The newest article wins.

Tiers (balance, term, loan amount), rate vs APY and personal vs business are separate facts and never count as conflicts, because rows are only compared when their labels match. When an answer uses a row that has a known stale copy, `conflict_note` says which page is out of date and why.

## Saying "I don't know"

**First, is this something a website could answer at all?** This check runs before any search, and it refuses:
- predictions ("will rates go up next month?", a future date phrased as a forecast)
- one member's own account ("my balance", "why was my card declined")
- live market prices (stock or Bitcoin price)
- anything unrelated to a credit union

A question counts as related if it contains any of these:
- a generic banking word (rate, loan, fee, account, branch, routing…); this short hand-written list describes banking, not this site
- a topic word **learned at ingest** from the titles, headings and table captions of the site's own pages, not blogs or news. A new product page brings its own name in automatically, and a unit test checks that a made-up "Pawprint Pet Protection" page is answerable with no code change.
- the site's own name, learned as the word in most page titles
- a rare name the site uses (Galena, Hy-Vee, Forbes)

Time words ("tomorrow") and words found everywhere ("Dubuque" is in every dateline) don't count.

**Then, can the answer be grounded?** The system refuses when it can't ground the subject of the question:
- A rate or fee question whose product words don't match any table row: the 7/1 ARM, construction loans, a 9-month CD, a "cryptocurrency savings account". Coverage is idf-weighted, so the most specific word counts most. Gerunds and filler ("I'm thinking about buying…") are ignored.
- A named place or thing that appears nowhere on the site, such as a branch that doesn't exist.
- An hours, address or phone question with no passage of that shape about that subject. This is how the Mullan Avenue branch is refused: it is mentioned, but no hours are published for it.

The refusal text contains no numbers, so it can't trip a stale-value trap. Misspelled words are first matched to the closest site word ("certficate" → certificate). A word that doesn't match anything stays unknown, and a rate question then can't be answered.

## Prose answers

Prose answers come from BM25 over passages. The ranking adds idf-weighted coverage of the question's subject, page authority, page title and heading matches, and the expected answer type (hours → times, when → dates, how many → a count next to the noun, routing → 9 digits, how much → $). It also gives a small boost to a news article's lead paragraph and penalises navigation blocks, app views and personal/business mismatches. The answer is then built from the best sentence on the best page:
- When a matching FAQ question is found, the sentence after it is the answer.
- A sentence starting "This…" or "It…" brings the sentence before it.
- "For more information…" call-to-action lines are down-weighted.
- General hours questions get two or three typical schedules and a "which branch?" follow-up.

Contract fine print (terms, conditions, agreements, disclosures) ranks below member-facing pages for "how do I…" questions.

## Malformed input

Bad input never crashes a run or corrupts later answers; `tests/test_engine.py` covers each case:
- **Ingest:**
  - A missing or invalid `taken_on` is recovered from the snapshot id ("01_2026-05-12_…"), then from the pages' fetch dates, then by placing the snapshot after the previous one.
  - A page that fails to parse is skipped, not the whole snapshot.
  - Duplicate URLs keep the last copy, and re-ingesting a snapshot id replaces it.
  - The new index is built completely before any state changes.
- **`answer()`:**
  - `None`, numbers or bytes are turned into text; an empty question gets "I don't know".
  - Very long questions are cut to 1,000 characters to stay within the speed budget.
  - ALL-CAPS questions skip the proper-name check, so one typo doesn't cause a refusal.
- **`as_of`:** accepted as ISO dates, ISO datetimes, "05/31/2026", "May 31, 2026", `date` or `datetime` objects, or `20260531`. Anything unparseable falls back to the newest snapshot.
- **Before any ingest:** `answer()` refuses and `conflicts()` returns an empty list.

## Trade-offs and decisions

- **No LLM.** Speed and consistency are 30% of the score and both are multiplied by accuracy. Every answer here is deterministic and fast, can be reproduced, and cites exactly where it came from. The cost is phrasing. Answers are extracted lines, not fluent prose, and very open questions get the best matching passage, not a synthesis. I didn't have an API key to test with, so I built nothing I couldn't measure.
- **Quote, then trim.** Rates are printed from rows with only the asked-for columns. Prose is cut to one to three sentences. Repeating an old or other-tier number scores zero, so short answers are safer.
- **Determinism.** No randomness or time-dependence: ties break on URL and line number, and answers are cached per normalised question. Repeat consistency is 1.0 on every set.

## Known limitations, including where I disagree with the practice key

- `dup-005` has an empty `must_include` and isn't a refusal, so the scorer gives it 0 for any answer without the optional LLM judge. My answer lists every certificate rate, which is what the expected answer asks for.
- `dup-088` ("employees who used VTO last year") expects 294, which is the 2023 figure from an April 2024 release. The site also has an April 2025 release saying 256 for 2024. My system answers with the newer figure, on purpose.
- `dup-081` (eligibility) expects the three states. My answer comes from the join page's opening paragraph, which describes the $25 deposit and doesn't name the states. That's a retrieval miss.
- `dup-060` (updating contact info) expects the main phone number. No current page pairs updating your information with that number. The answer now points to updating it in Shine, which is reasonable but doesn't score.
- The hand-written vocabulary in `text.py` (synonyms such as CD→certificate and mortgage→home, plus the generic-word lists) is small and specific to banking. A larger hidden set will surely contain phrasings it doesn't cover.
- The out-of-scope patterns (predictions, personal-account questions) are hand-written, so some phrasings will slip past them.
- Questions in other languages aren't supported. They get the closest English match rather than a refusal.

## What I'd do next

- Add an optional LLM layer that rewrites the extracted evidence into a fluent answer and is only allowed to use numbers from that evidence, with the extracted text kept as the fallback. This would improve phrasing without losing grounding.
- Add an embedding retriever next to BM25 for paraphrases that the synonym list misses.

## Running it

```bash
cp -r /path/to/participant-kit/snapshots_zip .     # snapshots are not in this repo
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
python -m unittest discover -s tests -v              # no snapshots needed
python tools/eval_report.py eval/heldout_eval.jsonl  # every miss, with the answer text
python tools/dump_conflicts.py                       # everything conflicts() reports
python tools/make_synthetic_eval.py > eval/synthetic_eval.jsonl   # regenerate the synthetic set
python tools/dev_run.py "What's the 6-month CD rate?" "And in May?@2026-05-31"
```