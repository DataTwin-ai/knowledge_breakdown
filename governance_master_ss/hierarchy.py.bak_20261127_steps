"""GOVERNANCE MASTER / fact hierarchy (2026-11-21): which fact to get first, per law and per TSG. Pure Python, no
database, no LLM - read from a governance master rulebook (the yaml) only; the yaml itself is not changed.

One unit = one legal expression (LE: one outcome of one law). A compound with two LEs (7(2): clause a OR clause b)
is two units - each clause is its own way to the result, so one clause's gate does not stop the other.
Levels, from the LE formula (expr):
  1     gate - a simple (python / LOW) check joined by AND at the top of the formula: if it is false this outcome
        fails, nothing below needs asking (also each member of an ALL group used that way, NOT x, and an ANY group
        used that way whose members all read the same one fact - that fact decides the group)
  2     a simple check inside an OR / bracket, when the top of the formula is OR, or an ANY group at the top whose
        members read different facts (the group gates, but more than one fact may be needed)
  3     a simple check in a law this one refers to (product / self or cross reference), or an ANY group deeper down
  LAST  a fact only a HIGH / heavy check reads (free text such as a description): asked last
Inside a level: cheaper source first (DOCUMENT / DERIVED, then LOOKUP, then ASK - Config/fact_kinds.json), then the
fact used by more laws of this TSG.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

LAST = 4
LEVEL_NAME = {1: "1 gate", 2: "2", 3: "3", LAST: "LAST (heavy check only)"}
SOURCE_COST = {"DOCUMENT": 1, "DERIVED": 1, "LOOKUP": 2, "ASK": 3}
_TOKEN = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bNOT\b|[A-Za-z_][A-Za-z0-9_]*")
_FACT = re.compile(r"\bF_[A-Z0-9_]+\b")


def parse(expr: str):
    """'A AND (B OR NOT C)' -> ('AND', [('ID','A'), ('OR', [('ID','B'), ('NOT', ('ID','C'))])])."""
    toks = _TOKEN.findall(expr or "")
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def p_or():
        items = [p_and()]
        while peek() == "OR":
            take()
            items.append(p_and())
        return items[0] if len(items) == 1 else ("OR", items)

    def p_and():
        items = [p_not()]
        while peek() == "AND":
            take()
            items.append(p_not())
        return items[0] if len(items) == 1 else ("AND", items)

    def p_not():
        t = peek()
        if t == "NOT":
            take()
            return ("NOT", p_not())
        if t == "(":
            take()
            node = p_or()
            if peek() == ")":
                take()
            return node
        if t is None or t == ")":
            return ("ID", "")
        return ("ID", take())

    return p_or() if toks else ("ID", "")


def _facts_of(pred: dict) -> List[str]:
    return list(dict.fromkeys(_FACT.findall(str(pred.get("rule") or "")) + [f for f in pred.get("grounding") or []
                                                                             if str(f).startswith("F_")]))


def _simple(pred: dict) -> bool:
    return str(pred.get("order", "")).upper() == "LOW" and str(pred.get("handler", "python")) == "python"


def law_levels(rb: dict, le_ids: List[str]) -> Dict[str, int]:
    """fact -> its lowest level in this law (the LEs of one compound)."""
    preds, groups, outs = rb.get("predicates") or {}, rb.get("groups") or {}, rb.get("outcomes") or {}
    made_by = {}
    for c in rb.get("compounds") or []:
        for p in c.get("produces") or []:
            for le in p.get("from") or []:
                made_by[p.get("product")] = le
    out: Dict[str, int] = {}
    seen = set()

    def put(f: str, lvl: int):
        out[f] = min(out.get(f, LAST), lvl)

    def ident(i: str, gate: bool, floor: int):
        if not i or i in seen and floor >= 3:
            return
        if i in preds:
            p = preds[i]
            lvl = LAST if not _simple(p) else max(floor, 1 if gate else 2)
            for f in _facts_of(p):
                put(f, lvl)
        elif i in groups:
            g = groups[i]
            logic = str(g.get("logic", "")).upper()
            refer = all(m in made_by or m in outs for m in g.get("members") or [])
            members = list(g.get("members") or [])
            if refer:
                for m in members:
                    ident(m, False, 3)
            elif logic == "ANY":
                one = {tuple(_facts_of(preds[m])) for m in members if m in preds}
                same = len(one) == 1 and all(m in preds and _simple(preds[m]) for m in members)
                for m in members:                      # the group gates when it sits at the top
                    ident(m, gate and same, floor if gate else 3) if (gate and floor < 3) else ident(m, False, 3)
            else:                                       # ALL: each member is as strong as the group itself
                for m in members:
                    ident(m, gate, floor)
        elif i in made_by or i in outs:                 # a product / outcome of another law: that law's facts
            seen.add(i)
            le = made_by.get(i, i)
            if le in outs:
                node(parse(str(outs[le].get("expr") or "")), True, 3)

    def node(n, gate: bool, floor: int):
        kind = n[0]
        if kind == "ID":
            ident(n[1], gate, floor)
        elif kind == "NOT":
            node(n[1], gate, floor)
        elif kind == "AND":
            for x in n[1]:
                node(x, gate, floor)
        else:                                            # OR: no single branch decides alone
            for x in n[1]:
                node(x, False, max(floor, 2) if floor < 3 else floor)

    for le in le_ids:
        if le in outs:
            node(parse(str(outs[le].get("expr") or "")), True, 1)
    return out


def source_of(fact: str, kinds: dict, derived: Optional[dict] = None) -> str:
    given = (kinds or {}).get(fact) or {}
    k = str(given.get("kind") or ("DERIVED" if (derived or {}).get(fact) else "DOCUMENT")).upper()
    return k if k in SOURCE_COST else "DOCUMENT"


def hierarchy(rb: dict, kinds: Optional[dict] = None, derived: Optional[dict] = None) -> Tuple[List[dict], List[dict]]:
    """-> (TSG rows: one per fact in ask order, law rows: one per fact per law in ask order)."""
    facts = rb.get("facts") or {}
    per_law: Dict[str, Dict[str, int]] = {}
    question: Dict[str, str] = {}
    module: Dict[str, str] = {}
    outs = rb.get("outcomes") or {}
    for c in rb.get("compounds") or []:
        for le in c.get("includes") or []:
            per_law[le] = law_levels(rb, [le])
            question[le] = str(c.get("question") or "")
            module[le] = str((outs.get(le) or {}).get("module") or "")
    used: Dict[str, List[str]] = {}
    for law, lv in per_law.items():
        for f in lv:
            used.setdefault(f, []).append(law)

    def key(f: str, lvl: int):
        return (lvl, SOURCE_COST[source_of(f, kinds, derived)], -len({module[l] for l in used.get(f, [])}), f)

    law_rows: List[dict] = []
    gates: Dict[str, List[str]] = {}
    for law, lv in per_law.items():
        gates[law] = sorted([f for f, n in lv.items() if n == 1], key=lambda f: key(f, 1))
        for i, f in enumerate(sorted(lv, key=lambda f: key(f, lv[f])), 1):
            law_rows.append({"law": module[law], "outcome": law, "question": question[law], "order": i, "level": LEVEL_NAME[lv[f]], "fact": f,
                             "name": (facts.get(f) or {}).get("name", ""), "source": source_of(f, kinds, derived),
                             "asked_only_if": "" if lv[f] == 1 else " + ".join(g for g in gates[law] if g != f)})
    tsg_rows: List[dict] = []
    best = {f: min(per_law[l][f] for l in laws) for f, laws in used.items()}
    for i, f in enumerate(sorted(best, key=lambda f: key(f, best[f])), 1):
        cond = []                                       # per law: that law's gates (one law passing them is enough)
        if best[f] > 1:
            for law in used[f]:
                g = [x for x in gates[law] if x != f]
                if g:
                    cond.append(f"{module[law] or law}: {' + '.join(g)}")
        tsg_rows.append({"order": i, "level": LEVEL_NAME[best[f]], "fact": f, "name": (facts.get(f) or {}).get("name", ""),
                         "source": source_of(f, kinds, derived), "laws": len({module[l] for l in used[f]}),
                         "law_ids": ", ".join(dict.fromkeys(module[l] for l in used[f])),
                         "asked_only_if": " | ".join(list(dict.fromkeys(cond))[:8])})
    return tsg_rows, law_rows
