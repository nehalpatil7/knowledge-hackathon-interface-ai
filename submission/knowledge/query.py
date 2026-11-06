"""Question understanding: intent, qualifiers (term, model year, balance, flags) and dates."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .text import SYN, stem, CONVERSATIONAL_S, GENERIC_S, NUMBER_WORDS, STOP, content_tokens, expand, normalize, tokens

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_NAMES.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

RATE_RE = re.compile(r"\b(rates?|aprs?|apys?|dividends?|yields?|interest(?! in)|percent)\b", re.I)
FEE_RE = re.compile(r"\b(fees?|cost|costs|charge|charged|how much|price|minimum|min\.?)\b", re.I)
LINK_RE = re.compile(r"\b(link|url|web ?address|website|web ?page|site address)\b|"
                     r"\bwhere (?:can|do|should) i (?:find|see|look up|check)\b.*\bonline\b", re.I)
LIST_RE = re.compile(r"\b(all|every|list|what (?:kinds?|types?)|which (?:cards?|accounts?|loans?)|"
                     r"options|do you (?:offer|have))\b", re.I)
SUPER_HI = re.compile(r"\b(highest|best|top|max(?:imum)?|most)\b", re.I)
SUPER_LO = re.compile(r"\b(lowest|cheapest|min(?:imum)? rate|least)\b", re.I)
CHANGE_RE = re.compile(r"\b(chang\w*|previous(?:ly)?|used to|before|prior|increas\w*|decreas\w*|went (?:up|down)|"
                       r"raise[ds]?|lower(?:ed)?|history|was it)\b", re.I)


@dataclass
class Query:
    raw: str
    norm: str
    toks: list[str]
    content: list[str]
    expanded: list[str]
    required: list[str] = field(default_factory=list)   # product words a table row must cover
    subject: list[str] = field(default_factory=list)    # words a prose answer should cover
    answer_type: Optional[str] = None                   # hours | count | date | address | phone | money | percent
    verbish: set = field(default_factory=set)           # -ing / -ed words (rarely name a product)
    unknown: list = field(default_factory=list)         # words the website never uses (after spell-fixing)
    count_noun: Optional[str] = None                    # "how many <branches>"
    proper: list[str] = field(default_factory=list)     # Capitalised names in the question
    months: set[int] = field(default_factory=set)       # loan / certificate term in months
    years: set[int] = field(default_factory=set)        # "30-year" style terms
    model_year: Optional[int] = None
    amount: Optional[float] = None
    flags: set[str] = field(default_factory=set)
    intent: str = "fact"
    asked_date: Optional[date] = None


def _num(s: str) -> Optional[int]:
    s = s.lower()
    if s.isdigit():
        return int(s)
    return NUMBER_WORDS.get(s)


def parse_question(q: str) -> Query:
    norm = normalize(q)
    toks = tokens(q)
    content = content_tokens(q)
    qu = Query(raw=q, norm=norm, toks=toks, content=content, expanded=expand(content))

    # ------------------------------------------------------------------ terms
    for m in re.finditer(r"\b(\d{1,3}|[a-z]+(?:-[a-z]+)?)[\s-]*(?:months?|mos?\.?)\b", norm):
        n = _num(m.group(1))
        if n:
            qu.months.add(n)
    for m in re.finditer(r"\b(\d{1,2}|[a-z]+)[\s-]*(?:years?|yrs?)\b(?![\s-]*old)", norm):
        n = _num(m.group(1))
        if n and n <= 40 and not re.search(r"model", norm[m.end():m.end() + 8]):
            qu.years.add(n)
    if re.search(r"\b(\d)/1\b|\barm\b|adjustable", norm):
        qu.flags.add("arm")

    # ------------------------------------------------------------------ model year (vehicles)
    m = re.search(r"\b(19[89]\d|20[0-4]\d)\b(?:\s*model)?", norm)
    if m and re.search(r"model|vehicle|car|auto|truck|motorcycle|suv|atv|boat|rv|snowmobile", norm):
        qu.model_year = int(m.group(1))
        qu.years.discard(qu.model_year)

    # ------------------------------------------------------------------ dollar amount (tier selection)
    amts = []
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million|m)?\b", norm):
        v = float(m.group(1).replace(",", ""))
        mult = {"k": 1e3, "thousand": 1e3, "million": 1e6, "m": 1e6}.get((m.group(2) or "").lower(), 1)
        amts.append(v * mult)
    for m in re.finditer(r"\b(\d[\d,]{3,})\s*(?:dollars)\b", norm):
        amts.append(float(m.group(1).replace(",", "")))
    if amts:
        # "between $10,000 and $24,999" -> the lower bound identifies the tier
        qu.amount = min(amts) if re.search(r"between|from|range", norm) else amts[0]

    # ------------------------------------------------------------------ flags
    for flag, pat in {
        "jumbo": r"\bjumbo\b", "ira": r"\b(ira|iras|roth|traditional|retirement)\b", "roth": r"\broth\b",
        "traditional": r"\btraditional\b", "business": r"\bbusiness\b", "personal": r"\bpersonal\b",
        "refinance": r"\b(refinanc\w*|refi)\b", "purchase": r"\b(purchas\w*|buy\w*)\b",
        "intro": r"(?<!not the )(?<!not an )(?<!not )(?<!excluding )\b(intro\w*|balance transfer|promo\w*)\b",
        "special": r"\b(special|promo\w*)\b",
        "variable": r"\bvariable\b", "hours": r"\b(hours?|open|close[sd]?|closing)\b",
        "address": r"\b(address|located|location|where is)\b", "phone": r"\b(phone|call|telephone)\b",
        "new": r"\bnew (?:car|vehicle|auto)\b", "used": r"\bused\b", "standard": r"\b(standard|regular|non-promo)\b",
    }.items():
        if re.search(pat, norm):
            qu.flags.add(flag)
    if SUPER_HI.search(norm):
        qu.flags.add("highest")
    if SUPER_LO.search(norm):
        qu.flags.add("lowest")
    if CHANGE_RE.search(norm):
        qu.flags.add("change")
    if LIST_RE.search(norm) or re.search(r"\b(cards|certificates|cds|loans|rates|accounts)\b", norm):
        qu.flags.add("plural")

    # ------------------------------------------------------------------ intent
    if LINK_RE.search(norm):
        qu.intent = "link"
    elif (RATE_RE.search(norm) and not re.search(r"\binterest(ed)? in\b", norm)) or \
            (re.search(r"\b(pays?|paying|earns?|earning)\b", norm) and not FEE_RE.search(norm)
             and re.search(r"\b(savings?|certificates?|cds?|accounts?|checking|money market|iras?|hsa|special)\b", norm)):
        qu.intent = "rate"
    elif FEE_RE.search(norm):
        qu.intent = "fee"
    else:
        qu.intent = "fact"

    # ------------------------------------------------------------------ "as of" mentioned in text
    m = re.search(r"\b(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\.?,?\s+(20\d\d)\b", norm)
    if m:
        y, mo = int(m.group(2)), MONTH_NAMES[m.group(1)]
        qu.asked_date = date(y, mo, calendar.monthrange(y, mo)[1])

    # ------------------------------------------------------------------ required / proper words
    qualifier_words = {stem(w) for w in ("jumbo", "roth", "traditional", "refinance",
                                         "purchase", "intro", "introductory", "special", "highest", "lowest",
                                         "best", "top", "model", "maximum", "max", "minimum", "largest",
                                         "biggest", "smallest", "cheapest", "lowest", "change", "changed",
                                         "changing", "increase", "decrease", "previous", "previously", "history",
                                         "before", "prior", "raise", "raised", "lower", "lowered", "went")}
    date_words = set(MONTH_NAMES) | {"year", "month", "week", "day", "ago", "last", "since"}
    # participles / gerunds ("thinking", "used", "planning") describe the member, not the product
    verbish = {stem(w) for w in re.findall(r"[a-z]+", norm) if re.search(r"(ing|ed)$", w) and len(w) >= 4}
    verbish |= {"new", "old", "used"}
    qu.verbish = verbish
    qu.required = [t for t in dict.fromkeys(content)
                   if t not in GENERIC_S and t not in CONVERSATIONAL_S and t not in qualifier_words and t not in date_words
                   and not t.isdigit() and (len(t) > 2 or t in SYN)]
    qu.subject = [t for t in dict.fromkeys(content)
                  if t not in CONVERSATIONAL_S and t not in date_words and not t.isdigit() and len(t) > 2]

    # ------------------------------------------------------------------ expected answer type
    if re.search(r"\bhow many\b|\bnumber of\b", norm) and not re.search(r"\b(routing|phone|account|card|aba)\s+number", norm):
        qu.answer_type = "count"
        m = re.search(r"\b(?:how many|number of)\s+(?:[a-z]+\s+)?([a-z]+)", norm)
        m2 = re.search(r"\b(?:how many|number of)\s+(?:dupaco\s+)?([a-z]+)", norm)
        for mm in (m2, m):
            if mm and stem(mm.group(1)) not in STOP and mm.group(1) not in STOP:
                qu.count_noun = stem(mm.group(1))
                break
    elif re.search(r"\b(routing|aba|transit)\b", norm):
        qu.answer_type = "routing"
    elif re.search(r"\b(hours?|what time|open(?:s)? (?:on|at|until)|close[sd]? at|when (?:is|are|does|do)\b.*\bopen)\b", norm) and not re.search(r"\b(when (?:does|did|will)|clos(?:e|ing) date)\b", norm):
        qu.answer_type = "hours"
    elif re.search(r"\b(when|what date|what day|since when)\b", norm) or re.search(r"\bstill open\b", norm):
        qu.answer_type = "date"
    elif re.search(r"\b(address|where is|located|location of)\b", norm):
        qu.answer_type = "address"
    elif re.search(r"\b(phone number|telephone|what number|number (?:do|should|can) i call|number to call|"
                   r"call (?:you|dupaco|to|about|if)|fax)\b", norm):
        qu.answer_type = "phone"
    elif re.search(r"\b(how much|fee|cost|price|charge)\b", norm):
        qu.answer_type = "money"
    if qu.answer_type == "count":
        qu.subject = [t for t in qu.subject if t not in ("number", "total", "operat")]

    words = re.findall(r"[A-Za-z][A-Za-z'’$]+", q)
    for i, w in enumerate(words):
        if i > 0 and w[0].isupper() and w.lower() not in STOP and len(w) > 2:
            lw = tokens(w)
            if lw and lw[0] not in GENERIC_S and w.lower() not in MONTH_NAMES:
                qu.proper.append(lw[0])
    return qu
