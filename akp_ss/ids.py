"""AKP / abbreviated ids (notes section 3). Python gives every element its id just before the AKP is saved.

The models keep working with their own short working ids (A_SALE, G_FORMS, LE_SOG, F_SUPPLY_FORM ...), so the
prompts, the judge, the comparison and the SAME check are unchanged. Before saving, every id is rewritten
everywhere in the table (ELEMENT, rules, members, expr, feeds, facts ...):

    A / G / LE / PD / OR / CT / TC   <MKP id>_<TYPE>_<acronym>     M_SIAFOS_A_SIAS   (A_REF -> A)
    FACTS                            F_<acronym of the fact name>  F_FOS             (no MKP prefix)

The acronym comes from the row's `name` (3-7 words) when the model gave one, else from the words of the model's
own working id (A_SALE_BY_PERSON -> SBP). It is at most 10 characters (a longer one is cut and noted).
A fact that is already in the fact registry keeps its id; a new fact whose name means the same as a registered
fact with that acronym reuses it (fact flow: one fact, one id); a different fact with the same acronym gets a number.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import kp_ids

from transport import get_field

from .parse import field_list, rfield

ID_TYPES = {"A": "A", "A_REF": "A", "G": "G", "LE": "LE", "PD": "PD", "OR": "OR", "CT": "CT", "TC": "TC", "LK": "LK"}
_STOP = {"the", "of", "whether", "is", "a", "an", "and", "to", "for", "in", "by", "on", "or", "does", "has", "have"}


def norm_name(name: str) -> str:
    """A fact name without case, small words and plural s, word order kept:
    'Date Of Birth' and 'dates of birth' -> 'date birth'."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(name or "").lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


def _words_of(working_id: str) -> str:
    """A_SALE_BY_PERSON -> 'SALE BY PERSON' (the prefix is dropped)."""
    parts = str(working_id or "").split("_")
    return " ".join(parts[1:] if len(parts) > 1 else parts)


def _acr(text: str, notes: List[str], label: str) -> str:
    acr = kp_ids.acronym(text)
    if len(acr) > kp_ids.MAX_LEN:
        notes.append(f"ID SHORTENED: {label} acronym {acr} cut to {acr[:kp_ids.MAX_LEN]}")
        acr = acr[:kp_ids.MAX_LEN]
    return acr


def _source(row: dict) -> str:
    name = rfield(row, "name").strip('"\' ')
    return name if kp_ids.words(name) else _words_of(row["ELEMENT"])


def _link_tail(row: dict) -> str:
    """The id part of an LK row: its relation word in capitals (AMENDS, OVERRIDES, REFERS), at most 10 letters;
    LINK when the row has no relation. M_IOSFC_LK_AMENDS, not LK_T_CGSTA17_S7_SS1_Cb_AMD1."""
    rel = re.sub(r"[^A-Z0-9]", "", rfield(row, "relation").upper())
    return rel[:10] or "LINK"


def abbreviate(rows: List[dict], mkp_id: str, known_facts: Optional[Dict[str, dict]] = None) -> Tuple[Dict[str, str], List[str]]:
    """Rewrite the ids of one AKP table in place. Returns (working id -> abbreviated id, notes).

    known_facts: fact_id -> {"name", "type", "aliases", "entity", "options"} of the fact registry (all_ids())."""
    from .factcheck import gate_same                             # here: factcheck imports this module
    known = dict(known_facts or {})
    if kp_ids.kind_of(mkp_id) != "MKP":
        raise kp_ids.IdError(f"AKP needs an abbreviated MKP id, got {mkp_id!r}")
    mapping: Dict[str, str] = {}
    notes: List[str] = []
    used: set = set()
    prefix = mkp_id + "_"

    # elements of this MKP
    for r in rows:
        kind = ID_TYPES.get(r["TYPE"])
        old = r["ELEMENT"]
        if not kind or old in mapping:
            continue
        if old.startswith(prefix) and kp_ids.kind_of(old) == kind and old not in used:
            new = old                                            # already abbreviated (re-save)
        elif kind == "LK":                                         # a link is named by its relation: _LK_AMENDS
            new = kp_ids.with_number(f"{mkp_id}_LK_{_link_tail(r)}", used)
        else:
            new = kp_ids.with_number(f"{mkp_id}_{kind}_{_acr(_source(r), notes, old)}", used)
        used.add(new)
        mapping[old] = new

    # facts: one id in every law
    by_norm: Dict[str, List[Tuple[str, bool]]] = {}
    for fid, f in sorted(known.items()):
        by_norm.setdefault(norm_name(f.get("name")), []).append((fid, False))
        for nm in f.get("aliases") or []:                           # a saved alias means the same fact
            by_norm.setdefault(norm_name(nm), []).append((fid, True))
    for r in rows:
        if r["TYPE"] != "FACTS":
            continue
        old = r["ELEMENT"]
        if old in mapping:
            continue
        if old in known and kp_ids.kind_of(old) == "F":
            mapping[old] = old                                   # reused from the registry as it is
            continue
        name = rfield(r, "name").strip('"\' ') or _words_of(old)
        base = "F_" + _acr(name, notes, old)
        # same name (or a saved alias) -> reuse that id, but only when Python's SAME gate passes: same type,
        # same entity, options fit. A same name with another type is a different fact (its own numbered id).
        hit = next((f for f, alias in by_norm.get(norm_name(name), [])
                    if (alias or kp_ids.strip_number(f, base)) and not gate_same(r, known[f])), None)
        if hit:
            new = hit                                            # same fact already registered: reuse its id
        else:
            new = kp_ids.with_number(base, set(known) | used)
            known[new] = {"name": name, "type": rfield(r, "type"), "entity": get_field(r, "entity"),
                          "options": field_list(r, "options")}
            by_norm.setdefault(norm_name(name), []).append((new, False))
        used.add(new)
        mapping[old] = new

    rewrite(rows, mapping)
    return mapping, notes


