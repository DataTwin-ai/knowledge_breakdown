"""References after the workers (bug list C, agreed 2026-11-18) - the helpers main.py uses.

SELF  (same PDF, e.g. 7(1)(a) -> Schedule III): the direct targets of the run's laws that have no MKP yet run like a
      user scope (MKP -> AKP -> GKP). The target keeps its OWN place / TSG; the citing law links to it and the
      governance master pulls its checks in by the same ids.
CROSS (outside document: amending Act, notification, circular): downloaded and checked (ref_engine.py), IKP'd, and
      only the EXACT part the citing law needs runs, MKP -> AKP with NO GKP. A part already inserted is not run
      again: the citing law only gets its link (mkp_ref.source_key). The governance master writes the part in full
      in the citing law's yaml and wires the exact atom that names it (reference design 2026-11-19).
Status of every reference (mkp_ref.target_status): PENDING -> QUEUED -> INSERTED, or FAILED; INSERTED = the exact
clause has an approved AKP; a QUEUED row left by a stopped run is picked again.
Nothing here uses permission.json scope.default: a reference run always uses the exact keys found.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Tuple

from psycopg2 import sql

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def _t(schema: str, name: str):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def unit(key: str) -> str:
    """CGSTA17_S7_SS1_Ca#TEXT -> CGSTA17_S7_SS1_Ca."""
    return str(key or "").split("#", 1)[0]


# ---------------------------------------------------------------- what the run's laws refer to

def pending_refs(conn, schema: str, link_t: str, doc_t: str, source_mkps: List[str]) -> List[dict]:
    """The references of these laws whose exact clause has no AKP yet (PENDING / FAILED, and QUEUED left by a run
    that was stopped), with their document's state."""
    units = sorted({unit(m) for m in source_mkps})
    if not units:
        return []
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT l.instrument, l.version_id, l.mkp_element, l.scope, l.ref_id, l.target_key,
                l.relation, l.mention, l.target_status, COALESCE(l.source_key, ''), COALESCE(d.kind, ''),
                COALESCE(d.status, ''), COALESCE(d.doc_code, ''), COALESCE(d.title, '')
            FROM {} l LEFT JOIN {} d ON d.ref_id = l.ref_id
            WHERE split_part(l.mkp_element, '#', 1) = ANY(%s) AND l.target_status IN ('PENDING', 'QUEUED', 'FAILED')
            ORDER BY l.scope, l.ref_id, l.mkp_element""").format(_t(schema, link_t), _t(schema, doc_t)), (units,))
        cols = ("instrument", "version_id", "mkp_element", "scope", "ref_id", "target_key", "relation", "mention",
                "target_status", "source_key", "kind", "doc_status", "doc_code", "title")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return rows


def set_status(conn, schema: str, link_t: str, rows: List[dict], status: str) -> None:
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(sql.SQL("""UPDATE {} SET target_status=%s WHERE version_id=%s AND mkp_element=%s AND ref_id=%s
                AND target_key=%s""").format(_t(schema, link_t)),
                        (status, r["version_id"], r["mkp_element"], r["ref_id"], r["target_key"]))
    conn.commit()


def set_source_key(conn, schema: str, link_t: str, row: dict, keys: List[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""UPDATE {} SET source_key=%s WHERE version_id=%s AND mkp_element=%s AND ref_id=%s
            AND target_key=%s""").format(_t(schema, link_t)),
                    (",".join(keys), row["version_id"], row["mkp_element"], row["ref_id"], row["target_key"]))
    conn.commit()


def self_targets(rows: List[dict], limit: int) -> List[Tuple[str, str]]:
    """(instrument, target key) of the SELF references, each once, in order, at most `limit`."""
    out: List[Tuple[str, str]] = []
    for r in rows:
        if r["scope"] == "SELF" and r["target_key"] and (r["instrument"], r["target_key"]) not in out:
            out.append((r["instrument"], r["target_key"]))
    return out[:max(0, int(limit))]


# ---------------------------------------------------------------- the exact part of an outside document

def needles(citing_key: str) -> List[str]:
    """What an amending text says to name the citing law: CGSTA17_S2_C17_SCh -> ['section 2', '(17)', '(h)'];
    CGSTR17_R36_SS4 -> ['rule 36', '(4)']; CGSTA17_SCHIII_EN5 -> ['schedule iii', '5']."""
    parts = unit(citing_key).split("_")[1:]
    out: List[str] = []
    for p in parts:
        m = re.fullmatch(r"S(\d+[A-Z]*)", p)
        if m:
            out.append(f"section {m.group(1).lower()}")
            continue
        m = re.fullmatch(r"R(\d+[A-Z]*)", p)
        if m:
            out.append(f"rule {m.group(1).lower()}")
            continue
        m = re.fullmatch(r"SCH([IVX]+)", p)
        if m:
            out.append(f"schedule {m.group(1).lower()}")
            continue
        m = re.fullmatch(r"(?:SS|C|SC|SSC|EN|P)(\w+)", p)
        if m:
            v = m.group(1)
            out.append(v if p.startswith("EN") else f"({v.lower()})")
    return out


def _norm(text: str) -> str:
    t = str(text or "").lower().replace("‘", "'").replace("’", "'")
    t = re.sub(r"\s*\(\s*", "(", t)
    t = re.sub(r"\s*\)", ")", t)
    return re.sub(r"\s+", " ", t)


