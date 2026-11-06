"""Contradiction detection and resolution.

Three kinds of contradiction are reported, each with the value we believe and why:

1. Stale copies inside the newest snapshot - a table on a calculator / landing / guidance page that
   shows the same product rows as the official rate sheet but with different numbers
   ("60 Months auto APR: calculator 5.34% vs rate sheet 5.39%").  Winner: the authoritative page
   (big authority gap) or the newer "effective as of" date.
2. Values that changed between snapshots for the same row of the same official table
   ("10 Month Fixed Jumbo Special APY: 3.90% (May) -> 4.00% (Sep)").  Winner: the newest snapshot.
3. Repeated factual statements in dated press releases / articles that disagree over time
   ("22 branch office locations" vs "24 branch office locations").  Winner: the newest dated page.

Tier differences (balance / term / loan-amount rows), rate-vs-APY columns and personal-vs-business
products are different facts, not conflicts: rows are only compared when their labels match.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .parse import TableRow

ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth".split()
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _rate_values(r: TableRow, engine_mod) -> list[tuple[str, str]]:
    """(column, value) pairs that carry a rate (APY / APR / dividend / rate)."""
    out = []
    for h, v in r.value_cells():
        if not v or v in ("—", "N/A") or engine_mod.DROP_VALUE.search(h) or re.search(r"min", h, re.I):
            continue
        if PCT.search(v):
            sh = engine_mod.short_header(h)
            kind = "APR" if sh.startswith("APR") else sh        # "APR (as low as)" and "APR" are the same measure
            out.append((kind or "rate", v.strip()))
    return out


def _mask(r: TableRow, E) -> str:
    return re.sub(r"\d[\d,.]*", "#", E.norm_label(r))


def _nums(r: TableRow, E) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", E.norm_label(r))}


def _pair(s, cands, E):
    """The authoritative row that a stale row is a copy of: same masked label and term, and the
    closest numbers (model-year range overlap / same loan amount)."""
    if not cands:
        return None
    sm = E.row_terms(s.obj)[0]
    cands = [g for g in cands if E.row_terms(g.obj)[0] == sm]
    if not cands:
        return None
    srange = E.row_model_years(s.obj)
    if srange:
        def ov(g):
            gr = E.row_model_years(g.obj)
            return (min(srange[1], gr[1]) - max(srange[0], gr[0])) if gr else -99
        best = max(cands, key=ov)
        return best if ov(best) > 0 else None
    sn = _nums(s.obj, E)
    best = max(cands, key=lambda g: len(sn & _nums(g.obj, E)))
    if sn and not (sn & _nums(best.obj, E)) and len(cands) > 1:
        return None
    return best


def _norm_val(v: str) -> str:
    m = PCT.search(v)
    if m:
        return f"{float(m.group(1)):.3f}%"
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", v)
    if m:
        return f"${float(m.group(1).replace(',', '')):.2f}"
    return v.strip().lower()


def _show(v: str) -> str:
    """Display a cell value without trailing APR/APY words: '5.39% APR' -> '5.39%'."""
    m = PCT.search(v)
    if m:
        return m.group(1) + "%"
    m = re.search(r"\$\s?[\d,]+(?:\.\d+)?", v)
    return m.group(0).replace(" ", "") if m else v


def _product(r: TableRow) -> str:
    cap = r.caption.split(" / ")[-1].strip() if r.caption else ""
    return cap


def build_conflicts(engine) -> dict:
    from . import engine as E

    out: list[dict] = []
    by_row: dict[str, dict] = defaultdict(dict)
    if not engine.snaps:
        return {"by_row": by_row, "list": out, "timelines": {}}
    latest = engine.snaps[-1]

    # ------------------------------------------------------------------ 1. stale copies in the newest snapshot
    keys = list(latest.tables.keys())
    sigs = {k: engine.table_signature(latest.tables[k]) for k in keys}
    by_label: dict[str, list] = defaultdict(list)
    for k, sg in sigs.items():
        for lab in sg:
            by_label[lab].append(k)
    merged: dict[tuple, dict] = {}
    for key in keys:
        near = list(dict.fromkeys(k for lab in sigs[key] for k in by_label[lab]))
        near.sort(key=lambda k: (-latest.tables[k][0].page.authority, k))   # cite the most authoritative page
        winner = engine.superseding(latest, key, near) if len(near) > 1 else None
        if not winner:
            continue
        good_by_mask = defaultdict(list)
        for g in latest.tables[winner]:
            good_by_mask[_mask(g.obj, E)].append(g)
        for s in latest.tables[key]:
            g = _pair(s, good_by_mask.get(_mask(s.obj, E), []), E)
            if g is None:
                continue
            sv, gv = dict(_rate_values(s.obj, E)), dict(_rate_values(g.obj, E))
            cols = [c for c in sv if c in gv and _norm_val(sv[c]) != _norm_val(gv[c])]
            if not cols:
                continue
            mk = (E.norm_label(g.obj), tuple(_norm_val(sv[c]) for c in cols))
            stale_vals = [_show(sv[c]) for c in cols]
            good_vals = [_show(gv[c]) for c in cols]
            reason = _stale_reason(s, g)
            if mk in merged:                      # same stale value on another out-of-date page
                merged[mk]["sources"].insert(-1, (s.page.url, latest.snapshot_id))
                continue
            merged[mk] = {
                "topic": f"{_product(g.obj) or 'rate'} — {E.row_text(g.obj)} ({', '.join(cols)})",
                "values": stale_vals + good_vals,
                "sources": [(s.page.url, latest.snapshot_id), (g.page.url, latest.snapshot_id)],
                "resolution": " / ".join(f"{v} {c}" for v, c in zip(good_vals, cols)),
                "reason": reason + f"; {s.page.url} is out of date",
                "kind": "stale_page",
            }
            by_row[latest.snapshot_id][(g.page.is_business, E.norm_label(g.obj))] = (
                s.page.url, ", ".join(stale_vals), g.page.url, reason)
    out.extend(merged.values())

    # ------------------------------------------------------------------ 2. changes across snapshots (official tables)
    history: dict[tuple, dict] = defaultdict(lambda: defaultdict(set))   # row -> snapshot -> {values}
    where: dict[tuple, str] = {}
    for snap in engine.snaps:
        for (url, tid), rows in snap.tables.items():
            if rows[0].page.authority < 0.8 or rows[0].page.carried:
                continue                           # official tables only (rate sheets, product pages)
            for u in rows:
                vals = _rate_values(u.obj, E)
                if not vals:
                    continue
                k = (u.page.is_business, history_key(u.obj, E))
                history[k][(snap.taken_on, snap.snapshot_id)].add(tuple((c, _norm_val(v), _show(v)) for c, v in vals))
                where.setdefault(k, url)
    timelines: dict[tuple, list] = {}
    seen = set()
    for k, per_snap in sorted(history.items(), key=lambda kv: (kv[0][0], kv[0][1])):   # personal first
        if any(len(v) > 1 for v in per_snap.values()):
            continue                               # same label, different values in one snapshot: ambiguous
        seq = []
        for (d, sid), vals in sorted(per_snap.items()):
            v = next(iter(vals))
            if not seq or [x[1] for x in seq[-1][2]] != [x[1] for x in v]:
                seq.append((d, sid, v))
        if len(seq) < 2:
            continue
        timelines[k] = [(d, [(x[0], x[2]) for x in v]) for d, _, v in seq]
        sig = (k[1], tuple(tuple(x[1] for x in v) for _, _, v in seq))
        if sig in seen:
            continue
        seen.add(sig)
        cur = seq[-1]
        hist = "; ".join(f"{d:%m/%d/%Y}: " + ", ".join(f"{x[2]} {x[0]}" for x in v) for d, _, v in seq)
        values = list(dict.fromkeys(x[2] for _, _, v in seq for x in v))
        out.append({
            "topic": f"{'Business ' if k[0] else ''}{k[1]} ({', '.join(x[0] for x in cur[2])})",
            "values": values,
            "sources": [(where[k], sid) for _, sid, _ in seq],
            "resolution": " / ".join(f"{x[2]} {x[0]}" for x in cur[2]),
            "reason": f"changed between snapshots ({hist}); the newest snapshot is current",
            "kind": "changed_over_time",
        })

    # ------------------------------------------------------------------ 3. repeated statements in dated articles
    out.extend(_dated_statement_conflicts(latest))
    return {"by_row": by_row, "list": out, "timelines": timelines}


def history_key(r: TableRow, E) -> str:
    """A row identity that survives re-formatting between crawls. Labels that name the product
    ('10 Month Fixed Jumbo Certificate Special') stand alone; bare tier labels ('$5,000 - $24,999.99')
    are prefixed with the product words of the table caption ('1st rate checking')."""
    label = E.norm_label(r)
    words = re.sub(r"[\d$,.+\-–|%]|months?|model|year|or less|and older", " ", label).split()
    if len(words) >= 2:
        return label
    cap = [w for w in re.findall(r"[a-z0-9]+", _product(r).lower())
           if w not in ("account", "accounts", "rates", "rate", "the")]
    return (cap[0] if cap else "") + " — " + label


def _stale_reason(s, g) -> str:
    sp, gp = s.page, g.page
    se, ge = s.obj.effective, g.obj.effective
    bits = []
    if gp.authority > sp.authority:
        bits.append(f"{gp.category.replace('_', ' ')} page outranks {sp.category.replace('_', ' ')} page")
    if se and ge and ge > se:
        bits.append(f"effective {ge:%m/%d/%Y} is newer than {se:%m/%d/%Y}")
    return "; ".join(bits) or "official rate table"


BOILER_RE = re.compile(r"(?<![\w$.])\$?(\d[\d,]*(?:\.\d+)?)\+?\s+((?:billion\s+)?[A-Za-z]+(?:\s+[A-Za-z]+){0,2})", re.I)
ORDINAL_RE = re.compile(r"\b(" + "|".join(ORDINALS) + r")\s+(time|consecutive year|year in a row)\s+(?:that\s+)?([A-Z][A-Za-z]+)?")
SKIP_TAIL = {"percent", "of", "and", "to", "in", "a", "the", "or", "years", "months", "days", "min", "minutes",
             "am", "pm", "p", "a"}


def _dated_statement_conflicts(snap) -> list[dict]:
    """Facts repeated in dated press releases that drift over time: the 'About Dupaco' boilerplate
    ('22 branch office locations', '118 counties') and running counts ('the fifth time Forbes ...')."""
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for u in snap.passages:
        p = u.page
        if not p.date or p.category not in ("news", "blog"):
            continue
        text = u.obj.text
        if u.obj.heading == "About":
            for m in BOILER_RE.finditer(text):
                num = m.group(1).rstrip(",")
                tail = m.group(2).lower()
                if tail.split()[0] in SKIP_TAIL or re.fullmatch(r"(19|20)\d\d", num):
                    continue
                lead = " ".join(text[:m.start()].split()[-4:])
                groups[f"# {tail}"][num].append((p.date, p.url, lead))
        for m in ORDINAL_RE.finditer(text):
            if m.group(3):
                groups[f"… {m.group(2)} {m.group(3).lower()}"][m.group(1).lower()].append((p.date, p.url, ""))
    out = []
    for key, by_val in groups.items():
        pages = sum(len(v) for v in by_val.values())
        if len(by_val) < 2 or (pages < 3 and not key.startswith("…")):
            continue
        newest = max(((max(o[0] for o in obs), v) for v, obs in by_val.items()))
        cur_date, cur_val = newest
        others = sorted(by_val, key=lambda v: max(o[0] for o in by_val[v]), reverse=True)
        lead = max(by_val[cur_val])[2]
        topic = (f"Dupaco press-release fact: '{lead} … {key[2:]}'" if key.startswith("#")
                 else f"How many times: '{key[2:]}' (Dupaco recognition count)")
        out.append({
            "topic": topic,
            "values": others,
            "sources": [(max(by_val[v])[1], snap.snapshot_id) for v in others],
            "resolution": cur_val,
            "reason": (f"the newest dated statement ({cur_date:%B %d, %Y}) says '{cur_val}'; older press "
                       f"releases repeat earlier values"),
            "kind": "dated_statement",
        })
    return out
