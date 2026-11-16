"""AKP / fact check - fact flow layer 3 (notes section 8): is a new fact really new?

After the AKP of one MKP is approved, every fact it made that is not in the fact registry is compared with ALL
registered facts (nearest by meaning, similarity >= fact_check_min_similarity). One model call per MKP answers per
new fact SAME (use the existing id) / DERIVED (worked out from existing facts) / NEW. Python checks every SAME before
it is used: same type, same entity, and the new fact's options must fit the existing options - otherwise it stays NEW.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Tuple

from transport import get_field

from .facts import canon_type
from .ids import rewrite
from .parse import field_list, rfield


def new_facts(rows: List[dict], known: Dict[str, dict]) -> List[dict]:
    return [r for r in rows if r["TYPE"] == "FACTS" and r["ELEMENT"] not in known]


def concept_of(r: dict) -> str:
    from .facts import _concept
    return _concept(r)


def card(r: dict) -> str:
    """The text a fact is embedded with - the same form as the registry (name - nl)."""
    return f"{get_field(r, 'name') or r['ELEMENT']} - {get_field(r, 'nl')}".strip(" -")


def request(source: dict, rows: List[dict], facts: List[dict], candidates: Dict[str, List[dict]]) -> dict:
    users = {}
    for r in rows:
        if r["TYPE"] in ("A", "A_REF"):
            for f in field_list(r, "facts"):
                users.setdefault(f, []).append({"atom": r["ELEMENT"], "statement": get_field(r, "statement"),
                                                "rule": get_field(r, "rule")})
    return {"source": source,
            "new_facts": [{"fact": r["ELEMENT"], "name": get_field(r, "name"), "concept": concept_of(r),
                           "entity": get_field(r, "entity"),
                           "type": rfield(r, "type"), "unit": get_field(r, "unit"), "options": field_list(r, "options"),
                           "nl": get_field(r, "nl"), "used_by": users.get(r["ELEMENT"], [])} for r in facts],
            "candidates": candidates}


def parse(raw: str) -> List[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
    items = obj.get("facts")
    if not isinstance(items, list):
        raise ValueError("facts must be a list")
    return [i for i in items if isinstance(i, dict)]


def _opts(v) -> List[str]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [x.strip() for x in v.strip("[]").split(",")]
    return [str(x).strip().strip("'\"").upper() for x in (v or []) if str(x).strip()]


NEW_OPTION = "NEW"                      # option_map value: a real new value of the same fact, added to it


def option_map(answer: dict) -> Dict[str, str]:
    """The fact-check model's map of the new fact's options to the existing fact's options:
    {"AGREED_TO_BE_PAID": "PAYABLE", "BARTER_IN_KIND": "NEW"} - upper case, quotes removed."""
    raw = answer.get("option_map") if isinstance(answer, dict) else None
    if not isinstance(raw, dict):
        return {}
    clean = lambda x: str(x or "").strip().strip("'\"").upper()
    return {clean(k): clean(v) for k, v in raw.items() if clean(k) and clean(v)}


def gate_same(new: dict, old: dict, omap: Dict[str, str] = None) -> str:
    """'' when the new fact may be read as the existing one, else why not. omap: the model's option map - an
    option worded differently (AGREED_TO_BE_PAID -> PAYABLE) or a real new value (-> NEW, added to the fact).
    Refused only for an option that is neither among the existing options nor mapped."""
    if canon_type(rfield(new, "type")) != canon_type(old.get("type") or ""):
        return f"type {rfield(new, 'type')} is not {old.get('type')}"
    ne, oe = get_field(new, "entity").upper(), str(old.get("entity") or "").upper()
    if ne and oe and ne != oe:
        return f"entity {ne} is not {oe}"
    no, oo, omap = _opts(field_list(new, "options")), _opts(old.get("options")), omap or {}
    bad = sorted(o for o in set(no) - set(oo) if not (omap.get(o) == NEW_OPTION or omap.get(o) in oo))
    if no and oo and bad:
        return f"options {bad} are not among {oo} (and not mapped)"
    wrong = sorted(f"{k}->{v}" for k, v in omap.items() if v != NEW_OPTION and v not in oo)
    if oo and wrong:
        return f"option map {wrong} points to options {oo} does not have"
    return ""


def map_options(rows: List[dict], fid: str, omap: Dict[str, str]) -> None:
    """Rewrite the new fact's option values to the existing fact's (rules of the rows that read it, and the
    FACTS row's options), so the atoms test the kept fact's own values. NEW options stay as they are."""
    change = {k: v for k, v in omap.items() if v != NEW_OPTION and k != v}
    if not change:
        return
    for r in rows:
        if r is None:
            continue
        if r["TYPE"] == "FACTS" and r["ELEMENT"] == fid:
            opts = [change.get(o, o) for o in _opts(field_list(r, "options"))]
            r["RESULTS"] = re.sub(r"options:\s*\[[^\]]*\]", "options: [" + ", ".join(dict.fromkeys(opts)) + "]",
                                  r["RESULTS"], count=1)
        elif fid in (r.get("RESULTS") or ""):
            for k, v in change.items():
                r["RESULTS"] = re.sub(r"(['\"])" + re.escape(k) + r"\1", lambda m: m.group(1) + v + m.group(1),
                                      r["RESULTS"], flags=re.I)


def apply(rows: List[dict], answers: List[dict], candidates: Dict[str, List[dict]], known: Dict[str, dict],
          drop_duplicates: bool = True) -> List[dict]:
    """Use the answers on the table in place. Returns the decisions (saved with the AKP attempt in akp_cache).
    drop_duplicates=False keeps every row in its place (the save step: row positions carry the embeddings)."""
    by_id = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "FACTS"}
    decisions, same = [], {}
    for a in answers:
        fid = str(a.get("fact", "")).strip()
        row = by_id.get(fid)
        if row is None or fid in known:
            continue
        cands = {c["fact_id"]: c for c in candidates.get(fid, [])}
        decision = str(a.get("decision", "")).upper()
        d = {"fact": fid, "name": get_field(row, "name"), "decision": decision, "existing": "", "reason": a.get("reason", "")}
        if decision == "SAME":
            old = str(a.get("existing", "")).strip()
            omap = option_map(a)
            why = "not a candidate" if old not in cands else gate_same(row, cands[old], omap)
            if why:
                d.update(decision="NEW", reason=f"SAME {old} refused by Python: {why}")
            else:
                d["existing"] = old
                same[fid] = old
                if omap:                     # options worded differently: the atoms now test the kept values
                    map_options(rows, fid, omap)
                    d["option_map"] = omap
        elif decision == "DERIVED":
            srcs = [x for x in (a.get("derived_from") or []) if x in cands or x in known]
            formula = " ".join(str(a.get("derivation") or "").split()).replace(";", ",").replace('"', "'")
            if srcs and formula:
                row["RESULTS"] = (row["RESULTS"].rstrip("; ") + f"; derived_from: [{', '.join(srcs)}]; "
                                  f'derivation: "{formula[:300]}"')
                d["existing"] = ", ".join(srcs)
            else:
                d.update(decision="NEW", reason="DERIVED without known source facts or formula - kept NEW")
        else:
            d["decision"] = "NEW"
        decisions.append(d)
    if same:
        rewrite(rows, same)          # the atoms read the existing fact; its row now updates that fact (+ alias)
    if same and drop_duplicates:
        seen = set()
        for i in [i for i, r in enumerate(rows) if r["TYPE"] == "FACTS"]:
            if rows[i]["ELEMENT"] in seen:
                rows[i] = None       # the table already listed that fact
            seen.add(rows[i]["ELEMENT"] if rows[i] else None)
        rows[:] = [r for r in rows if r is not None]
    return decisions


def name_match(rows: List[dict], known: Dict[str, dict], drop_duplicates: bool = True,
               ask: Dict[str, List[str]] = None) -> List[dict]:
    """Fact flow layer 2, BEFORE the model: a new fact whose name (or a saved alias) is the name of a registered
    fact takes that fact's id - only when Python's SAME gate passes (same type, same entity, options fit).
    A same name with another type stays new (it gets its own numbered id). Rewrites the table in place;
    drop_duplicates=False keeps every row in its place (the save step: row positions carry the embeddings).
    ask: filled with new fact -> registered ids that have its name, type and entity but other option words
    (PAYABLE / AGREED_TO_BE_PAID): the fact-check model decides those with an option map, not Python alone."""
    from .ids import norm_name
    by_norm: Dict[str, List[str]] = {}
    for fid, f in sorted(known.items()):
        for nm in [f.get("name")] + list(f.get("aliases") or []):
            if norm_name(nm):
                by_norm.setdefault(norm_name(nm), []).append(fid)
    same, decisions = {}, []
    for r in new_facts(rows, known):
        hits = list(dict.fromkeys(by_norm.get(norm_name(get_field(r, "name")), [])))
        refused = []
        for fid in hits:
            why = gate_same(r, known[fid])
            if not why:
                same[r["ELEMENT"]] = fid
                decisions.append({"fact": r["ELEMENT"], "name": get_field(r, "name"), "decision": "SAME",
                                  "existing": fid, "reason": "same name as a registered fact (Python, no model)"})
                break
            if ask is not None and not gate_same(r, {**known[fid], "options": []}):   # only the options differ
                ask.setdefault(r["ELEMENT"], []).append(fid)
                continue
            refused.append(f"{fid}: {why}")
        if hits and r["ELEMENT"] not in same and r["ELEMENT"] not in (ask or {}):
            decisions.append({"fact": r["ELEMENT"], "name": get_field(r, "name"), "decision": "NEW", "existing": "",
                              "reason": "same name, but " + "; ".join(refused)[:250]})
    if same:
        rewrite(rows, same)
    if same and drop_duplicates:
        seen = set()
        for i in [i for i, r in enumerate(rows) if r["TYPE"] == "FACTS"]:
            if rows[i]["ELEMENT"] in seen:
                rows[i] = None
            seen.add(rows[i]["ELEMENT"] if rows[i] else None)
        rows[:] = [r for r in rows if r is not None]
    return decisions


def check(rows: List[dict], known: Dict[str, dict], embed: Callable, nearest: Callable, ask: Callable,
          source: dict, since: set = None) -> Tuple[List[dict], int]:
    """Run layer 3 for one approved AKP table. nearest(vector, concept_vector) -> candidates; ask(request_dict) -> raw model answer.
    since: only candidates registered after this set of fact ids (the second check at save time: facts another
    worker registered while this MKP was split - clauses of one section run at the same time).
    Returns (decisions, number of facts sent)."""
    to_model: Dict[str, List[str]] = {}
    by_name = name_match(rows, known, drop_duplicates=since is None, ask=to_model)   # layer 2 first
    facts = new_facts(rows, known)
    candidates: Dict[str, List[dict]] = {}
    for fid, hits in to_model.items():          # same name, other option words: the model maps the options
        candidates[fid] = [dict(known[h], fact_id=h, similarity=1.0, found_by="same name") for h in hits]
    for r in facts:
        try:
            v = embed(card(r))
        except Exception:
            v = None
        try:                                    # the concept is searched too, like an atom's
            cv = embed(concept_of(r)) if concept_of(r) else None
        except Exception:
            cv = None
        found = [c for c in nearest(v, cv) if c["fact_id"] != r["ELEMENT"] and (since is None or c["fact_id"] not in since)]
        have = {c["fact_id"] for c in candidates.get(r["ELEMENT"], [])}
        found = candidates.get(r["ELEMENT"], []) + [c for c in found if c["fact_id"] not in have]
        if found:
            candidates[r["ELEMENT"]] = found
    asked = [r for r in facts if r["ELEMENT"] in candidates]
    later: List[dict] = []
    if asked:
        later = apply(rows, parse(ask(request(source, rows, asked, candidates))), candidates, known,
                      drop_duplicates=since is None)
    if since is None:
        done = {d["fact"] for d in later}
        later += [{"fact": r["ELEMENT"], "name": get_field(r, "name"), "decision": "NEW", "existing": "",
                   "reason": "no registered fact is near in meaning" if r["ELEMENT"] not in candidates
                   else "no answer from the model - kept NEW"} for r in facts if r["ELEMENT"] not in done]
    # one decision per fact: the model's answer comes after a refused match by name
    merged = {d["fact"]: d for d in by_name}
    for d in later:
        if d["decision"] != "NEW" or d["fact"] not in merged:
            merged[d["fact"]] = d
    return list(merged.values()), len(asked)
