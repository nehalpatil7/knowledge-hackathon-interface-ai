"""The knowledge system: ingest snapshots, answer questions, report contradictions."""
from __future__ import annotations

import difflib
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .parse import TableRow, expired_on
from .query import Query, parse_question
from .store import AUTHORITY, PageCache, PageRec, SnapshotIndex, Unit, classify, iso_date
from .text import DOMAIN, SYN, tokens

YESNO_RE = re.compile(r"\s*(do|does|did|is|are|can|will|should|must|am)\b(?!.*\b(what|how much|which)\b)", re.I)

REFUSAL = ("I'm sorry, I couldn't find that on the Dupaco website, so I don't want to guess. "
           "It isn't listed; please contact Dupaco directly and a team member can help.")

PASSAGE_AUTH = {"rate_sheet": 1.0, "product": 1.0, "site": 0.95, "document": 0.8, "landing": 0.8,
                "news": 0.85, "blog": 0.75, "calculator": 0.45, "legal": 0.6, "index": 0.25}


# ============================================================================ result types

@dataclass
class Evidence:
    url: str
    snapshot_id: str
    quote: str


@dataclass
class Result:
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    confident: bool = True
    conflict_note: Optional[str] = None
    coverage: float = 1.0          # share of the question's subject words the evidence names


# ============================================================================ row helpers

RANGE_RE = re.compile(r"\$?\s?([\d,]+(?:\.\d+)?)\s*(?:–|-|to)\s*\$?\s?([\d,]+(?:\.\d+)?)")
PLUS_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*\+")
LESS_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*or less")
MONTHS_RE = re.compile(r"\b(\d{1,3})[\s-]*(?:months?|mo)\b", re.I)
YEARS_RE = re.compile(r"\b(\d{1,2})[\s-]*year\b", re.I)
MODEL_RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s*[-–]\s*(\d{2,4})\b")
MODEL_OLDER_RE = re.compile(r"\b((?:19|20)\d\d)\s*model year\s*\(?and older", re.I)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def row_text(r: TableRow) -> str:
    return " | ".join(r.label_cells())


def row_terms(r: TableRow) -> tuple[set[int], set[int]]:
    lab = row_text(r) + " " + r.caption
    return ({int(m) for m in MONTHS_RE.findall(lab)}, {int(m) for m in YEARS_RE.findall(lab)})


