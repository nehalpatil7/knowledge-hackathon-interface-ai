"""Markdown page -> structured units.

A crawled page is turned into three kinds of units:

* TableRow  - one row of a markdown table, with the table's caption (the short lines just above
              it, e.g. "Special Certificates"), its header, and the "Rates are effective as of ..."
              date printed under the table.
* Passage   - a chunk of prose (a few related lines under one heading).
* Link      - an <a> link with its anchor text and the sentence it sits in.

Everything is plain data so it can be cached by content hash: most pages are identical across
snapshots, so each distinct page body is parsed once.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}

LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
EFFECTIVE_RE = re.compile(
    r"effective\s+(?:as\s+of\s+|on\s+|date[:\s]+)?(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(\d{1,2}),\s+(\d{4})\b")
FOOTNOTE_AFTER_PCT = re.compile(r"(%|\bAPR|\bAPY|\))(\d)\b")
WS = re.compile(r"\s+")
RELATED_RE = re.compile(r"(handpicked for you|related (posts|articles|stories)|you may also like|more from)", re.I)


@dataclass
class TableRow:
    caption: str            # nearest short line(s) above the table, e.g. "Special Certificates"
    header: list[str]
    cells: list[str]
    effective: Optional[date]
    table_id: int           # position of the table on the page
    row_index: int
    line_no: int

    def label_cells(self) -> list[str]:
        return [c for h, c in zip(self.header, self.cells) if not is_value_header(h, c)]

    def value_cells(self) -> list[tuple[str, str]]:
        return [(h, c) for h, c in zip(self.header, self.cells) if is_value_header(h, c)]


@dataclass
class Passage:
    heading: str
    text: str
    line_no: int
    links: list = field(default_factory=list)     # (anchor text, url) found inside the passage


@dataclass
class Link:
    text: str
    url: str
    context: str


@dataclass
class ParsedPage:
    title: str
    h1: str
    published: Optional[date]          # article date for blog / press-release pages
    rows: list[TableRow] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def clean_inline(s: str) -> str:
    """Strip markdown decoration but keep link text."""
    s = html.unescape(html.unescape(s))       # PDF extracts carry "&amp;#45;"
    s = IMG_RE.sub("", s)
    s = LINK_RE.sub(lambda m: m.group(1), s)
    s = s.replace("\\*", "*").replace("\\#", "#").replace("\\$", "$").replace("\\_", "_")
    s = re.sub(r"\*\*|__", "", s)
    s = s.replace("\\r\\n", " ").replace("\\n", " ")
    return WS.sub(" ", s).strip()


def clean_cell(s: str) -> str:
    s = clean_inline(s)
    s = FOOTNOTE_AFTER_PCT.sub(r"\1", s)        # "4.20%1" -> "4.20%", "APR1" -> "APR"
    s = s.replace("\\", "")
    return s.strip()


VALUE_HEADER_RE = re.compile(r"rate|apr\b|apy|yield|fee|cost|charge|price|payment|points|min\.?\b|minimum", re.I)
LABEL_HEADER_RE = re.compile(r"\b(type|program|term|tier|product|certificate|account|balance|loan amount|category|card)\b", re.I)


def is_value_header(header: str, cell: str = "") -> bool:
    h = header or ""
    if LABEL_HEADER_RE.search(h) and not re.search(r"rate|apr\b|apy|yield|fee", h, re.I):
        return False
    if VALUE_HEADER_RE.search(h):
        return True
    # header-less tables: a cell that starts with an amount / percent is a value ("$30.00* per item")
    c = (cell or "").strip()
    return bool(re.match(r"(\$\s?\d|\d[\d,.]*\s*%|\d[\d,.]*\s*(?:each|per)\b|free\b)", c, re.I)) or \
        (bool(re.fullmatch(r"[\s$\d.,%–\-]+(?:APR|APY)?", c)) and bool(re.search(r"\d", c)))


def parse_us_date(m: re.Match) -> Optional[date]:
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_long_date(m: re.Match) -> Optional[date]:
    mon = m.group(1).lower()[:3]
    num = next((v for k, v in MONTHS.items() if k.startswith(mon)), None)
    try:
        return date(int(m.group(3)), num, int(m.group(2))) if num else None
    except ValueError:
        return None


def is_noise(line: str) -> bool:
    """Calculator widgets dump chart data; navigation dumps short menus."""
    if len(line) > 200 and line.count(";") > 10:
        return True
    if "data points" in line.lower():
        return True
    digits = sum(ch.isdigit() for ch in line)
    return len(line) > 80 and digits > 0.45 * len(line)


# --------------------------------------------------------------------------- main

def parse_page(markdown: str, title: str = "") -> ParsedPage:
    lines = markdown.splitlines()
    h1 = ""
    for ln in lines[:40]:
        if ln.startswith("# "):
            h1 = clean_inline(ln[2:])
            break

    # article date: the first long-form date near the top (press releases / blog posts)
    published = None
    for ln in lines[:25]:
        m = LONG_DATE_RE.search(ln)
        if m and (len(ln.strip()) < 40 or re.search(r"written by|posted|published|\|", ln, re.I)) and len(ln) < 200:
            published = parse_long_date(m)
            break

    page = ParsedPage(title=title, h1=h1, published=published)

    heading = h1
    buf: list[str] = []
    buf_links: list[tuple[str, str]] = []
    buf_start = 0
    table_id = 0
    group = ""          # section label carried over to later tables ("Credit Cards" -> "Rewards Visa")
    kv_table, kv_heading, kv_last = None, None, -10
    i = 0
    n = len(lines)

    def flush():
        nonlocal buf, buf_links
        if buf:
            text = " ".join(buf).strip()
            if len(text) >= 3:
                page.passages.append(Passage(heading=heading, text=text, line_no=buf_start, links=buf_links))
        buf = []
        buf_links = []

    while i < n:
        raw = lines[i]
        line = raw.strip()

        # ---------------------------------------------------------------- tables
        if line.startswith("|") and line.count("|") >= 2:
            flush()
            start = i
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = [[clean_cell(c) for c in r.strip("|").split("|")] for r in block]
            rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            if not rows:
                continue
            header, body = rows[0], rows[1:]
            if not body:            # a lone header row (data rendered elsewhere)
                continue
            caption = _caption_above(lines, start)
            parts = caption.split(" / ") if caption else []
            if len(parts) >= 2:
                group = parts[0]
            elif group and parts and group.lower() not in caption.lower():
                caption = f"{group} / {caption}"
            effective = _effective_below(lines, i)
            for ri, cells in enumerate(body):
                if len(cells) < len(header):
                    cells = cells + [""] * (len(header) - len(cells))
                page.rows.append(TableRow(caption=caption, header=header, cells=cells[:len(header)],
                                          effective=effective, table_id=table_id, row_index=ri,
                                          line_no=start + 1 + ri))
            table_id += 1
            continue

        # ---------------------------------------------------------------- links
        for m in LINK_RE.finditer(raw):
            if raw[m.start():m.start() + 1] == "!":
                continue
            txt = clean_inline(m.group(1))
            url = m.group(2)
            if url.startswith("http") or url.startswith("mailto") or url.startswith("tel"):
                page.links.append(Link(text=txt, url=url, context=(heading + " | " + clean_inline(raw))[:300]))
                if url.startswith("http") and txt:
                    buf_links.append((txt, url))

        # ---------------------------------------------------------------- prose
        if not line:
            i += 1
            continue
        if line.startswith("#"):
            flush()
            heading = clean_inline(line.lstrip("#"))
            if RELATED_RE.match(heading):
                break                    # "Handpicked for You" etc.: teasers of *other* articles
            buf_start = i
            # headings are content too (FAQ questions, product names)
            i += 1
            continue
        text = clean_inline(line)
        if re.match(r"(?:\*\*)?ABOUT [A-Z][A-Z .&'’-]{4,}(?::|\*\*|$)", line):
            flush()                      # press-release boilerplate starts its own passage
            heading = "About"
            buf_start = i
        if not text or is_noise(text) or text in {"✕", "Cancel", "* * *"}:
            i += 1
            continue
        if text.startswith("- ") or text.startswith("* "):
            text = text[2:]
        kv = _key_value(text)
        if kv:
            # "6-month certificate APY: 3.50%" / "Domestic wire $25.00 each": a one-line fact becomes a
            # table row so it gets the same tier / staleness / conflict handling as real tables
            if kv_table is None or kv_heading != heading or i - kv_last > 3:
                kv_table, kv_heading = table_id, heading
                table_id += 1
            kv_last = i
            page.rows.append(TableRow(caption=heading or h1, header=["Item", "Value"], cells=list(kv),
                                      effective=_effective_below(lines, i + 1), table_id=kv_table,
                                      row_index=len([r for r in page.rows if r.table_id == kv_table]), line_no=i))
        if not buf:
            buf_start = i
        buf.append(text)
        # chunk boundary: keep passages to a few hundred characters
        size = sum(len(b) for b in buf)
        if (size > 450 and len(text) > 80) or size > 1000:
            flush()
        i += 1
    flush()
    return page


KV_COLON = re.compile(r"^(?P<label>[A-Za-z0-9][^:]{2,90}?):\s*(?P<val>(?:\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*%)[^:]{0,80})$")
KV_PRICE = re.compile(r"^(?P<label>[A-Za-z(][^$:]{3,90}?)\s+(?P<val>\$\s?\d[\d,]*(?:\.\d+)?\*?(?:\s*(?:each|per [a-z /]+))?)$")


def _key_value(text: str) -> Optional[tuple[str, str]]:
    if len(text) > 180:
        return None
    m = KV_COLON.match(text) or KV_PRICE.match(text)
    if not m:
        return None
    return m.group("label").strip(), m.group("val").strip()


DATE_ANY = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},?\s+\d{4})")
EXPIRED_RE = re.compile(r"\b(?:offer|promotion|promo|special|rate)?\s*(?:ended|expired|expires|ends|end(?:s|ed)? on|"
                        r"valid (?:through|until|thru)|available (?:through|until))\s+(?:on\s+)?" + DATE_ANY, re.I)


def any_date(s: str) -> Optional[date]:
    s = s.strip().rstrip(",")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s)
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
        if m:
            y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
            return date(y, int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    except (ValueError, KeyError):
        return None
    return None


def expired_on(text: str) -> Optional[date]:
    """'(offer ended 2025-09-30)' -> 2025-09-30. Promotions whose end date has passed are not current."""
    m = EXPIRED_RE.search(text or "")
    return any_date(m.group(1)) if m else None


def _caption_above(lines: list[str], start: int) -> str:
    """Short, label-like lines right above a table ("Special Certificates", "Home Loans - Purchase")."""
    caps: list[str] = []
    j = start - 1
    seen = 0
    while j >= 0 and seen < 12 and len(caps) < 2:
        t = lines[j].strip()
        j -= 1
        if not t:
            continue
        seen += 1
        if t.startswith("|"):
            break
        if t.startswith("[") or t.startswith("!["):
            continue                     # button / link rows ("Open my certificate", "See details")
        c = clean_inline(t.lstrip("#"))
        if not c or c in {"✕", "Cancel"} or c.lower().startswith("rates are effective"):
            continue
        if len(c) > 90:
            # an explanatory sentence just above the table - stop looking further up
            if caps:
                break
            continue
        caps.append(c)
    return " / ".join(reversed(caps))


def _effective_below(lines: list[str], end: int) -> Optional[date]:
    for j in range(end, min(end + 12, len(lines))):
        t = lines[j]
        if t.strip().startswith("|"):
            break
        m = EFFECTIVE_RE.search(t)
        if m:
            return parse_us_date(m)
    return None