def python_part(idx, citing_key: str) -> List[str]:
    """The smallest part of the outside document whose text names every needle of the citing law (Python)."""
    want = needles(citing_key)
    if not want or not want[0].startswith(("section", "rule", "schedule")):
        return []
    hits = []
    for k in idx.order:
        if k == idx.doc:
            continue
        text = _norm(idx.text(k, False))
        if all(re.search(r"(?<![\w(])" + re.escape(n) + r"(?![\w)])" if not n.startswith("(") else re.escape(n), text)
               for n in want):
            hits.append((len(text), k))
    if not hits:
        return []
    best = min(hits)[1]
    # a part inside another hit is the more exact one; keep only the smallest that has every needle
    return [best]


def ask_part(idx, citing: dict, ask: Callable[[str], str], prompt: str) -> Tuple[List[str], str]:
    """The model picks the part(s), only from the document's keys (used when Python finds none)."""
    parts = [{"key": k, "level": idx.nodes[k].get("level"), "text": idx.text(k, False)[:600]}
             for k in idx.order if k != idx.doc][:400]
    req = {"citing": citing, "document": {"code": idx.doc}, "parts": parts}
    raw = ask(prompt + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False))
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1] or "{}")
    keys = [k for k in (obj.get("keys") or []) if k in idx.nodes]
    return keys, str(obj.get("reason") or "")[:200]


def find_part(idx, citing: dict, whole_max: int, ask: Optional[Callable[[str], str]] = None,
              prompt: str = "") -> Tuple[List[str], str]:
    """The keys of the outside document this citing law needs, and how they were found.
    citing: {key, text, mention, relation, target}."""
    target = str(citing.get("target") or "")                   # 2026-11-23: the exact part the law names
    if target and target != idx.doc and target in idx.nodes:   # ("clause (b) of sub-section (1) of section 2")
        return [target], "python: the exact part the law names"
    got = python_part(idx, citing["key"])                     # an amending text names the provision it changes
    if got:
        return got, "python: the part that names " + ", ".join(needles(citing["key"]))
    small = len([k for k in idx.order if k != idx.doc]) <= whole_max
    if small and citing.get("relation", "").upper() not in ("AMENDS",):
        return [idx.doc], f"whole document ({len(idx.order) - 1} parts)"
    if ask and prompt:
        keys, why = ask_part(idx, citing, ask, prompt)
        if keys:
            return keys, "model: " + why
    if small:
        return [idx.doc], f"whole document ({len(idx.order) - 1} parts) - no exact part found"
    return [], "no part found"


# ---------------------------------------------------------------- is the part already inserted?

def part_done(conn, schema: str, akp_t: str, instrument: str, keys: List[str]) -> bool:
    """True when every part key has an approved, latest AKP (the part itself or a part of it) - no stored list."""
    if not keys:
        return False
    with conn.cursor() as cur:
        for k in keys:
            cur.execute(sql.SQL(r"""SELECT 1 FROM {} WHERE instrument=%s AND is_latest AND validation_status='APPROVED'
                AND (split_part(source_mkp, '#', 1) = %s OR source_mkp LIKE %s) LIMIT 1""").format(_t(schema, akp_t)),
                        (instrument, k, k.replace("_", r"\_") + r"\_%"))
            if not cur.fetchone():
                conn.commit()
                return False
    conn.commit()
    return True


# ---------------------------------------------------------------- which atom holds the outside reference

def _nums(text: str) -> List[str]:
    return [re.sub(r"\s", "", n) for n in re.findall(r"\d+\s*/\s*\d{4}", str(text or ""))]


def mark_outside(rows: List[dict], refs: List[dict]) -> List[str]:
    """Reference design 2026-11-19 (item 7): only the EXACT atom that names an outside document holds it. Python
    writes `outside_refs: [ref ids]` on that atom (A rows): the document's number ("14/2017") in the atom's words;
    with no number, the MKP's only outside reference goes to the atom that says notified / circular. refs:
    [{ref_id, mention}] of this MKP (mkp_ref, scope CROSS). Returns log notes (a reference no atom names is listed)."""
    from transport import get_field
    notes, used = [], set()
    atoms = [r for r in rows if r.get("TYPE") == "A"]
    for ref in refs:
        nums = _nums(ref.get("mention"))
        hits = []
        for r in atoms:
            said = re.sub(r"\s", "", " ".join([get_field(r, "statement"), get_field(r, "reference"),
                                                get_field(r, "nl")]).lower())
            if nums and any(n in said for n in nums):
                hits.append(r)
            elif not nums and len(refs) == 1 and ("notif" in said or "circular" in said):
                hits.append(r)
        for r in hits:
            have = [x.strip() for x in re.findall(r"outside_refs:\s*\[([^\]]*)\]", r.get("RESULTS") or "")
                    for x in x.split(",") if x.strip()] if "outside_refs:" in (r.get("RESULTS") or "") else []
            ids = list(dict.fromkeys(have + [ref["ref_id"]]))
            text = re.sub(r";?\s*outside_refs:\s*\[[^\]]*\]", "", r.get("RESULTS") or "").strip(" ;")
            r["RESULTS"] = text + "; outside_refs: [" + ", ".join(ids) + "]"
            used.add(ref["ref_id"])
            notes.append(f"REFERENCE              : {r['ELEMENT']} holds {ref['ref_id']} ({ref.get('mention', '')[:60]})")
        if not hits:
            notes.append(f"REFERENCE NOT ON ATOM  : {ref['ref_id']} ({ref.get('mention', '')[:60]}) - no atom names it")
    return notes
