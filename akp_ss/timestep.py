"""AKP / time step - the law in force on the invoice date (notes section 6).

The approved AKP table of an MKP splits the CURRENT text. When the provision was changed after GST began, one model
call per MKP says which atoms each change touched and writes the atoms of the OLD text. Python then adds the dates
and the logic, inside the same table, so the governance master only copies them:

    SUBSTITUTED  new atom X: valid_from <effective>;  old atom X_OLD: valid_to <day before>;
                 group G_T_X (ANY of X, X_OLD) takes X's place in every expression.
    INSERTED     new atom Y: valid_from <effective>.  A CONDITION also gets Y_PRE ("did not apply before",
                 rule <law date> IS_SET, valid_to <day before>) and G_T_Y (ANY of Y, Y_PRE) takes Y's place.
                 An ALTERNATIVE needs nothing more (before the change it reads FALSE: it did not exist).
    OMITTED      old atom Z: valid_to <day before>. A CONDITION also gets Z_POST ("no longer applies", valid_from
                 <effective>) and G_T_Z (ANY) is ANDed into its LE / ALL group; an ALTERNATIVE is ORed in.
The engine reads a check outside its window as FALSE ("not in force"), which makes these groups exact.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from transport import get_field

from .parse import field_list, rfield

KINDS = ("SUBSTITUTED", "INSERTED", "OMITTED")


def day_before(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def changes_for(citation_key: str, time_facts: List[dict], gst_start: str) -> List[dict]:
    """Time facts on this provision (or on the provision it belongs to) that apply after GST began."""
    out, seen = [], set()
    for tf in time_facts:
        target, eff = str(tf.get("target") or ""), str(tf.get("effective_from") or "")
        if not target or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff) or eff <= gst_start:
            continue
        if not (citation_key == target or citation_key.startswith(target + "_")):
            continue
        if tf["time_fact_id"] in seen or str(tf.get("kind") or "").upper() in ("IBID", "RENUMBERED"):
            continue
        seen.add(tf["time_fact_id"])
        out.append({"id": tf["time_fact_id"], "kind": str(tf.get("kind") or "AMENDED").upper(), "effective_from": eff,
                    "replaced_text": tf.get("replaced_text") or "", "note": str(tf.get("note") or "")[:1500]})
    return sorted(out, key=lambda c: (c["effective_from"], c["id"]))


def request(source: dict, rows: List[dict], changes: List[dict]) -> dict:
    atoms = [{"id": r["ELEMENT"], "statement": get_field(r, "statement"), "order": get_field(r, "order"),
              "handler": get_field(r, "handler"), "facts": field_list(r, "facts"), "rule": get_field(r, "rule"),
              "interpretation": get_field(r, "interpretation")} for r in rows if r["TYPE"] == "A"]
    exprs = [{"id": r["ELEMENT"], "type": r["TYPE"], "expr": get_field(r, "expr"), "logic": get_field(r, "logic"),
              "members": field_list(r, "members")} for r in rows if r["TYPE"] in ("LE", "G")]
    facts = [{"id": r["ELEMENT"], "name": get_field(r, "name"), "type": rfield(r, "type"),
              "options": field_list(r, "options")} for r in rows if r["TYPE"] == "FACTS"]
    return {"source": source, "changes": changes, "atoms": atoms + exprs, "facts": facts}


def parse(raw: str) -> List[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
    items = obj.get("changes")
    if not isinstance(items, list):
        raise ValueError("changes must be a list")
    return [i for i in items if isinstance(i, dict)]


def _q(t) -> str:
    return '"' + " ".join(str(t or "").split()).replace(";", ",").replace("|", "/").replace('"', "'") + '"'


def _add(row: dict, key: str, value: str) -> None:
    if not re.search(rf"(^|;)\s*{key}\s*:", row["RESULTS"]):
        row["RESULTS"] = row["RESULTS"].rstrip("; ") + f"; {key}: {value}"


def _atom(mkp: str, el: str, a: dict, extra: str) -> dict:
    parts = [f"name: {_q(a.get('name') or a.get('statement'))}", f"statement: {_q(a.get('statement'))}",
             "scope: TRANSACTION", f"role: {'CONDITION'}", f"order: {str(a.get('order') or '').upper()}",
             f"handler: {str(a.get('handler') or '').lower()}",
             f"facts: [{', '.join(str(f) for f in a.get('facts') or [])}]"]
    if str(a.get("order") or "").upper() == "LOW":
        parts.append(f"rule: {_q(a.get('rule'))}")
    else:
        parts.append(f"interpretation: {_q(a.get('interpretation'))}")
    return {"MKP": mkp, "TYPE": "A", "ELEMENT": el, "CONCEPT": (a.get("name") or "Old wording")[:60],
            "RESULTS": "; ".join(parts) + "; " + extra}


def _placeholder(mkp: str, el: str, text: str, law: str, window: str) -> dict:
    return {"MKP": mkp, "TYPE": "A", "ELEMENT": el, "CONCEPT": "Time window",
            "RESULTS": (f"name: {_q('Time Window Of ' + el.split('_', 1)[-1].replace('_', ' ').title())}; "
                        f"statement: {_q(text)}; scope: TRANSACTION; role: CONDITION; order: LOW; handler: python; "
                        f"facts: [{law}]; rule: {_q(law + ' IS_SET')}; {window}")}


_SMALL = {"the", "of", "a", "an", "and", "or", "to", "in", "by", "for", "on", "under", "as", "is", "be", "such", "any"}


def _has_old_words(old_text: str, statements: List[str], share: float = 0.6) -> bool:
    """Python check of the model's old atoms: most words of the replaced text appear in them (never guessed)."""
    want = [w for w in re.findall(r"[a-z0-9]+", old_text.lower()) if w not in _SMALL]
    have = set(re.findall(r"[a-z0-9]+", " ".join(str(x or "") for x in statements).lower()))
    return not want or sum(w in have for w in want) / len(want) >= share


