"""Cross references / find what one MKP refers to (Python, no LLM).

SELF  = another part of the same document (sub-section (1), Schedule II, section 15): from the IKP cross references
        of the provision, its lead-ins and closing line, which IKP already resolved to keys.
CROSS = anything outside the document: notifications, circulars, amending Acts, and the Act / Rules of the corpus
        ("section 15 of the Act" in the Rules, "rule 27 of the Central Goods and Services Tax Rules").
A footnote "Ins. by / Subs. by / Omitted by ..." is an AMENDING reference (relation AMENDED_BY). It is recorded
as PENDING like any other outside document: ref_engine.py fetches it (Law/Amendments), because the law before / after
the change is needed (notes section 13; features.push_amending_notifications is no longer used).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from .ids import ACT_RE, CIRC_RE, NOTIF_RE, corpus_code, parse

AMEND_CUE = re.compile(r"\b(ins\.|inserted|subs\.|substituted|omitted|amended|w\.e\.f\.|renumbered)\b", re.I)
SECTION_OF_ACT = re.compile(r"\b(?P<kind>section|rule)\s+(?P<no>\d{1,3}[A-Z]{0,2})(?P<chain>(?:\s*\(\s*\w{1,5}\s*\))*)"
                            r"\s+of\s+the\s+(?P<doc>[A-Za-z ()]{3,90}?(?:Act|Rules))", re.I)
SAME_DOC_WORDS = re.compile(r"^(?:this|the said)\b", re.I)
# the full name of an amending Act: "The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)"
FULL_ACT = re.compile(r"(?:\bThe\s+)?[A-Z][A-Za-z&,()'\s-]*?\bAct\b,?\s*(?:19|20)\d{2}\s*\(\s*No\.?\s*\d{1,3}\s+of\s+"
                      r"(?:19|20)\d{2}\s*\)")
# the date after a notification / circular number: "..., dated 25.01.2018" / "dt. 24.09.2021"
DATED = re.compile(r"^\s*,?\s*(?:dated|dt\.?)\s*(\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{4})", re.I)


def full_name(text: str, m: "re.Match", kind: str) -> str:
    """The whole name of the document the match `m` found in `text` (the title of ref_document): an amending Act
    with its own name and number, a notification / circular with its date. Falls back to the match itself."""
    short = " ".join(m.group(0).split())
    if kind == "AMENDING_ACT":
        for f in FULL_ACT.finditer(text or ""):
            if f.start() <= m.start() < f.end():
                return " ".join(f.group(0).split())
        return short.rstrip(" (") + (")" if short.count("(") > short.count(")") else "")
    d = DATED.match((text or "")[m.end():m.end() + 40])
    if not d:
        return short
    return short.rstrip(" ,") + " dated " + re.sub(r"\s+", "", d.group(1))


def _key(doc: str, kind: str, no: str, chain: str) -> str:
    key = f"{doc}_{'R' if kind.lower() == 'rule' else 'S'}{no.upper()}"
    toks = [t.strip("() ") for t in re.findall(r"\(\s*\w{1,5}\s*\)", chain or "")]
    if toks:
        key += f"_{'SR' if kind.lower() == 'rule' else 'SS'}{toks[0].upper()}"
        for t in toks[1:2]:
            key += f"_C{t}"
    return key


def _outside(text: str, own_doc: str, relation: str, where: str, name: str = "") -> List[dict]:
    """name: the full name of the document IKP read from the footnote (its `instrument` field), used as the title
    when it is the same document."""
    out = []
    named = parse(name) if name else None
    for rx in (NOTIF_RE, CIRC_RE, ACT_RE):
        for m in rx.finditer(text or ""):
            found = parse(m.group(0))
            if not found:
                continue
            rid, kind, info = found
            amending = where == "FOOTNOTE" and bool(AMEND_CUE.search(text)) or kind == "AMENDING_ACT"
            title = " ".join(name.split()) if named and named[0] == rid else full_name(text, m, kind)
            out.append({"ref_id": rid, "scope": "CROSS", "kind": kind, "mention": title[:300],
                        "relation": "AMENDED_BY" if amending else relation, "target_key": "", "where": where,
                        "amending": amending, **info})
    for m in SECTION_OF_ACT.finditer(text or ""):
        doc = corpus_code(m.group("doc"))
        if not doc or doc == own_doc:
            continue
        out.append({"ref_id": doc, "scope": "CROSS", "kind": "RULES" if doc.endswith(("R17",)) else "ACT",
                    "mention": " ".join(m.group(0).split())[:200], "relation": relation,
                    "target_key": _key(doc, m.group("kind"), m.group("no"), m.group("chain")), "where": where,
                    "amending": False})
    return out


def detect(own_doc: str, text: str, footnotes: Iterable[str] = (), xrefs: Iterable[dict] = (),
           llm_refs: Iterable[str] = (), act_code: Optional[str] = None) -> List[dict]:
    """All references of one MKP, de-duplicated by (scope, ref_id, target_key).

    xrefs     IKP cross references of the provision / lead-ins / closing line: {"cue", "to", "resolved"}
    llm_refs  the MKP's own cross_refs labels (the LLM's list): used for outside documents it names
    act_code  for Rules / notifications: the parent Act code that "section 15 of the Act" means
    """
    found: List[dict] = []
    for x in xrefs or []:
        to = str(x.get("to") or "")
        if not to or x.get("resolved") is False:
            continue
        scope = "SELF" if to.startswith(own_doc + "_") or to == own_doc else "CROSS"
        found.append({"ref_id": own_doc if scope == "SELF" else to.split("_", 1)[0], "scope": scope,
                      "kind": "PROVISION" if scope == "SELF" else "ACT", "mention": str(x.get("cue") or "")[:200],
                      "relation": "REFERS", "target_key": to, "where": "TEXT", "amending": False})
    found += _outside(text, own_doc, "REFERS", "TEXT")
    for f in footnotes or []:            # a footnote text, or IKP's amendment {"text", "instrument"}
        if isinstance(f, dict):
            name = str(f.get("instrument") or "")
            found += _outside(f"{f.get('text') or ''} {name}".strip(), own_doc, "REFERS", "FOOTNOTE", name)
        else:
            found += _outside(str(f), own_doc, "REFERS", "FOOTNOTE")
    for label in llm_refs or []:
        if parse(label):
            found += _outside(label, own_doc, "REFERS", "TEXT")
    if act_code and act_code != own_doc:          # "section 15 of the Act" in the Rules
        for m in re.finditer(r"\bsection\s+(\d{1,3}[A-Z]{0,2})((?:\s*\(\s*\w{1,5}\s*\))*)\s+of\s+the\s+Act\b", text or "", re.I):
            found.append({"ref_id": act_code, "scope": "CROSS", "kind": "ACT", "mention": " ".join(m.group(0).split()),
                          "relation": "REFERS", "target_key": _key(act_code, "section", m.group(1), m.group(2)),
                          "where": "TEXT", "amending": False})
    seen: Dict[tuple, dict] = {}
    for r in found:
        k = (r["scope"], r["ref_id"], r["target_key"])
        if k not in seen or (seen[k]["amending"] and not r["amending"]):
            seen[k] = r
    return list(seen.values())


def labels(refs: List[dict], scope: str) -> List[str]:
    """Short list for the MKP columns self_refs / cross_refs."""
    out = []
    for r in refs:
        if r["scope"] != scope:
            continue
        out.append(r["target_key"] or r["ref_id"])
    return list(dict.fromkeys(out))