def row_model_years(r: TableRow) -> Optional[tuple[int, int]]:
    lab = row_text(r)
    if "model" not in lab.lower():
        return None
    m = MODEL_OLDER_RE.search(lab)
    if m:
        return (1900, int(m.group(1)))
    m = MODEL_RANGE_RE.search(lab)
    if m:
        a = int(m.group(1))
        b = m.group(2)
        b = int(b) if len(b) == 4 else (a // 100) * 100 + int(b)
        return (min(a, b), max(a, b))
    m = re.search(r"\b((?:19|20)\d\d)\b", lab)
    if m:
        return (int(m.group(1)), int(m.group(1)))
    return None


def row_amount_range(r: TableRow) -> Optional[tuple[float, float]]:
    lab = row_text(r)
    m = RANGE_RE.search(lab)
    if m and "$" in lab:
        a, b = _f(m.group(1)), m.group(2)
        bv = _f(b)
        if bv < a and len(b.replace(",", "")) <= 3:     # "$10,000-24,999" style already handled; guard odd cases
            bv = a
        return (a, bv)
    m = PLUS_RE.search(lab)
    if m:
        return (_f(m.group(1)), float("inf"))
    m = LESS_RE.search(lab)
    if m:
        return (0.0, _f(m.group(1)))
    return None


def primary_value(r: TableRow) -> Optional[float]:
    """The headline number of a row: APY if present, else APR / rate, else first %."""
    vals = r.value_cells()
    order = [lambda h: "yield" in h.lower() or "apy" in h.lower(),
             lambda h: "apr" in h.lower() or "annual percentage rate" in h.lower(),
             lambda h: "rate" in h.lower()]
    for pick in order:
        for h, v in vals:
            if pick(h):
                m = PCT_RE.search(v)
                if m:
                    return float(m.group(1))
    for _, v in vals:
        m = PCT_RE.search(v)
        if m:
            return float(m.group(1))
    return None


def short_header(h: str) -> str:
    hl = h.lower()
    if hl in ("value", "col2", "col3", "col4", ""):
        return ""
    if "yield" in hl or "apy" in hl:
        return "APY"
    if "annual percentage rate" in hl or hl.startswith("apr"):
        return "APR" + (" (as low as)" if "as low as" in hl else "")
    if "dividend" in hl:
        return "dividend rate"
    return h.strip().rstrip(":")


KEEP_VALUE = re.compile(r"rate|apr|apy|yield|fee|amount|cost|charge|price|limit", re.I)
DROP_VALUE = re.compile(r"payment|points|min\.? to open", re.I)


def fmt_value(h: str, v: str) -> str:
    sh = short_header(h)
    v = v.strip()
    if sh.startswith("APR") and v.upper().endswith("APR"):
        v = v[:-3].strip()
    if sh == "APY" and v.upper().endswith("APY"):
        v = v[:-3].strip()
    if sh in ("APY", "dividend rate") or sh.startswith("APR"):
        return f"{v} {sh}"
    return f"{sh}: {v}" if sh else v


def focus_columns(r: TableRow, words: list[str]) -> Optional[set[str]]:
    """Wide 'comparison chart' tables put attributes in the header ('Late Payment Fee', 'Cash Advance Fee').
    When the question names one of them, show just that column."""
    vals = r.value_cells()
    if len(vals) < 3 or not words:
        return None
    score = {h: len(set(tokens(h)) & set(words)) for h, _ in vals}
    best = max(score.values())
    return {h for h, v in score.items() if v == best} if best > 0 else None


def fmt_row(r: TableRow, measure: Optional[str] = None, focus: Optional[set[str]] = None) -> str:
    """One table row as a short line. `measure` (apy | dividend | apr) keeps only the column the member
    asked about, so 'What's the APY?' isn't answered with the dividend rate next to it."""
    labels = [c for c in r.label_cells() if c and c not in ("—", "-", "N/A")]
    prod = r.caption.split(" / ")[-1].strip() if r.caption else ""
    label = " — ".join(dict.fromkeys(labels))
    if prod and prod.lower() not in label.lower() and not any(l.lower() in prod.lower() for l in labels[:1]):
        label = f"{prod}: {label}" if label else prod
    cells = [(h, v) for h, v in r.value_cells()
             if v and v not in ("—", "-", "N/A", "None") and (
                 (KEEP_VALUE.search(h) and not DROP_VALUE.search(h)) or (measure == "min" and "min" in h.lower()))]
    if measure:
        only = [(h, v) for h, v in cells if short_header(h).lower().startswith(measure)]
        if only:
            cells = only
    if focus:
        cells = [(h, v) for h, v in r.value_cells() if h in focus and v] or cells
    vals = [fmt_value(h, v) for h, v in cells]
    if not vals:
        vals = [fmt_value(h, v) for h, v in r.value_cells() if v and v not in ("—", "N/A") and not DROP_VALUE.search(h)]
    return f"{label}: {', '.join(vals)}" if vals else label


def norm_label(r: TableRow) -> str:
    s = row_text(r).lower().replace("–", "-").replace(".00", "")
    s = re.sub(r"\s+", " ", s)
    return s


# ============================================================================ the system

class KnowledgeEngine:
    def __init__(self):
        self.snaps: list[SnapshotIndex] = []
        self.cache = PageCache()
        self._last_seen: dict[str, PageRec] = {}
        self._answers: dict[tuple, Result] = {}
        self._conflicts = None

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot_id: str, taken_on: str, pages) -> None:
        recs = []
        for p in pages:
            cat = classify(p.url)
            parsed = self.cache.get(p.markdown or "", p.title or "")
            rec = PageRec(url=p.url, title=p.title or "", category=cat, authority=AUTHORITY[cat],
                          fetched=iso_date(p.fetched_at), parsed=parsed)
            recs.append(rec)
        # A page that wasn't re-crawled this time is still part of what we know (its newest copy), but it
        # ranks below pages seen in this crawl.
        present = {r.url for r in recs}
        for url, old in self._last_seen.items():
            if url not in present:
                recs.append(PageRec(url=old.url, title=old.title, category=old.category,
                                    authority=round(old.authority * 0.85, 3), fetched=old.fetched,
                                    parsed=old.parsed, carried=True))
        for r in recs:
            if not r.carried:
                self._last_seen[r.url] = r
        self.snaps.append(SnapshotIndex(snapshot_id, iso_date(taken_on), recs))
        self.snaps.sort(key=lambda s: s.taken_on)
        self._answers.clear()
        self._conflicts = None
        self.conflict_index()          # precompute (ingest is untimed; keeps answers fast)

    def snapshot_for(self, when: Optional[date]) -> SnapshotIndex:
        if when is None:
            return self.snaps[-1]
        best = None
        for s in self.snaps:
            if s.taken_on <= when:
                best = s
        return best or self.snaps[0]

    # ------------------------------------------------------------------ answer
    def answer(self, question: str, as_of: Optional[str] = None) -> Result:
        if not self.snaps:
            return Result(REFUSAL, confident=False)
        q = parse_question(question)
        when = iso_date(as_of) if as_of else q.asked_date
        snap = self.snapshot_for(when)
        q = self._spell_fix(q, snap)
        key = (" ".join(sorted(set(q.toks))), snap.snapshot_id)
        if key in self._answers:
            return self._answers[key]
        try:
            res = self._answer(q, snap, dated=when is not None, when_asked=when)
        except Exception:                      # never let one odd question break the run
            res = Result(REFUSAL, confident=False)
        self._answers[key] = res
        return res

    def _out_of_scope(self, q: Query, snap: SnapshotIndex, when_asked: Optional[date]) -> bool:
        """Questions no snapshot of a website can answer: predictions, one member's own account data,
        live market prices, dates after the newest snapshot asked as a forecast, and questions with nothing
        to do with a credit union - judged by generic banking words, topic words learned from the site's own
        page titles / headings / captions, and rare names the site uses."""
        if q.out_of_scope:
            return True
        if when_asked and when_asked > self.snaps[-1].taken_on and re.search(r"\b(will|be in|going to)\b", q.norm):
            return True
        if set(q.toks) & (DOMAIN | snap.site_names):
            return False                       # a banking concept, or the organisation itself
        rare = max(0.02 * len(snap.passages), 5)     # "specific": on < 2% of passages (at least 5 on tiny sites)
        if any(t in snap.topics and snap.passage_index.df(t) < rare for t in q.subject):
            return False                       # a product / topic the site has a page or section for
                                               # (specific ones only: "Dubuque" is in every dateline)
        # a rare name the site uses (a branch town, a program) also keeps the question in scope
        return not any(0 < snap.passage_index.df(t) < rare for t in q.proper)

    def _answer(self, q: Query, snap: SnapshotIndex, dated: bool, when_asked: Optional[date] = None) -> Result:
        if self._out_of_scope(q, snap, when_asked):
            return Result(REFUSAL, confident=False)
        res = None
        if q.intent == "link":
            res = self.answer_link(q, snap)
        elif q.intent in ("rate", "fee"):
            res = self.answer_table(q, snap, dated)
            if res is None and q.intent == "fee":
                res = self.answer_passage(q, snap)
            elif res is not None and res.coverage < 1.0 and YESNO_RE.match(q.raw):
                # "Do I need eStatements to earn dividends?" is a policy question that mentions a rate
                alt = self.answer_passage(q, snap)
                if alt is not None and alt.coverage >= 0.99:
                    res = alt
        else:
            res = self.answer_passage(q, snap)
        if res is None:
            return Result(REFUSAL, confident=False)
        if dated and res.confident:
            when = f"As of {snap.taken_on:%B %d, %Y}"
            if when_asked and when_asked < self.snaps[0].taken_on:
                when += " (the earliest copy of the website available)"
            res.text = f"{when}: " + res.text
        return res

    # ------------------------------------------------------------------ coverage
    @staticmethod
    def _covered(t: str, bag) -> bool:
        if t in bag:
            return True
        return any(s in bag for s in SYN.get(t, []))

    def _spell_fix(self, q: Query, snap: SnapshotIndex) -> Query:
        """Map words the site never uses to a close site word ('certficate' -> 'certificat').
        Words with no close match stay unknown: for a rate question that means we can't answer."""
        unknown = [t for t in q.required + q.subject
                   if len(t) >= 5 and not t.isdigit() and snap.vocab.get(t, 0) == 0 and t not in SYN]
        if not unknown:
            return q
        fixes = {}
        for t in dict.fromkeys(unknown):
            pool = [w for w in snap.vocab if w[:1] == t[:1] and abs(len(w) - len(t)) <= 2]
            m = difflib.get_close_matches(t, pool, n=1, cutoff=0.85)
            if m:
                fixes[t] = m[0]
        if fixes:
            q.required = [fixes.get(t, t) for t in q.required]
            q.subject = [fixes.get(t, t) for t in q.subject]
            q.expanded = q.expanded + [fixes[t] for t in fixes]
        q.unknown = [t for t in unknown if t not in fixes]
        return q

    def _required(self, q: Query, snap: SnapshotIndex) -> list[str]:
        # words the website never uses can't be required of an answer (unless they are names), and
        # -ing / -ed words only count when the rate tables themselves use them ("checking")
        rows = snap.row_index.post
        return [t for t in q.required
                if (snap.vocab.get(t, 0) > 0 or any(snap.vocab.get(s, 0) for s in SYN.get(t, [])))
                and (t not in q.verbish or t in rows)] + [t for t in q.unknown if t in q.required]

    def _unknown_names(self, q: Query, snap: SnapshotIndex) -> list[str]:
        """Capitalised words in the question that appear nowhere on the site (e.g. a branch that
        doesn't exist). Asking about them is a sign we should not answer."""
        return [t for t in q.proper if snap.vocab.get(t, 0) == 0]

    # ------------------------------------------------------------------ tables (rates, fees)
    def score_rows(self, q: Query, snap: SnapshotIndex) -> list[tuple[float, Unit]]:
        bm = snap.row_index.scores(q.expanded)
        if not bm:
            return []
        required = self._required(q, snap)
        idf = snap.row_index.idf
        pidf = snap.passage_index.idf
        def weight(t):
            w = max(idf.get(t, 0.0), max((idf.get(x, 0.0) for x in SYN.get(t, [])), default=0.0))
            return w if w > 0 else max(pidf.get(t, 0.0), 0.5)    # never in a rate table: very specific
        rw = [(t, weight(t)) for t in required]
        rw_total = sum(w for _, w in rw) or 1.0
        out = []
        for i, base in sorted(bm.items(), key=lambda kv: -kv[1])[:600]:
            u = snap.rows[i]
            r: TableRow = u.obj
            p = u.page
            if p.category in ("index",):
                continue
            ctx = set(u.ctx_toks) | set(tokens(p.parsed.h1 or p.title))
            lab = (row_text(r) + " " + r.caption).lower()
            # ---- what is being asked about must be named by the row / its table (idf-weighted)
            if required:
                cov = sum(w for t, w in rw if self._covered(t, ctx)) / rw_total
                if cov < (0.75 if q.intent == "fee" else 0.5):
                    continue                   # fee schedules list many similar items: be stricter
            else:
                cov = 1.0
            s = base * (0.25 + 0.75 * cov)
            vals = r.value_cells()
            if not any(re.search(r"\d", v) for _, v in vals):
                continue                       # form templates / empty tables
            exp = expired_on(" ".join(r.cells) + " " + r.caption)
            if exp and exp < snap.taken_on:
                continue                       # a promotion that has already ended
            if q.intent == "rate" and primary_value(r) is None:
                continue                       # a rate question needs a percentage
            if q.intent == "fee" and not any(re.search(r"\$|free", v, re.I) or (
                    re.search(r"\d\s*%", v) and not re.search(r"apr|apy|yield|rate|dividend", h + " " + v + " " + lab, re.I))
                    for h, v in vals) \
                    and not any(re.search(r"fee|charge|cost|min", h, re.I) for h, _ in vals):
                continue                       # a fee question needs an amount
            # ---- term
            rm, ry = row_terms(r)
            want_m = set(q.months) | {y * 12 for y in q.years}
            if want_m or q.years:
                if rm or ry:
                    if (rm & want_m) or (ry & q.years):
                        s *= 2.0
                    else:
                        continue
            # ---- model year
            if q.model_year:
                mr = row_model_years(r)
                if mr:
                    if mr[0] <= q.model_year <= mr[1]:
                        s *= 2.0
                    else:
                        continue
            # ---- balance / loan amount tier
            if q.amount is not None:
                ar = row_amount_range(r)
                if ar:
                    if ar[0] <= q.amount <= ar[1]:
                        s *= 2.0 / (1 + min(1.0, (ar[1] - ar[0]) / max(q.amount, 1) / 50))
                    else:
                        continue
            # ---- qualifiers that separate otherwise-identical rows
            s *= self._qualifier_factor(q, r, p, lab)
            s *= p.authority
            out.append((s, u))
        out.sort(key=lambda x: (-x[0], x[1].page.url, x[1].obj.line_no))
        return out

    @staticmethod
    def _qualifier_factor(q: Query, r: TableRow, p: PageRec, lab: str) -> float:
        f = 1.0
        pairs = [
            ("jumbo", "jumbo" in lab),
            ("ira", bool(re.search(r"\b(ira|roth|traditional)\b", lab))),
            ("business", p.is_business or "business" in lab),
            ("refinance", "refinanc" in lab),
            ("intro", bool(re.search(r"intro|balance transfer", lab))),
        ]
        for flag, row_has in pairs:
            asked = flag in q.flags
            if row_has and not asked:
                f *= 0.45 if flag != "business" else 0.6
            elif asked and not row_has:
                f *= 0.5
        if "roth" in q.flags and "traditional" in lab:
            f *= 0.3
        if "traditional" in q.flags and "roth" in lab:
            f *= 0.3
        if "special" in q.flags:
            f *= 1.3 if "special" in lab else 0.7
        if "standard" in q.flags and re.search(r"intro|balance transfer", lab):
            f *= 0.3
        if "arm" in q.flags and not re.search(r"adjustable|\barm\b|\d/1", lab):
            f *= 0.0
        return f

    def answer_table(self, q: Query, snap: SnapshotIndex, dated: bool = False) -> Optional[Result]:
        if self._unknown_names(q, snap):
            return None
        scored = self._drop_superseded(snap, self.score_rows(q, snap))
        if not scored:
            return None
        best_s, best = scored[0]
        if best_s <= 0:
            return None
        specific = bool(q.months or q.years or q.model_year or q.amount is not None
                        or {"highest", "lowest"} & q.flags or q.intent == "fee")
        by_table: dict[tuple, list[tuple[float, Unit]]] = defaultdict(list)
        for s, u in scored:
            by_table[(u.page.url, u.obj.table_id)].append((s, u))

        def tkey(u: Unit):
            return (u.page.url, u.obj.table_id)

        chosen: list[Unit] = []
        if specific:
            rows = [(s, u) for s, u in by_table[tkey(best)] if s >= (0.8 if q.intent == "fee" else 0.6) * best_s]
            if q.amount is not None:
                ranged = [(s, u, row_amount_range(u.obj)) for s, u in rows]
                widths = [r[1] - r[0] for _, _, r in ranged if r]
                if widths:
                    narrow = min(widths)
                    rows = [(s, u) for s, u, r in ranged if not r or (r[1] - r[0]) <= narrow * 1.0001]
            if {"highest", "lowest"} & q.flags and len(by_table[tkey(best)]) > 1:
                pool = [u for s, u in by_table[tkey(best)] if s >= 0.3 * best_s and primary_value(u.obj) is not None]
                if pool:
                    pick = (max if "highest" in q.flags else min)(pool, key=lambda u: (primary_value(u.obj), -u.obj.row_index))
                    rows = [(best_s, pick)]
            chosen = [u for _, u in sorted(rows, key=lambda x: x[1].obj.row_index)]
        else:
            # general / list question: every row of the best table, plus other tables that match the
            # product equally well on authoritative pages (e.g. personal AND business credit cards)
            tables = sorted(by_table.items(), key=lambda kv: -kv[1][0][0])
            seen_labels: set[str] = set()
            picked = 0
            base_rows = self._table_rows(snap, best)
            enumerate_all = "plural" in q.flags and "personal" not in q.flags and "business" not in q.flags
            for (url, tid), lst in tables:
                ts = lst[0][0]
                page = lst[0][1].page
                if enumerate_all and page.is_business:
                    ts /= 0.6            # "what cards do you offer?" covers business cards too
                if (url, tid) != tkey(best):
                    if ts < 0.7 * best_s or page.authority < 0.85 or picked >= 5:
                        continue
                rows = self._table_rows(snap, lst[0][1]) if (url, tid) != tkey(best) else base_rows
                new = [u for u in rows if norm_label(u.obj) + "|" + fmt_row(u.obj) not in seen_labels]
                if not new:
                    continue
                for u in new:
                    seen_labels.add(norm_label(u.obj) + "|" + fmt_row(u.obj))
                chosen.extend(new)
                picked += 1
            # drop rows the question clearly excludes (jumbo / IRA / refinance when not asked) - but only
            # if something is left
            def fac(u):
                f = self._qualifier_factor(q, u.obj, u.page, (row_text(u.obj) + " " + u.obj.caption).lower())
                if enumerate_all and (u.page.is_business or "business" in u.obj.caption.lower()):
                    f /= 0.6
                return f
            keep = [u for u in chosen if fac(u) >= 0.59]
            if keep:
                chosen = keep
        if not chosen:
            return None
        chosen = chosen[:24]
        measure = ("min" if re.search(r"\b(minimum|min\.?|to open|to earn)\b", q.norm) else
                   "apy" if re.search(r"\b(apy|yield)\b", q.norm) else
                   "dividend" if re.search(r"\bdividend", q.norm) else
                   "apr" if re.search(r"\bapr\b", q.norm) else None)
        words = [t for t in q.required if t not in ("fee", "rate", "apr", "apy")]
        lines = [fmt_row(u.obj, measure, focus_columns(u.obj, words)) for u in chosen]
        lines = list(dict.fromkeys(lines))
        eff = max((u.obj.effective for u in chosen if u.obj.effective), default=None)
        head = ("Rates" if dated else "Current rates") if q.intent == "rate" else "From the Dupaco website"
        if eff:
            head += f" (effective {eff:%m/%d/%Y})"
        text = head + ":\n" + "\n".join(f"- {l}" for l in lines)
        if "change" in q.flags:
            text += self._timeline_text(snap, chosen)
        evidence = self._row_sources(snap, chosen)
        note = self._stale_note(snap, chosen)
        return Result(text=text, evidence=evidence, confident=True, conflict_note=note,
                      coverage=self._row_coverage(q, snap, chosen[0]))

    def _row_coverage(self, q: Query, snap: SnapshotIndex, u: Unit) -> float:
        req = self._required(q, snap)
        if not req:
            return 1.0
        ctx = set(u.ctx_toks) | set(tokens(u.page.parsed.h1 or u.page.title))
        return sum(self._covered(t, ctx) for t in req) / len(req)

    def _timeline_text(self, snap: SnapshotIndex, chosen: list[Unit]) -> str:
        """'Did the 12-month rate change?': how the same row read in each snapshot up to this one."""
        from .conflicts import history_key
        self.conflict_index()
        tl = self._conflicts.get("timelines", {})
        out = []
        for u in chosen[:4]:
            seq = tl.get((u.page.is_business, history_key(u.obj, sys.modules[__name__])))
            if not seq:
                continue
            seq = [(d, v) for d, v in seq if d <= snap.taken_on]
            if len(seq) < 2:
                continue
            steps = "; ".join(f"{', '.join(f'{val} {col}' for col, val in v)} from {d:%m/%d/%Y}" for d, v in seq)
            out.append(f"{row_text(u.obj)} changed over time: {steps}.")
        if not out:
            return "\nNo change in this rate across the website snapshots (May-Sep 2026)."
        return "\n" + "\n".join(out)

    def _table_rows(self, snap: SnapshotIndex, u: Unit) -> list[Unit]:
        return snap.tables[(u.page.url, u.obj.table_id)]

    # ------------------------------------------------------------------ stale tables
    @staticmethod
    def table_signature(rows: list[Unit]) -> set[str]:
        """Row labels with numbers masked: '2023-26 model year | 60 months' ~ '2024-27 model year | 60 months'."""
        return {re.sub(r"\d[\d,.]*", "#", norm_label(u.obj)) for u in rows}

    @staticmethod
    def table_rank(rows: list[Unit]) -> tuple:
        u = rows[0]
        eff = max((x.obj.effective for x in rows if x.obj.effective), default=None)
        return (u.page.authority, eff or date.min)

    @staticmethod
    def outranks(a: tuple, b: tuple) -> bool:
        """Does a table with rank `a` beat one with rank `b`? A big authority gap decides first
        (rate sheet vs calculator); otherwise the newer 'effective as of' date wins."""
        if abs(a[0] - b[0]) >= 0.25:
            return a[0] > b[0]
        if a[1] != b[1]:
            return a[1] > b[1]
        return a[0] > b[0]

    def superseding(self, snap: SnapshotIndex, key: tuple, among: list[tuple]) -> Optional[tuple]:
        """Return the table that makes `key` stale: same product rows (by masked label), same
        personal/business side, and a better (authority, effective date) rank."""
        rows = snap.tables[key]
        sig = self.table_signature(rows)
        if not sig:
            return None
        biz = rows[0].page.is_business
        rk = self.table_rank(rows)
        for other in among:
            if other == key:
                continue
            orows = snap.tables[other]
            if orows[0].page.is_business != biz:
                continue
            osig = self.table_signature(orows)
            if len(sig & osig) / len(sig) >= 0.5 and self.outranks(self.table_rank(orows), rk):
                return other
        return None

    def _drop_superseded(self, snap: SnapshotIndex, scored: list[tuple[float, Unit]]) -> list[tuple[float, Unit]]:
        keys = list(dict.fromkeys((u.page.url, u.obj.table_id) for _, u in scored))[:80]
        stale = {k for k in keys if self.superseding(snap, k, keys)}
        return [(s, u) for s, u in scored if (u.page.url, u.obj.table_id) not in stale]

    def _row_sources(self, snap: SnapshotIndex, chosen: list[Unit]) -> list[Evidence]:
        """Cite the page we answered from plus other trustworthy pages that show the same row+value."""
        ev = [Evidence(chosen[0].page.url, snap.snapshot_id, fmt_row(chosen[0].obj))]
        want = {(norm_label(u.obj), fmt_row(u.obj)) for u in chosen[:6]}
        seen = {chosen[0].page.url}
        for x in snap.rows:
            if x.page.url in seen or x.page.authority < 0.85:
                continue
            if (norm_label(x.obj), fmt_row(x.obj)) in want:
                seen.add(x.page.url)
                ev.append(Evidence(x.page.url, snap.snapshot_id, fmt_row(x.obj)))
        return ev[:5]

    def _stale_note(self, snap: SnapshotIndex, chosen: list[Unit]) -> Optional[str]:
        conf = self.conflict_index().get(snap.snapshot_id, {})
        stale: dict[str, list[str]] = defaultdict(list)
        why = {}
        for u in chosen[:8]:
            c = conf.get((u.page.is_business, norm_label(u.obj)))
            if c:
                stale_url, stale_vals, good_url, reason = c
                stale[stale_url].append(stale_vals)
                why[stale_url] = (good_url, reason)
        notes = [f"{url} still shows older figures ({'; '.join(v)}); the values above come from "
                 f"{why[url][0]} ({why[url][1]})." for url, v in stale.items()]
        return " ".join(notes) or None

    # ------------------------------------------------------------------ passages (facts, hours, news)
    TYPE_PATTERNS = {
        "hours": re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\b\.?|p\.?m\b\.?)|\bnoon\b", re.I),
        "date": re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|"
                           r"December)\s+\d{1,2},?\s+\d{4}|\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
        "address": re.compile(r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z0-9][\w.]*(?:\s+[A-Z][\w.]*){0,3}\s+"
                              r"(?:St|Ave|Rd|Dr|Ln|Blvd|Street|Avenue|Road|Drive|Lane|Way|Pkwy|Parkway|Ct|Court|"
                              r"Plaza|Pl|Hwy|Highway)\b\.?"),
        "phone": re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
        "routing": re.compile(r"(?<![\d-])\d{9}(?![\d-])"),
        "money": re.compile(r"\$\s?\d"),
    }
    CTA_RE = re.compile(r"^(for more information|to learn more|learn more|visit|click|call us|contact us|"
                        r"read more|see details|apply today)", re.I)
    HOWTO_RE = re.compile(r"^\s*(how (?:do|can|would|should) (?:i|we|you)|where (?:do|can) i|can i|is it possible)", re.I)

    def _type_ok(self, q: Query, text: str) -> Optional[bool]:
        if q.answer_type == "count":
            if not q.count_noun:
                return bool(re.search(r"\d", text))
            for m in re.finditer(r"(?<![\w$.,-])(\d[\d,]*)\s+((?:[A-Za-z’'-]+\s+){0,2}[A-Za-z’'-]+)", text):
                if q.count_noun in tokens(m.group(2)):
                    return True
            for m in re.finditer(r"((?:[A-Za-z’'-]+\s+){1,5})(?:more than |over |nearly |about )?(\d[\d,]{2,})(?![\d%])", text):
                if q.count_noun in tokens(m.group(1)):
                    return True
            return False
        pat = self.TYPE_PATTERNS.get(q.answer_type or "")
        if pat is None:
            return None
        return bool(pat.search(text))

    def passage_candidates(self, q: Query, snap: SnapshotIndex) -> list:
        bm = snap.passage_index.scores(q.expanded)
        if not bm:
            return []
        subject = [t for t in q.subject if snap.vocab.get(t, 0) or any(snap.vocab.get(x, 0) for x in SYN.get(t, []))]
        proper = [t for t in q.proper if snap.vocab.get(t)]
        howto = bool(self.HOWTO_RE.search(q.raw))
        action = subject[0] if (howto and subject) else None
        # rarer words say more about what is being asked: weight coverage by idf
        idf = snap.passage_index.idf
        sw = [(t, max(idf.get(t, 0.0), max((idf.get(x, 0.0) for x in SYN.get(t, [])), default=0.0), 0.5)) for t in subject]
        sw_total = sum(w for _, w in sw) or 1.0
        cands = []
        depth = 2000 if q.answer_type else 600
        for i, base in sorted(bm.items(), key=lambda kv: -kv[1])[:depth]:
            u = snap.passages[i]
            p = u.page
            title = set(tokens(p.parsed.h1)) | set(tokens(p.title))
            bag = set(u.ctx_toks) | title
            if proper and not all(self._covered(t, bag) for t in proper):
                continue
            cov = (sum(w for t, w in sw if self._covered(t, bag)) / sw_total) if subject else 1.0
            tcov = (sum(w for t, w in sw if self._covered(t, title)) / sw_total) if subject else 0.0
            head = set(tokens(u.obj.heading)) - title
            hcov = (sum(w for t, w in sw if self._covered(t, head)) / sw_total) if subject and head else 0.0
            auth = PASSAGE_AUTH.get(p.category, 0.8)
            if howto and p.category in ("news", "blog"):
                auth *= 0.6
            if howto and p.category == "legal":
                auth *= 0.4                                # "how do I…" isn't answered by contract fine print
            if p.is_business != ("business" in q.flags):
                auth *= 0.6                                # personal vs business is a different product
            if howto and action and action not in u.ctx_toks:
                auth *= 0.4                                # "how do I join": the passage must talk about joining
            s = base * auth * (0.15 + 0.85 * cov ** 2) * (1 + 2.0 * tcov) * (1 + 0.5 * hcov)
            exp = expired_on(u.obj.text)
            if exp and exp < snap.taken_on:
                s *= 0.1                                   # an offer that has ended
            if i in snap.leads:
                s *= 1.25                                  # news: the lead paragraph carries the key fact
            link_chars = sum(len(t) for t, _ in u.obj.links)
            if link_chars > 0.5 * len(u.obj.text):
                s *= 0.3                                   # navigation / button rows, not content
            ok = self._type_ok(q, u.obj.text)
            if ok is True:
                s *= 2.0
            elif ok is False:
                s *= 0.3
            cands.append((s, cov, ok, u))
        if q.answer_type == "count":
            cands = self._corroborate_counts(q, snap, cands)
        cands.sort(key=lambda x: (-x[0], x[3].page.url, x[3].obj.line_no))
        return cands

    def answer_passage(self, q: Query, snap: SnapshotIndex) -> Optional[Result]:
        if self._unknown_names(q, snap):
            return None
        cands = self.passage_candidates(q, snap)
        if not cands:
            return None
        s, cov, ok, u = cands[0]
        if u.page.date and u.page.category in ("news", "blog"):
            # several dated articles on the same topic: the most recent one is the current statement
            ttl = set(tokens(u.page.title))
            close = [c for c in cands[:40] if c[0] >= 0.7 * s and c[1] >= cov and c[2] is not False
                     and c[3].page.date and c[3].page.category in ("news", "blog")
                     and len(ttl & set(tokens(c[3].page.title))) >= 0.5 * max(1, len(ttl))] or [cands[0]]
            newest = max(close, key=lambda c: (c[3].page.date, c[0]))
            if newest[3].page.date > u.page.date:
                s, cov, ok, u = newest
        if q.answer_type == "hours" and not q.proper:
            multi = self._hours_examples(q, snap, cands)
            if multi:
                return multi
        if re.match(r"\s*(what is|what's|what are|tell me about|explain|describe)\b", q.raw, re.I) and q.answer_type is None:
            # a definition is usually in the lead paragraph, not in the closing call-to-action
            same = [c for c in cands if c[3].page is u.page and c[1] >= cov and c[0] >= 0.5 * s]
            if same:
                s, cov, ok, u = min(same, key=lambda c: c[3].obj.line_no)
        if cov < 0.5 or (ok is False and q.answer_type in ("hours", "address", "phone")):
            return None
        sent, u = self._best_on_page(q, snap, cands, (s, cov, ok, u))
        sent, u2 = self._prefer_newest_phrase(q, snap, sent, u)
        sent = re.sub(r"^.{0,80}?\b[A-Z][A-Z .]{2,},\s+(?:[A-Z][a-z]+\.?|[A-Z]{2})\s?[—–-]\s*", "", sent)  # dateline
        text = sent
        title = self._page_label(u2.page)
        if title and q.subject:
            stoks, ttoks = set(tokens(sent)), set(tokens(title))
            if any(t in ttoks and t not in stoks for t in q.subject) and title.lower() not in sent.lower():
                text = f"{title}: {sent}"
        howto = bool(self.HOWTO_RE.search(q.raw))
        link = self._relevant_link(q, u2) if howto or re.search(r"\b(online|apply|join|enroll|sign up|download|website)\b", q.norm) else None
        if link and link not in text:
            text += f" ({link})"
        ev = [Evidence(u2.page.url, snap.snapshot_id, sent[:200])]
        return Result(text=text, evidence=ev, confident=True, coverage=cov)

    FOLLOW_RE = re.compile(r"(?<![\w$.,-])(\d[\d,]*)\s+((?:[A-Za-z’'-]+\s+){0,2}[A-Za-z’'-]+)")
    PRECEDE_RE = re.compile(r"((?:[A-Za-z’'-]+\s+){2,6})(\d[\d,]{2,})(?![\d%])")

    def _count_shapes(self, q: Query, text: str) -> set[str]:
        """Number-free 'shapes' of the count statements in a text: '# branch office locations',
        'membership has grown to more than #'."""
        out = set()
        for m in self.FOLLOW_RE.finditer(text):
            if q.count_noun in tokens(m.group(2)):
                out.add("# " + m.group(2).lower())
        for m in self.PRECEDE_RE.finditer(text):
            if q.count_noun in tokens(m.group(1)):
                out.add(m.group(1).lower().strip() + " #")
        return out

    def _corroborate_counts(self, q: Query, snap: SnapshotIndex, cands: list) -> list:
        """'How many branches?': a count the site states over and over (press-release boilerplate) is
        about the credit union itself; a one-off number in a blog post is usually about something else."""
        freq: dict[str, int] = defaultdict(int)
        for x in snap.passages:
            if q.count_noun in x.ctx_toks:
                for sh in self._count_shapes(q, x.obj.text):
                    freq[sh] += 1
        out = []
        for s, cov, ok, u in cands:
            if ok:
                f = max((freq.get(sh, 0) for sh in self._count_shapes(q, u.obj.text)), default=0)
                s *= 1 + math.log(1 + f)
            out.append((s, cov, ok, u))
        return out

    def _hours_examples(self, q: Query, snap: SnapshotIndex, cands: list) -> Optional[Result]:
        """General hours question: schedules differ by branch, so give the typical ones and ask which branch."""
        pat = self.TYPE_PATTERNS["hours"]
        picked, seen = [], set()
        for s, cov, ok, u in sorted(cands, key=lambda c: (-c[0], c[3].page.url, c[3].obj.line_no)):
            if not ok or cov < 0.6:
                continue
            text = re.split(r"\bABOUT [A-Z]", u.obj.text)[0].strip()
            m = re.search(r"(Branch hours|Hours|Lobby)", text)
            if m:
                text = text[m.start():]
            key = re.sub(r"[^0-9:apmnoon]", "", text.lower())[:80]
            if key in seen or not pat.search(text):
                continue
            seen.add(key)
            picked.append((text[:400], u))
            if len(picked) == 3:
                break
        if not picked:
            return None
        body = " | ".join(t for t, _ in picked)
        text = ("Hours vary by branch. Typical schedules on the Dupaco website: " + body +
                " Which branch would you like hours for?") if len(picked) > 1 else picked[0][0]
        return Result(text=text, evidence=[Evidence(u.page.url, snap.snapshot_id, t[:200]) for t, u in picked])

    @staticmethod
    def _page_label(p: PageRec) -> str:
        t = (p.title or "").split(" | ")[0].strip()
        h = (p.parsed.h1 or "").strip()
        for c in (t, h):
            if c and len(c) <= 70 and not c.startswith("http"):
                return c
        return ""

    def _relevant_link(self, q: Query, u: Unit) -> Optional[str]:
        best, best_s = None, 0
        want = set(q.expanded)
        for txt, url in u.obj.links:
            host = urlparse(url).netloc
            if "dupaco.com" not in host:
                continue
            sc = len(want & (set(tokens(txt)) | set(tokens(urlparse(url).path)) | set(tokens(host))))
            if sc > best_s:
                best, best_s = url, sc
        return best

    ABBREV_RE = re.compile(r"\b(St|Ave|Rd|Dr|Ln|Blvd|Hwy|Inc|Co|Corp|Mr|Mrs|Ms|Dr|Jr|Sr|No|Mt|Ft|U\.S|"
                           r"e\.g|i\.e|a\.m|p\.m|Jan|Feb|Mar|Apr|Aug|Sept?|Oct|Nov|Dec|[A-Z])\.$")

    @classmethod
    def _sentences(cls, text: str) -> list[str]:
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", text) if p.strip()]
        out: list[str] = []
        for p in parts:
            if out and cls.ABBREV_RE.search(out[-1]):
                out[-1] = out[-1] + " " + p          # "South Locust St. Hy-Vee store" is one sentence
            else:
                out.append(p)
        return out

    def _best_on_page(self, q: Query, snap: SnapshotIndex, cands: list, top) -> tuple[str, Unit]:
        """Pick the best sentence from the strongest passages of the chosen page (idf-weighted
        overlap, answer-type bonus, FAQ and call-to-action handling), then expand it to a readable answer."""
        s0, _, _, u0 = top
        pool = [c for c in cands[:200] if c[3].page is u0.page]
        if not any(c[3] is u0 for c in pool):
            pool.insert(0, top)
        idf = snap.passage_index.idf
        want = set(q.expanded) | ({"dupaco"} & set(q.toks))
        unit = (sum(idf.get(t, 1.0) for t in want) / len(want)) if want else 1.0
        best = None
        for c in pool:
            u = c[3]
            sents = self._sentences(u.obj.text)
            toks = [set(tokens(x)) for x in sents]
            for i, st in enumerate(sents):
                sc = sum(idf.get(t, 1.0) for t in want & toks[i])
                if i > 0 and self.ANAPHOR_RE.match(st):
                    sc += 0.5 * sum(idf.get(t, 1.0) for t in want & toks[i - 1])
                if self.CTA_RE.search(st):
                    sc *= 0.5
                if self._type_ok(q, st):
                    sc += 2.5 * unit
                if st.endswith("?") and i + 1 < len(sents):
                    # an FAQ question that matches the member's question: the answer is what follows
                    sc *= 1.15
                    if st.split()[0].lower() == q.raw.split()[0].lower():
                        sc += 0.5 * unit
                elif st.endswith("?"):
                    sc *= 0.5
                sc *= 0.7 + 0.3 * min(1.0, c[0] / s0) if s0 else 1.0     # passage relevance, mildly
                if u is u0:
                    sc += 0.05 * unit
                key = (sc, -u.obj.line_no, -i)
                if best is None or key > best[0]:
                    best = (key, u, sents, i)
        _, u, sents, i = best
        return self._expand(sents, i), u

    def _expand(self, sents: list[str], i: int) -> str:
        text = " ".join(sents)
        if len(sents) == 1:
            return text[:700]
        out = [sents[i]]
        j = i + 1
        # an FAQ question, or a short lead-in: keep the following sentence(s)
        while j < len(sents) and (out[-1].endswith("?") or len(" ".join(out)) < 140 or
                                  (sents[i].endswith("?") and len(" ".join(out)) < 300)):
            out.append(sents[j])
            j += 1
        if sents[i].endswith("?") and len(out) > 1:
            out = out[1:]
        elif i > 0 and self.ANAPHOR_RE.match(sents[i]):
            out.insert(0, sents[i - 1])
        # keep a directly following sentence that refers back ("This initiative provides up to ...")
        if j < len(sents) and self.ANAPHOR_RE.match(sents[j]) and len(" ".join(out)) < 400:
            out.append(sents[j])
        return " ".join(out)[:700]

    def _best_sentences(self, q: Query, text: str, heading: str) -> str:
        sents = self._sentences(text)
        want = set(q.expanded) | ({"dupaco"} & set(q.toks))
        scores = [len(want & set(tokens(x))) + (2.5 if self._type_ok(q, x) else 0) for x in sents]
        i = max(range(len(sents)), key=lambda k: (scores[k], -k)) if sents else 0
        return self._expand(sents, i) if sents else text[:700]

    ANAPHOR_RE = re.compile(r"(This|That|These|Those|It|They|He|She|Both|Each|The program|The branch|The initiative)\b")

    NUM_PHRASE = re.compile(r"\b(\d[\d,]*)\s+([a-z]+(?:\s+[a-z]+){1,2})", re.I)

    def _prefer_newest_phrase(self, q: Query, snap: SnapshotIndex, sent: str, u: Unit) -> tuple[str, Unit]:
        """Press-release boilerplate repeats facts ("22 branch office locations", "membership has grown to
        more than 179,000") with values that change over the years. If the chosen sentence states such a
        fact, answer with the newest dated statement of it."""
        if u.page.category not in ("news", "blog"):
            return sent, u
        mine = u.page.date or date.min
        want = set(q.expanded) | ({q.count_noun} if q.count_noun else set())
        pats = []
        for m in self.FOLLOW_RE.finditer(sent):
            phrase = m.group(2).lower()
            if set(tokens(phrase)) & want:
                pats.append(re.compile(r"\b(\d[\d,]*)\s+" + re.escape(phrase) + r"\b", re.I))
        for m in self.PRECEDE_RE.finditer(sent):
            phrase = m.group(1).strip()
            if set(tokens(phrase)) & want:
                pats.append(re.compile(re.escape(phrase) + r"\s+(\d[\d,]{2,})", re.I))
        best = None
        for pat in pats:
            for x in snap.passages:
                if x.page.date is None or x.page.category not in ("news", "blog"):
                    continue
                if (best is None or x.page.date > best[0]) and pat.search(x.obj.text):
                    best = (x.page.date, x)
        if best and best[0] > mine:
            x = best[1]
            return self._best_sentences(q, x.obj.text, x.obj.heading), x
        return sent, u

    # ------------------------------------------------------------------ links
    def answer_link(self, q: Query, snap: SnapshotIndex) -> Optional[Result]:
        skip = {"link", "url", "websit", "website", "web", "page", "site", "address", "onlin", "online", "find"}
        want = [t for t in q.expanded if t not in skip]
        if not want:
            return None
        idf = snap.passage_index.idf
        strong = set(self._required(q, snap)) - skip
        wt = {t: (idf.get(t, 1.0) if t in strong else 0.3) for t in want}
        # "open / apply / join / log in" wants an application link; "see / find / check rates" wants an info page
        transact = bool(re.search(r"\b(open|apply|join|enroll|sign up|log ?in|register|pay)\b", q.norm))
        scores: dict[str, float] = defaultdict(float)
        where: dict[str, PageRec] = {}
        for p, ln in snap.links:
            url = ln.url
            host = urlparse(url).netloc.lower()
            if "dupaco.com" not in host:
                continue
            bag = set(tokens(ln.text)) | set(tokens(ln.context)) | set(tokens(urlparse(url).path))
            hit = sum(wt[t] for t in set(want) if self._covered(t, bag))
            if not hit or (strong and not any(self._covered(t, bag) for t in strong)):
                continue
            clean = url.split("?")[0]
            target = AUTHORITY[classify(clean)] if "www.dupaco.com" in host else (0.9 if transact else 0.4)
            scores[clean] += hit * p.authority * target
            where.setdefault(clean, p)
        for p in snap.pages.values():
            bag = set(tokens(p.parsed.h1 or p.title)) | set(tokens(urlparse(p.url).path))
            hit = sum(wt[t] for t in set(want) if self._covered(t, bag))
            if hit and (not strong or any(self._covered(t, bag) for t in strong)):
                scores[p.url] += 2 * hit * p.authority * p.authority
                where.setdefault(p.url, p)
        if not scores:
            return None
        url = sorted(scores, key=lambda k: (-scores[k], len(k)))[0]
        url = re.sub(r"#[^#]*\d{6,}.*$", "", url)
        p = where[url] if url in where else None
        return Result(text=f"You can use this link: {url}",
                      evidence=[Evidence(url, snap.snapshot_id, url)] + ([Evidence(p.url, snap.snapshot_id, "")] if p else []),
                      confident=True)

    # ------------------------------------------------------------------ conflicts
    def conflict_index(self) -> dict:
        if self._conflicts is None:
            from .conflicts import build_conflicts
            self._conflicts = build_conflicts(self)
        return self._conflicts["by_row"]

    def conflicts(self) -> list[dict]:
        self.conflict_index()
        return self._conflicts["list"]