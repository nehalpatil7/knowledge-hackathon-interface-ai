"""Tokenisation, stop-words and the small domain vocabulary (member words -> website words)."""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[$'][a-z0-9]+)*")

STOP = set("""
a an the and or of to in on for at by with from as is are was were be been being am do does did done
i me my we our us you your yours it its this that these those there here what which who whom whose
when where why how can could would should will shall may might must have has had having not no yes
if then than so such about into over under up down out off again any all some each every both few more
most other own same too very just also only s t get got give tell let lets know need want please thanks
thank hi hello like much many well ok okay dupaco dupacos cu credit union im id ive dont
their them they theirs going go goes gone come comes doing make makes made say says said one ones
someone something anything still even really actually currently via per
between within among across after before during while until since upon onto both either neither
whether anyway etc
two three four five six seven eight nine ten eleven twelve fifteen eighteen twenty thirty sixty
""".split())

# Words that carry no information about *which* product / fact is meant. They still help ranking,
# but a candidate answer is not required to contain them.
GENERIC = STOP | set("""
rate rates interest apr apy annual percentage yield dividend dividends current currently today now right
still latest new newest offer offers offered provide provides available charge charges pay paying earn
earning get account accounts option options program programs product products bank banking information info
loan loans term terms month months year years mo yr standard regular fixed price cost fee fees
tell show list give find see look use using repeat again number amount balance
general typical usual usually normally member members people person someone anyone online
""".split())

# Words that never identify the subject of a question (used for prose answers, where "loan" or
# "account" DO matter).
CONVERSATIONAL = STOP | set("""
current currently today now right still latest new newest general typical usual usually normally
tell show list give find see look use using repeat again provide explain describe information info
option options way ways able possible anything something someone anyone people person
receive received receiving get getting got have has keep kept become becoming need needs want
put putting support supports offer offers do does work works hi hey hello thinking looking planning
considering hoping wondering expect expecting finance financing afford borrow borrowing buy buying purchase
plan plans interested currently soon maybe
""".split())

# member word -> extra words the website uses for the same thing
SYNONYMS: dict[str, list[str]] = {
    "cd": ["certificate"], "cds": ["certificate"], "certificates": ["certificate"],
    "car": ["auto"], "vehicle": ["auto"], "vehicles": ["auto"], "truck": ["auto"], "suv": ["auto"], "automobile": ["auto"], "motorcycle": ["auto"],
    "mortgage": ["home"], "mortgages": ["home"],
    "heloc": ["home", "equity", "line"],
    "rv": ["recreational", "boat"], "camper": ["recreational", "rv"], "boat": ["recreational"],
    "jetski": ["jet", "ski"], "snowmobile": ["atv"],
    "hsa": ["health", "savings"],
    "visa": ["card"], "creditcard": ["card", "visa"], "card": ["visa"], "cards": ["visa"],
    "ira": ["retirement"], "iras": ["ira", "retirement"],
    "refi": ["refinance"], "refinancing": ["refinance"],
    "hours": ["hour"], "open": ["hour"],
    "atm": ["atm"], "locator": ["location", "locate", "find"],
    "zelle": ["zelle"], "phone": ["call"], "lose": ["lost"], "aba": ["routing"], "stolen": ["lost"],
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "thirty": 30,
    "thirty-six": 36, "forty-eight": 48, "sixty": 60, "seventy-two": 72, "eighty-four": 84,
}


_SUFFIXES = ("ship", "ability", "ibility", "able", "ible", "izations", "ization", "isation", "ations", "ation", "itions", "ition", "ments", "ment",
             "ness", "ings", "ing", "ized", "ised", "izes", "ize", "ise", "ed", "ly", "ions", "ion")


def stem(w: str) -> str:
    """A small, deterministic suffix stripper (Porter-lite): recognition/recognized -> recogn,
    branches -> branch, rates -> rate, closing/closed/close -> clos."""
    if w.isdigit() or len(w) <= 3 or not w.isascii():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("ches", "shes", "sses", "xes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("–", "-").replace("—", "-").replace("’", "'").replace("‘", "'")
    s = s.replace("certificate of deposit", "certificate").replace("share certificate", "certificate")
    s = s.replace("credit card", "creditcard card").replace("jet ski", "jetski")
    s = s.replace("e-statement", "estatement").replace("e statement", "estatement")
    s = re.sub(r"\bbecome (?:a )?(?:new )?(?:dupaco )?member\b|\bapply for (?:a )?membership\b|\bopen (?:a )?membership\b",
               "join", s)
    return s


def tokens(s: str) -> list[str]:
    return [stem(t.replace("'", "")) for t in TOKEN_RE.findall(normalize(s))]


def content_tokens(s: str) -> list[str]:
    return [stem(t.replace("'", "")) for t in TOKEN_RE.findall(normalize(s))
            if t.replace("'", "") not in STOP]


def expand(toks: list[str]) -> list[str]:
    out = list(toks)
    for t in toks:
        out.extend(SYN.get(t, []))
    return out


# stemmed views of the word lists (tokens are always stemmed)
STOP_S = {stem(w) for w in STOP} | STOP
GENERIC_S = {stem(w) for w in GENERIC} | GENERIC
CONVERSATIONAL_S = {stem(w) for w in CONVERSATIONAL} | CONVERSATIONAL
SYN = {stem(k): [stem(x) for x in v] for k, v in SYNONYMS.items()}


# Generic banking concepts (not this site's product names - those are learned from page titles,
# headings and table captions at ingest, so new products are covered automatically). A question with
# none of these, no learned topic word and no rare name the site uses is out of scope.
DOMAIN = {stem(w) for w in """
rate rates apr apy yield dividend dividends interest account accounts checking savings saving certificate
certificates cd cds ira iras roth loan loans mortgage mortgages refinance refi heloc equity card cards visa
debit credit atm atms branch branches lobby drive hours hour open close closed closing member members membership
join fee fees deposit deposits withdraw withdrawal transfer transfers wire routing aba swift pin statement statements
online mobile app wallet wallets pay payment payments check checks insurance auto car truck vehicle vehicles boat rv
motorcycle home homebuyer business money market balance balances minimum tier term phone number call address
contact located location locations headquartered headquarters employees employee counties county award grant
program programs scholarship notary safe coin overdraft budget jumbo cash advance late limit apply application
borrow lend lending bank banking teller service services link url website locator
""".split()}