def _replace_refs(rows: List[dict], old: str, new: str, skip: set) -> None:
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r"(?![A-Za-z0-9_])")
    for r in rows:
        if r["TYPE"] in ("LE", "G", "OR", "CT") and r["ELEMENT"] not in skip:
            r["RESULTS"] = pat.sub(new, r["RESULTS"])


def apply(rows: List[dict], answers: List[dict], changes: List[dict], law: dict) -> List[str]:
    """Use the answers on the table in place. law: {"fact", "name"} of the law-date fact. Returns notes.
    A change Python cannot place exactly is skipped with a note (the current law stays as it is)."""
    by_id = {c["id"]: c for c in changes}
    els = {r["ELEMENT"]: r for r in rows}
    atoms = {e for e, r in els.items() if r["TYPE"] == "A"}
    mkp = next((r["MKP"] for r in rows if r["TYPE"] == "A"), next((r["MKP"] for r in rows), ""))
    notes: List[str] = []
    used_law = False
    taken = set(els)

    def new_id(base: str) -> str:
        n, cand = 1, base
        while cand in taken:
            n += 1
            cand = f"{base}{n}"
        taken.add(cand)
        return cand

    def group(x: str, members: List[str], why: str) -> str:
        gid = new_id(f"G_T_{x.split('_', 1)[-1]}")
        label = (get_field(els[x], "name") if x in els else "") or x.split("_", 1)[-1].replace("_", " ").title()
        rows.append({"MKP": mkp, "TYPE": "G", "ELEMENT": gid, "CONCEPT": "Time window",
                     "RESULTS": f"name: {_q(label + ' In Force')}; logic: ANY; members: [{', '.join(members)}]; "
                                f"nl: {_q(why)}"})
        return gid

    for a in answers:
        ch = by_id.get(str(a.get("id", "")).strip())
        kind = str(a.get("kind", "")).upper()
        if not ch or kind == "NONE" or kind not in KINDS:
            if ch:
                notes.append(f"TIME {ch['id']}: no atom changed - {str(a.get('reason', ''))[:120]}")
            continue
        eff, before = ch["effective_from"], day_before(ch["effective_from"])
        tag = f"time_fact: {ch['id']}"
        new = [x for x in a.get("new_atoms") or [] if x in atoms]
        olds = [o for o in a.get("old_atoms") or [] if isinstance(o, dict) and o.get("statement")]
        if kind in ("SUBSTITUTED", "INSERTED") and not new:
            notes.append(f"TIME {ch['id']}: {kind} names no current atom - skipped")
            continue
        for x in new:
            _add(els[x], "valid_from", eff)
            _add(els[x], "time_fact", ch["id"])
        if kind in ("SUBSTITUTED", "OMITTED") and ch.get("replaced_text") and olds and \
                not _has_old_words(ch["replaced_text"], [o.get("statement") for o in olds]):
            notes.append(f"TIME {ch['id']}: the old atoms do not hold the old words '{ch['replaced_text'][:60]}' - skipped")
            for x in new:                       # undo the dates of this change: nothing of it is used
                els[x]["RESULTS"] = re.sub(r";\s*valid_from:\s*" + eff + r";\s*time_fact:\s*" + re.escape(ch["id"]),
                                           "", els[x]["RESULTS"])
            continue
        if kind == "SUBSTITUTED":
            by_target: Dict[str, List[str]] = {}
            for o in olds:
                target = o.get("replaces") if o.get("replaces") in new else (new[0] if len(new) == 1 else None)
                if not target:
                    notes.append(f"TIME {ch['id']}: old atom '{str(o.get('statement'))[:60]}' replaces no new atom - skipped")
                    continue
                oid = new_id(f"{target}_OLD")
                rows.append(_atom(mkp, oid, o, f"valid_to: {before}; {tag}"))
                by_target.setdefault(target, []).append(oid)
            for target, oids in by_target.items():
                gid = group(target, [target] + oids, f"{target} from {eff}, the old wording before")
                _replace_refs(rows, target, gid, {gid})
        elif kind == "INSERTED" and str(a.get("role", "")).upper() == "CONDITION":
            for y in new:
                pid = new_id(f"{y}_PRE")
                rows.append(_placeholder(mkp, pid, f"Before {eff} this condition did not apply: {get_field(els[y], 'statement')}",
                                         law["fact"], f"valid_to: {before}; {tag}"))
                gid = group(y, [y, pid], f"{y} applies from {eff}")
                _replace_refs(rows, y, gid, {gid})
                used_law = True
        elif kind == "OMITTED":
            for o in olds:
                joins = o.get("joins")
                target = els.get(joins)
                role = str(o.get("role", "")).upper()
                if target is None or target["TYPE"] not in ("LE", "G"):
                    notes.append(f"TIME {ch['id']}: omitted atom joins unknown {joins!r} - skipped")
                    continue
                oid = new_id("A_OMITTED")
                rows.append(_atom(mkp, oid, o, f"valid_to: {before}; {tag}"))
                node = oid
                if role == "CONDITION":
                    pid = new_id(f"{oid}_POST")
                    rows.append(_placeholder(mkp, pid, f"From {eff} this condition no longer applies: {o.get('statement')}",
                                             law["fact"], f"valid_from: {eff}; {tag}"))
                    node = group(oid, [oid, pid], f"condition omitted from {eff}")
                    used_law = True
                logic = get_field(target, "logic").upper()
                if target["TYPE"] == "LE":
                    expr = get_field(target, "expr")
                    op = "AND" if role == "CONDITION" else "OR"
                    target["RESULTS"] = re.sub(r'expr:\s*"[^"]*"', lambda m: f'expr: "({expr}) {op} {node}"',
                                               target["RESULTS"], count=1)
                elif (logic == "ALL") == (role == "CONDITION"):
                    members = field_list(target, "members") + [node]
                    target["RESULTS"] = re.sub(r"members:\s*\[[^\]]*\]", f"members: [{', '.join(members)}]",
                                               target["RESULTS"], count=1)
                else:
                    notes.append(f"TIME {ch['id']}: a {role} cannot join the {logic} group {joins} - skipped")
                    rows[:] = [r for r in rows if r["ELEMENT"] not in (oid, node)]
                    continue
        else:
            continue
        notes.append(f"TIME {ch['id']}: {kind} from {eff} - {len(new)} current atom(s), {len(olds)} old atom(s)")
    if used_law and not any(r["TYPE"] == "FACTS" and r["ELEMENT"] == law["fact"] for r in rows):
        rows.append({"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": law["fact"], "CONCEPT": "NA",
                     "RESULTS": f"name: {_q(law.get('name') or law['fact'])}; entity: INVOICE; type: DATE; "
                                f"source_kind: DOCUMENT; evidence: [invoice]"})
    return notes


def run(rows: List[dict], changes: List[dict], ask, law: dict, verify) -> Tuple[List[dict], List[str]]:
    """One model call for the MKP, then apply; verify(rows) -> engine errors. A table the engine could not run
    is not used: the current law is kept and the reason noted. Returns (rows, notes)."""
    if not changes:
        return rows, []
    trial = copy.deepcopy(rows)
    notes = apply(trial, parse(ask()), changes, law)
    errs = verify(trial)
    if errs:
        return rows, notes + [f"TIME STEP NOT USED: {e[:160]}" for e in errs[:4]]
    return trial, notes
