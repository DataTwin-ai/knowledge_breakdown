"""RELATIONSHIP / build: the links of one MKP as kp_relation rows. No LLM and no database here.

Input (read by store.py from the latest rows of the database):
    akp     the MKP's AKP rows: MKP, A, A_REF, G, LE, OR, CT, PD, TC, LK
    edges   akp_atom_fact: atom -> fact
    gkp     the TSG each element was grouped in (APPROVED only)
    gm      the governance masters (TSGs) that use this MKP
    names   fact names (akp_fact); same: for A_REF, the atom it is the SAME as

Two kinds of rows (column `link`):

DIRECT - how the law is built, one step each:
    ATOM               READS         FACT                the atom's rule tests the fact
    ATOM / GROUP / PD  MEMBER_OF     GROUP               member of a G group
    ATOM / GROUP / PD  USED_BY       LE                  in the LE expression
    LE                 PRODUCES      PD                  the product the LE gives
    OR                 OTHERWISE_OF  LE                  what happens when the LE fails
    CT                 TRIGGERED_BY  ATOM / GROUP / LE / FACT   when the review question is asked
    TC                 TESTS         LE                  a test case of the LE
    LE / PD / OR / CT / TC  PART_OF  MKP                 element of the MKP
    ATOM / GROUP / LE  GROUPED_IN    TSG                 the TSG GKP put it in
    TSG                BELONGS_TO    TG
    ATOM               SAME_AS       ATOM                the same atom in another MKP (SAME check)
    MKP                USED_IN       GOVERNANCE_MASTER   a governance master uses the MKP
    MKP                AMENDS ...    PROVISION           an amendment MKP and the provision it changes

DERIVED - fact first:
    GROUP / LE / PD / OR / CT / TC / MKP / TSG / GOVERNANCE_MASTER   NEEDS   FACT
    worked out from the DIRECT rows; `via` keeps the path (A_MADE > G_TIMING > LE_SIAFOS). An atom has no NEEDS row:
    its READS row already says it. "What needs this fact" = relation IN ('READS', 'NEEDS').

Ids: facts, TSGs, TGs, governance masters, provisions and MKPs keep their own id (the same in every law - that is
what joins the laws). Atom, group, LE, PD, OR, CT and TC ids are the abbreviated akp_id (M_SIAFOS_A_SIAS, unique in
all laws); an older short id that repeats across laws is kept as <MKP>::<element>.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

BUILD_VERSION = "6"                         # 4: abbreviated ids (notes section 3); 5: handler + in_force of atoms; 6: MKP short id, fact concept                         # part of every fingerprint: a new version rebuilds every MKP once
TOKEN = re.compile(r"\b(?:M_[A-Z0-9]+_(?:A|G|PD|LE|OR|CT|TC)_[A-Z0-9]+|(?:A|G|PD|LE|OR|CT|TC|F)_[A-Z0-9_]+)\b")
ABBREVIATED = re.compile(r"^M_[A-Z0-9]+_(?:A|G|PD|LE|OR|CT|TC)_[A-Z0-9]+$")
KIND = {"A": "ATOM", "A_REF": "ATOM", "G": "GROUP", "PD": "PD", "LE": "LE", "OR": "OR", "CT": "CT", "TC": "TC"}
COLS = ("from_type", "from_id", "from_name", "relation", "to_type", "to_id", "to_name", "link", "via",
        "handler", "in_force")                 # handler / in_force: of the atom a row starts from (observer filters)
GLOBAL = {"FACT", "TSG", "TG", "GOVERNANCE_MASTER", "PROVISION", "MKP"}


def fields(results: str) -> Dict[str, str]:
    """'key: value; key: "v"; list: [A, B]' -> {key: value} - the project's own RESULTS parser."""
    from transport import parse_results
    return {k: str(v) for k, v in parse_results(results or "").items()}


def items(value: str) -> List[str]:
    return [v.strip().strip('"\'') for v in (value or "").strip("[] ").split(",") if v.strip().strip('"\'')]