def rewrite(rows: List[dict], mapping: Dict[str, str]) -> None:
    """Replace every working id by its abbreviated id, as a whole token, in ELEMENT and RESULTS."""
    changes = {k: v for k, v in mapping.items() if k != v}
    if not changes:
        return
    pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(k) for k in sorted(changes, key=len, reverse=True))
                     + r")(?![A-Za-z0-9_])")
    sub = lambda s: pat.sub(lambda m: changes[m.group(1)], s)
    for r in rows:
        r["ELEMENT"] = changes.get(r["ELEMENT"], r["ELEMENT"])
        if isinstance(r.get("RESULTS"), str):
            r["RESULTS"] = sub(r["RESULTS"])
        if r.get("_facts"):
            r["_facts"] = [changes.get(f, f) for f in r["_facts"]]


def check(rows: List[dict], mkp_id: str) -> List[str]:
    """Python gate after abbreviate(): every element and fact id follows the rule and is unique in the MKP."""
    errs, seen = [], {}
    for r in rows:
        kind = ID_TYPES.get(r["TYPE"]) or ("F" if r["TYPE"] == "FACTS" else None)
        if not kind:
            continue
        el = r["ELEMENT"]
        errs += [f"ID: {e}" for e in kp_ids.check(el, kind)]
        if kind != "F" and not el.startswith(mkp_id + "_"):
            errs.append(f"ID: {el} does not start with its MKP id {mkp_id}")
        if el in seen and seen[el] != r["TYPE"]:
            errs.append(f"ID: {el} used by a {seen[el]} and a {r['TYPE']} row")
        seen.setdefault(el, r["TYPE"])
    return errs


def merge_same_tests(rows: List[dict]) -> List[str]:
    """Duplicates inside one law (notes section 7, Python, no LLM): two LOW atoms that read the same facts with the
    same rule always have the same truth value, so the second one is merged into the first (its references move to
    the first; its phrases stay with the first). A merge that would leave a group with fewer than two members is not
    made. Returns notes."""
    from transport import get_field
    from .parse import field_list
    norm = lambda x: " ".join(str(x or "").replace('"', "").split()).upper()
    first: Dict[tuple, str] = {}
    notes: List[str] = []
    for el in [x["ELEMENT"] for x in rows if x["TYPE"] == "A"]:
        r = next((x for x in rows if x["ELEMENT"] == el and x["TYPE"] == "A"), None)
        if r is None or get_field(r, "order").upper() != "LOW" or not get_field(r, "rule"):
            continue
        if get_field(r, "valid_from") or get_field(r, "valid_to"):
            continue
        key = (tuple(sorted(field_list(r, "facts"))), norm(get_field(r, "rule")))
        if not key[0]:
            continue
        keep = first.setdefault(key, r["ELEMENT"])
        if keep == r["ELEMENT"]:
            continue
        dup = r["ELEMENT"]
        trial = [dict(x) for x in rows if x is not r]
        rewrite(trial, {dup: keep})
        bad = False
        for g in trial:
            if g["TYPE"] == "G":
                members = field_list(g, "members")
                uniq = list(dict.fromkeys(members))
                if len(uniq) < 2 and len(members) >= 2:
                    bad = True
                    break
                if uniq != members:
                    g["RESULTS"] = re.sub(r"members:\s*\[[^\]]*\]", f"members: [{', '.join(uniq)}]", g["RESULTS"], count=1)
        if bad:
            notes.append(f"SAME TEST: {dup} reads the same facts with the same rule as {keep} (kept - one group)")
            continue
        rows[:] = trial
        notes.append(f"SAME TEST: {dup} merged into {keep} (same facts, same rule)")
    return notes
