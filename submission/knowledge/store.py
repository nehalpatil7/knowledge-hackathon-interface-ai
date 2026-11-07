"""Snapshot store: every page version, classified, parsed and indexed per snapshot.

Authority model (how much we trust a page when two pages disagree):

    rate_sheet  /rates, /business-rates                         1.00   the official rate tables
    product     /personal/..., /business/..., /contact, ...      0.90   product pages (same tables)
    document    /hubfs/*.pdf                                     0.60   fee schedules, disclosures
    landing     *-lp, promo / switch / guidance pages            0.50   campaign pages, rarely updated
    news        /newsroom/...                                    0.50   dated press releases
    blog        /learn/..., /YYYY/MM/DD/...                      0.40   dated articles
    calculator  /calculators/...                                 0.35   embedded example rates go stale
    index       tag / author / listing pages, ?query app views,   0.10   teasers of other pages
                "offer expired" pages

Freshness signals: "Rates are effective as of MM/DD/YYYY" under a table, the article date of a
press release / blog post, and the snapshot date itself. A page missing from a crawl is carried
forward from the last crawl that had it, at 85% of its authority.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlparse

from .parse import Link, ParsedPage, parse_page
from .text import tokens

AUTHORITY = {"rate_sheet": 1.0, "product": 0.9, "site": 0.8, "document": 0.6, "landing": 0.5,
             "news": 0.5, "blog": 0.4, "calculator": 0.35, "legal": 0.5, "index": 0.1}

LANDING_RE = re.compile(r"(-lp$|^/switch|offer|promo|guidance|makeover|^/earn-more|^/mortgage-refi|"
                        r"challenge|^/not-for-sale|^/covid)")


def classify(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if parsed.query and not path.startswith("/hubfs"):
        return "index"                  # app views / filtered listings (…?scrollTo=…, ?page=2)
    if path in ("/rates", "/business-rates") or path.startswith("/rates/"):
        return "rate_sheet"
    if "expired" in path:
        return "index"
    if "/tag/" in path or "/author/" in path or "/category" in path or path in ("/learn", "/newsroom", "/learn/blog") \
            or path.startswith("/resources") or path.endswith("/blog"):
        return "index"
    if path.startswith("/calculators") or path.endswith("calculators"):
        return "calculator"
    if path.startswith("/newsroom"):
        return "news"
    if path.startswith("/learn") or re.match(r"^/\d{4}/\d{2}/", path) or path.startswith("/business/resources/"):
        return "blog"
    if re.search(r"privacy|disclosure|terms|conditions|agreement", path):
        return "legal"                  # contract fine print (also /hubfs/*-terms-conditions.pdf)
    if path.startswith("/hubfs"):
        return "document"
    if LANDING_RE.search(path.lstrip("/")) or LANDING_RE.search(path):
        return "landing"
    if path.startswith("/personal") or path.startswith("/business") or path.startswith("/savings") \
            or path.startswith("/checking") or path.startswith("/loans") or path.startswith("/online") \
            or path in ("", "/contact", "/about", "/membership", "/about/join", "/careers"):
        return "product"
    return "site"


@dataclass
class PageRec:
    url: str
    title: str
    category: str
    authority: float
    fetched: Optional[date]
    parsed: ParsedPage
    carried: bool = False      # not in this crawl; last seen in an earlier snapshot

    @property
    def date(self) -> Optional[date]:
        return self.parsed.published

    @property
    def is_business(self) -> bool:
        return "/business" in self.url


@dataclass
class Unit:
    kind: str                # "row" | "passage"
    page: PageRec
    obj: object              # TableRow | Passage
    toks: list[str]          # tokens used for retrieval
    ctx_toks: frozenset      # caption + labels + header (rows) / heading + text (passages)


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.6):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = [len(d) for d in docs]
        self.avg = (sum(self.lens) / self.n) if self.n else 1.0
        post: dict[str, dict[int, int]] = defaultdict(dict)
        for i, d in enumerate(docs):
            for t in d:
                post[t][i] = post[t].get(i, 0) + 1
        self.post = dict(post)
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def df(self, t: str) -> int:
        return len(self.post.get(t, ()))

    def scores(self, q: list[str], weights: Optional[dict[str, float]] = None) -> dict[int, float]:
        out: dict[int, float] = defaultdict(float)
        for t in set(q):
            p = self.post.get(t)
            if not p:
                continue
            w = self.idf[t] * (weights.get(t, 1.0) if weights else 1.0)
            for i, tf in p.items():
                dl = self.lens[i]
                out[i] += w * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avg))
        return out


class SnapshotIndex:
    """Everything known about one snapshot (the full site as of `taken_on`)."""

    def __init__(self, snapshot_id: str, taken_on: date, pages: list[PageRec]):
        self.snapshot_id = snapshot_id
        self.taken_on = taken_on
        self.pages = {p.url: p for p in pages}
        self.rows: list[Unit] = []
        self.passages: list[Unit] = []
        self.leads: set[int] = set()      # ids of lead paragraphs of dated articles
        self.links: list[tuple[PageRec, Link]] = []
        for p in pages:
            title_toks = tokens(p.parsed.h1 or p.title)
            for r in p.parsed.rows:
                ctx = tokens(r.caption) + tokens(" ".join(r.label_cells())) + tokens(" ".join(r.header))
                # words inside value cells ("$10.00 for non-members") describe the row too
                ctx += [t for t in tokens(" ".join(v for _, v in r.value_cells())) if not t[0].isdigit()]
                self.rows.append(Unit("row", p, r, ctx + title_toks, frozenset(ctx)))
            lead_done = p.category not in ("news", "blog")
            for ps in p.parsed.passages:
                if not lead_done and len(ps.text) >= 120 and not ps.text.lower().startswith(("1 min", "2 min", "3 min")):
                    self.leads.add(len(self.passages))
                    lead_done = True
                body = tokens(ps.text)
                head = tokens(ps.heading)
                self.passages.append(Unit("passage", p, ps, body + head + title_toks, frozenset(body + head)))
            for ln in p.parsed.links:
                self.links.append((p, ln))
        self.tables: dict[tuple, list[Unit]] = defaultdict(list)
        for u in self.rows:
            self.tables[(u.page.url, u.obj.table_id)].append(u)
        self.row_index = BM25([u.toks for u in self.rows])
        self.passage_index = BM25([u.toks for u in self.passages])
        vocab: dict[str, int] = defaultdict(int)
        for u in self.passages:
            for t in set(u.toks):
                vocab[t] += 1
        for u in self.rows:
            for t in set(u.toks):
                vocab[t] += 1
        self.vocab = dict(vocab)
        # topic words learned from the site itself: titles, headings and table captions of its own
        # (non-blog, non-news) pages - product and service names, so new content is covered automatically
        self.topics: set[str] = set()
        for p in pages:
            if p.category in ("rate_sheet", "product", "site", "document", "landing"):
                self.topics.update(tokens(p.title + " " + p.parsed.h1))
                self.topics.update(t for ps in p.parsed.passages for t in tokens(ps.heading))
                self.topics.update(t for r in p.parsed.rows for t in tokens(r.caption))
        # the organisation's own name: the word that appears in most page titles ("… | Dupaco")
        title_counts: dict[str, int] = defaultdict(int)
        for p in pages:
            for t in set(tokens(p.title)):
                title_counts[t] += 1
        self.site_names = {t for t, c in title_counts.items() if c > 0.5 * max(1, len(pages)) and len(t) > 3}


class PageCache:
    """Parse each distinct page body once, whichever snapshot it appears in."""

    def __init__(self):
        self._by_hash: dict[str, ParsedPage] = {}

    def get(self, markdown: str, title: str) -> ParsedPage:
        h = hashlib.sha1(markdown.encode("utf-8", "ignore")).hexdigest()
        pp = self._by_hash.get(h)
        if pp is None:
            pp = parse_page(markdown, title)
            self._by_hash[h] = pp
        return pp


def iso_date(s) -> Optional[date]:
    """Lenient date parsing: date / datetime objects, '2026-05-31', '2026-05-31T10:00:00Z', '05/31/2026',
    'May 31, 2026', 20260531. Anything unparseable -> None (never raises)."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    if isinstance(s, int) and 19000101 <= s <= 29991231:
        s = str(s)
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if not isinstance(s, str):
        return None
    s = s.strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    from .parse import any_date
    return any_date(s[:40])