def links(mkp: str, akp: List[dict], edges: List[dict], gkp: List[dict], gm: List[dict],
          fact_names: Dict[str, str], same: Dict[str, dict] = None, mkp_id: str = "") -> List[dict]:
    """All kp_relation rows of one MKP. akp rows: type, element, concept, results, same_akp_id.
    mkp: the MKP key (citation_key#part, the kp_relation.mkp column); mkp_id: its short id (M_SIAFOS), the id of
    its MKP node - the citation (Section 7(1)(a)) is the node's name."""
    same = same or {}
    node = mkp_id or mkp
    kind: Dict[str, str] = {}
    name: Dict[str, str] = {}
    flds: Dict[str, Dict[str, str]] = {}
    cite = mkp
    for r in akp:
        t, el, f = r["type"], r["element"], fields(r.get("results") or "")
        if t == "MKP":
            cite = f.get("citation") or f.get("citation_key") or mkp
        if t in KIND:
            kind[el], flds[el] = KIND[t], f
            name[el] = (f.get("statement") or f.get("outcome") or f.get("question") or f.get("name")
                        or (f"{f['logic']} of {', '.join(items(f.get('members')))}" if f.get("logic") else "")
                        or (r.get("concept") if r.get("concept") not in (None, "", "NA") else "") or el)
    les = [el for el, k in kind.items() if k == "LE"]
    out: List[dict] = []
    seen: Set[Tuple] = set()

    def ident(t: str, x: str) -> Tuple[str, str]:
        """(full id, name) of a node: global ids as they are, MKP elements as <MKP>::<element>.
        An abbreviated element id (M_SIAFOS_A_SIAS) is unique in all laws, so it is used as it is."""
        if t in GLOBAL:
            return x, ""
        if ABBREVIATED.match(x):
            return x, name.get(x, x)
        return f"{mkp}::{x}", name.get(x, x)

    def add(ft, fx, rel, tt, tx, to_name="", from_name=None, via=""):
        f_id, f_nm = ident(ft, fx)
        t_id, t_nm = ident(tt, tx)
        key = (ft, f_id, rel, tt, t_id)          # a TSG and its governance master share the id TSG01_01
        if key in seen:
            return
        seen.add(key)
        out.append({"from_type": ft, "from_id": f_id, "from_name": str(from_name if from_name is not None else f_nm)[:300],
                    "relation": rel, "to_type": tt, "to_id": t_id, "to_name": str(to_name or t_nm)[:300],
                    "link": "DERIVED" if rel == "NEEDS" else "DIRECT", "via": via[:500]})

    def local(a, rel, b):                     # a -> b, both elements of this MKP
        if a in kind and b in kind:
            add(kind[a], a, rel, kind[b], b)

    # ---- facts read by atoms (the fact network written by AKP; older rows: the atom's own facts field)
    reads: Dict[str, Set[str]] = {}
    for e in edges:
        if e["atom"] in kind:
            reads.setdefault(e["atom"], set()).add(e["fact_id"])
    for el, k in kind.items():
        if k == "ATOM" and el not in reads:
            got = set(items(flds[el].get("facts")))
            if got:
                reads[el] = got
    for a in sorted(reads):
        for fact in sorted(reads[a]):
            add("ATOM", a, "READS", "FACT", fact, fact_names.get(fact, fact))

    # ---- how the MKP is built
    down: Dict[str, Set[str]] = {}            # element -> the elements it is built from (for NEEDS)
    for el in sorted(kind):
        k, f = kind[el], flds[el]
        if k == "GROUP":
            for m in items(f.get("members")):
                local(m, "MEMBER_OF", el)
                down.setdefault(el, set()).add(m)
        elif k == "LE":
            for tok in TOKEN.findall(f.get("expr") or ""):
                if kind.get(tok) in ("ATOM", "GROUP", "PD"):
                    local(tok, "USED_BY", el)
                    down.setdefault(el, set()).add(tok)
            for pd in items(f.get("feeds")) + items(f.get("produces")):
                if kind.get(pd) == "PD":
                    local(el, "PRODUCES", pd)
                    down.setdefault(pd, set()).add(el)
        elif k in ("OR", "CT", "TC"):
            text = " ".join(v for key, v in f.items() if key not in ("nl", "notes"))
            toks = [t for t in TOKEN.findall(text) if t != el]
            rel = {"OR": "OTHERWISE_OF", "CT": "TRIGGERED_BY", "TC": "TESTS"}[k]
            targets = [t for t in toks if kind.get(t) in (("ATOM", "GROUP", "LE") if k == "CT" else ("LE",))]
            if not targets and k != "CT" and len(les) == 1:     # names no LE, and the MKP has only one
                targets = les
            for t in targets:
                local(el, rel, t)
                down.setdefault(el, set()).add(t)
            for fact in sorted({t for t in toks if t.startswith("F_")}):
                if k == "CT":
                    add("CT", el, "TRIGGERED_BY", "FACT", fact, fact_names.get(fact, fact))
                reads.setdefault(el, set()).add(fact)
        if k in ("LE", "PD", "OR", "CT", "TC"):
            add(k, el, "PART_OF", "MKP", node, cite)
    for r in akp:
        t, el, f = r["type"], r["element"], fields(r.get("results") or "")
        if t == "A_REF" and r.get("same_akp_id") in same:
            s = same[r["same_akp_id"]]
            f_id, f_nm = ident("ATOM", el)
            t_id = s["element"] if ABBREVIATED.match(s["element"]) else f"{s['source_mkp']}::{s['element']}"
            if ("ATOM", f_id, "SAME_AS", "ATOM", t_id) not in seen:
                seen.add(("ATOM", f_id, "SAME_AS", "ATOM", t_id))
                out.append({"from_type": "ATOM", "from_id": f_id, "from_name": f_nm[:300], "relation": "SAME_AS",
                            "to_type": "ATOM", "to_id": t_id, "to_name": str(s.get("label") or s["element"])[:300],
                            "link": "DIRECT", "via": ""})
        if t == "LK" and f.get("target"):
            add("MKP", node, (f.get("relation") or "LINKS").upper(), "PROVISION", f["target"], f.get("kind") or "",
                from_name=cite)

    # ---- NEEDS: the facts each element depends on, with the path
    memo: Dict[str, Dict[str, List[str]]] = {}

    def needs(el: str, stack: Tuple[str, ...] = ()) -> Dict[str, List[str]]:
        """fact -> shortest path of elements, from the one that reads the fact up to el."""
        if el in memo:
            return memo[el]
        got: Dict[str, List[str]] = {f: [el] for f in reads.get(el, ())}
        for child in sorted(down.get(el, ())):
            if child in stack:
                continue
            for fact, path in needs(child, stack + (el,)).items():
                if fact not in got or len(path) + 1 < len(got[fact]):
                    got[fact] = path + [el]
        memo[el] = got
        return got

    for el in sorted(kind):
        if kind[el] == "ATOM":
            continue                           # atoms READS their facts directly
        for fact, path in sorted(needs(el).items()):
            add(kind[el], el, "NEEDS", "FACT", fact, fact_names.get(fact, fact), via=" > ".join(path))
    mkp_facts: Dict[str, List[str]] = {}
    for el in sorted(kind):
        for fact, path in needs(el).items():
            if fact not in mkp_facts or len(path) < len(mkp_facts[fact]):
                mkp_facts[fact] = path
    for fact, path in sorted(mkp_facts.items()):
        add("MKP", node, "NEEDS", "FACT", fact, fact_names.get(fact, fact), from_name=cite, via=" > ".join(path))

    # ---- TSGs (GKP), TGs and governance masters
    for g in gkp:
        el, tsg = g["akp_element"], g.get("tsg_id")
        if el not in kind or not tsg:
            continue
        add(kind[el], el, "GROUPED_IN", "TSG", tsg, g.get("tsg_name") or tsg)
        if g.get("tg_id"):
            add("TSG", tsg, "BELONGS_TO", "TG", g["tg_id"], g.get("tg_name") or g["tg_id"], from_name=g.get("tsg_name") or tsg)
        for fact, path in sorted(needs(el).items()):
            add("TSG", tsg, "NEEDS", "FACT", fact, fact_names.get(fact, fact), from_name=g.get("tsg_name") or tsg,
                via=" > ".join(path + [tsg]))
    for g in gm:
        gname = g.get("tsg_name") or g["tsg_id"]
        add("MKP", node, "USED_IN", "GOVERNANCE_MASTER", g["tsg_id"], gname, from_name=cite)
        for fact, path in sorted(mkp_facts.items()):
            add("GOVERNANCE_MASTER", g["tsg_id"], "NEEDS", "FACT", fact, fact_names.get(fact, fact), from_name=gname,
                via=" > ".join(path + [node, g["tsg_id"]]))
    # who checks each atom and when it is in force (notes sections 6, 12, 14): on every row that starts at the atom
    attrs = {}
    for el, k in kind.items():
        if k == "ATOM":
            f = flds.get(el) or {}
            vf, vt = f.get("valid_from") or "", f.get("valid_to") or ""
            attrs[ident("ATOM", el)[0]] = (f.get("handler") or "", f"{vf}..{vt}" if vf or vt else "")
    for r in out:
        h, w = attrs.get(r["from_id"], ("", "")) if r["from_type"] == "ATOM" else ("", "")
        r["handler"], r["in_force"] = h, w
    return out
