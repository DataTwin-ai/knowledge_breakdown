"""AKP / SAME check of SAVED atoms (bug list A4 + A5, agreed 2026-11-18).

Laws run in parallel (worker pool) never see each other's new atoms, and the atoms saved before the SAME fix were
never mapped (291 SAME checks, 0 A_REF rows). This module finds and maps them afterwards:

  1. pairs    the latest approved atoms of two DIFFERENT laws whose statement similarity (atom_embedding) is above
              same_review_min_similarity, or whose concept similarity (concept_embedding) is >= same_concept_min_
              similarity with a statement at least same_concept_statement_floor alike - up to same_review_top_k per
              atom. In every pair the FIRST-SAVED atom is kept; chains (A ~ B ~ C) all point to the first one.
  2. decide   an exact copy (same statement, facts and rule) is SAME by Python; the rest go to the SAME model
              (prompt akp_same_pair_prompt, same_review_batch pairs per call). The answers are cached.
  3. apply    for every SAME pair the later law gets a new AKP version (insert-only, the old one is SUPERSEDED): its
              atom becomes an A_REF row with the kept atom's id, its groups / LE use that id, its fact links stay.

Used by akp_engine.py --same-review (writes Output/same_review.xlsx for a person, changes nothing),
--same-apply (maps the rows a person marked YES) and by main.py after every run (same_sweep_after_run: the run's
new atoms, mapped automatically).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from psycopg2 import sql

from transport import get_field

from .ids import rewrite
from .parse import field_list

REVIEW_KEY = "SAME_REVIEW"
COLUMNS = ["PAIR", "KEEP", "MAP", "KEEP_LAW", "MAP_LAW", "KEEP_STATEMENT", "MAP_STATEMENT", "KEEP_FACTS",
           "MAP_FACTS", "SIMILARITY", "CONCEPT_SIMILARITY", "AI", "AI_REASON", "APPROVE"]


def _norm(x) -> str:
    return " ".join(str(x or "").replace('"', "").replace("'", "").split()).strip(" .").upper()


def _r(results: str) -> dict:
    return {"RESULTS": results or ""}


# ---------------------------------------------------------------- 1. pairs

def find_pairs(store, min_similarity: float, concept_min: float, concept_floor: float, top_k: int,
               only_mkps: Optional[List[str]] = None) -> List[dict]:
    """Close pairs of saved atoms (other law, saved earlier). only_mkps: only atoms of these MKPs are the later one
    (the SAME check after a run); None = every saved atom (--same-review)."""
    T, M = store.t(store.table), store.t(store.mkp_table)
    where = "AND b.source_mkp = ANY(%s)" if only_mkps else ""
    q = sql.SQL("""
        WITH atoms AS (
            SELECT a.instrument, a.version_id, a.source_mkp, a.mkp_id, a.akp_id, a.results, a.inserted_at,
                   a.atom_embedding AS e, a.concept_embedding AS c
            FROM {t} a
            WHERE a.type = 'A' AND a.is_latest AND a.validation_status = 'APPROVED' AND a.atom_embedding IS NOT NULL)
        SELECT b.akp_id, b.source_mkp, b.mkp_id, b.results, x.akp_id, x.source_mkp, x.mkp_id, x.results,
               x.sim, x.csim,
               (SELECT m.results FROM {m} m WHERE m.type = 'MKP' AND m.element = x.source_mkp
                AND m.version_id = x.version_id AND COALESCE(m.is_latest, true) LIMIT 1),
               (SELECT m.results FROM {m} m WHERE m.type = 'MKP' AND m.element = b.source_mkp
                AND m.version_id = b.version_id AND COALESCE(m.is_latest, true) LIMIT 1)
        FROM atoms b
        CROSS JOIN LATERAL (
            SELECT * FROM (
                (SELECT a.akp_id, a.source_mkp, a.mkp_id, a.results, a.version_id,
                        1 - (a.e <=> b.e) AS sim,
                        CASE WHEN a.c IS NOT NULL AND b.c IS NOT NULL AND vector_dims(a.c) = vector_dims(b.c)
                             THEN 1 - (a.c <=> b.c) END AS csim
                 FROM atoms a
                 WHERE a.source_mkp <> b.source_mkp AND vector_dims(a.e) = vector_dims(b.e)
                   AND (a.inserted_at, a.akp_id) < (b.inserted_at, b.akp_id)
                 ORDER BY a.e <=> b.e LIMIT %s)
                UNION
                (SELECT a.akp_id, a.source_mkp, a.mkp_id, a.results, a.version_id,
                        1 - (a.e <=> b.e) AS sim, 1 - (a.c <=> b.c) AS csim
                 FROM atoms a
                 WHERE a.source_mkp <> b.source_mkp AND vector_dims(a.e) = vector_dims(b.e)
                   AND b.c IS NOT NULL AND a.c IS NOT NULL AND vector_dims(a.c) = vector_dims(b.c)
                   AND (a.inserted_at, a.akp_id) < (b.inserted_at, b.akp_id)
                 ORDER BY a.c <=> b.c LIMIT %s)) u) x
        WHERE 1 = 1 {w}
    """.replace("{w}", where)).format(t=T, m=M)
    params = [top_k, top_k] + ([list(only_mkps)] if only_mkps else [])
    out = []
    for (oid, omkp, omid, ores, kid, kmkp, kmid, kres, sim, csim, ktext, otext) in store.run(q, params, fetch=True):
        sim, csim = float(sim or 0), float(csim or 0)
        if not (sim > min_similarity or (csim >= concept_min and sim >= concept_floor)):
            continue
        out.append({"keep": kid, "keep_mkp": kmkp, "keep_mkp_id": kmid, "keep_results": kres or "",
                    "keep_text": get_field(_r(ktext), "text"),
                    "map": oid, "map_mkp": omkp, "map_mkp_id": omid, "map_results": ores or "",
                    "map_text": get_field(_r(otext), "text"),
                    "similarity": round(sim, 4), "concept_similarity": round(csim, 4)})
    return out


def orphans(store) -> List[tuple]:
    """Bug list F24: SAME atoms (A_REF) whose saved atom is no longer a latest atom (its law got a new version that
    reworded / removed it, or was deleted). Returns [(source_mkp, akp_id)] - those laws must be checked again."""
    T = store.t(store.table)
    rows = store.run(sql.SQL("""SELECT DISTINCT r.source_mkp, r.akp_id FROM {t} r WHERE r.is_latest
        AND r.type = 'A_REF' AND COALESCE(r.akp_id, '') <> '' AND NOT EXISTS (SELECT 1 FROM {t} a
        WHERE a.akp_id = r.akp_id AND a.type = 'A' AND a.is_latest AND a.validation_status = 'APPROVED')
        ORDER BY 1""").format(t=T), fetch=True)
    store.conn.commit()
    return [(m, a) for m, a in rows]


# ---------------------------------------------------------------- 2. decide

def exact_copy(p: dict) -> bool:
    """The same statement word for word, the same facts and the same rule (no model needed)."""
    k, m = _r(p["keep_results"]), _r(p["map_results"])
    return bool(get_field(k, "statement")) and (
        (_norm(get_field(k, "statement")), sorted(field_list(k, "facts")), _norm(get_field(k, "rule"))) ==
        (_norm(get_field(m, "statement")), sorted(field_list(m, "facts")), _norm(get_field(m, "rule"))))


def _parse_answers(raw: str) -> Dict[int, dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
    out = {}
    for a in obj.get("answers") or []:
        try:
            out[int(a.get("pair"))] = {"same": str(a.get("same", "")).strip().upper() == "TRUE",
                                       "reason": str(a.get("reason", "")).strip()}
        except (TypeError, ValueError):
            continue
    return out


def decide(pairs: List[dict], ask: Callable[[str], str], prompt: str, batch: int = 15,
           note: Callable[[str], None] = lambda _m: None) -> List[dict]:
    """Fill p['ai'] (SAME / NOT SAME / NO ANSWER), p['reason'] and p['found_by'] for every pair."""
    to_model = []
    for p in pairs:
        if exact_copy(p):
            p.update(ai="SAME", reason="exact copy: same statement, facts and rule", found_by="python")
        else:
            to_model.append(p)
    for i in range(0, len(to_model), batch):
        chunk = to_model[i:i + batch]
        req = {"pairs": [{"pair": n + 1,
                          "keep": {"akp_id": p["keep"], "results": p["keep_results"], "law_text": p["keep_text"]},
                          "other": {"akp_id": p["map"], "results": p["map_results"], "law_text": p["map_text"]}}
                         for n, p in enumerate(chunk)]}
        try:
            answers = _parse_answers(ask(prompt + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False, default=str)))
        except Exception as exc:
            from llm_runtime_errors import is_rate_limited
            if is_rate_limited(exc):
                raise
            note(f"SAME REVIEW            : {len(chunk)} pair(s) not checked - {type(exc).__name__}: {str(exc)[:120]}")
            answers = {}
        for n, p in enumerate(chunk):
            a = answers.get(n + 1)
            if a is None:
                p.update(ai="NO ANSWER", reason="the model gave no answer for this pair", found_by="model")
            else:
                p.update(ai="SAME" if a["same"] and a["reason"] else "NOT SAME", reason=a["reason"], found_by="model")
    return pairs


def plan(pairs: List[dict]) -> List[dict]:
    """The mapping to make from SAME pairs: each later atom -> ONE kept atom (the most similar), chains to the first
    one (A ~ B, B ~ C -> C and B both keep A). A kept atom is never mapped away itself."""
    best: Dict[str, dict] = {}
    for p in pairs:
        if p.get("ai") != "SAME":
            continue
        cur = best.get(p["map"])
        if cur is None or p["similarity"] > cur["similarity"]:
            best[p["map"]] = p
    root = {m: p["keep"] for m, p in best.items()}

    def first(aid: str, seen=()) -> str:
        nxt = root.get(aid)
        return aid if nxt is None or nxt in seen else first(nxt, seen + (aid,))

    out = []
    for m, p in best.items():
        out.append(dict(p, keep=first(p["keep"])))
    return out


# ---------------------------------------------------------------- Excel for a person

def write_excel(path: Path, pairs: List[dict]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "SAME review"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF2CC")
    for n, p in enumerate(pairs, 1):
        k, m = _r(p["keep_results"]), _r(p["map_results"])
        ws.append([n, p["keep"], p["map"], p["keep_mkp"], p["map_mkp"], get_field(k, "statement"),
                   get_field(m, "statement"), ", ".join(field_list(k, "facts")), ", ".join(field_list(m, "facts")),
                   p["similarity"], p["concept_similarity"], p.get("ai", ""), p.get("reason", ""), ""])
        ws.cell(row=n + 1, column=len(COLUMNS)).fill = fill
    for col, width in zip("ABCDEFGHIJKLMN", (6, 22, 22, 26, 26, 60, 60, 18, 18, 10, 10, 10, 50, 9)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "B2"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def read_approved(path: Path) -> List[dict]:
    """The rows a person marked APPROVE = YES: [{keep, map}]."""
    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    if not rows or list(rows[0][:len(COLUMNS)]) != COLUMNS:
        raise ValueError(f"{path.name} is not a SAME review file (columns {COLUMNS})")
    i = {c: n for n, c in enumerate(COLUMNS)}
    return [{"keep": str(r[i["KEEP"]]).strip(), "map": str(r[i["MAP"]]).strip(), "reason": "approved by a person",
             "found_by": "person"}
            for r in rows[1:] if r and str(r[i["APPROVE"]] or "").strip().upper() in ("YES", "Y")]


# ---------------------------------------------------------------- 3. apply

def apply(conn, store, registry, mapping: List[dict], note: Callable[[str], None] = lambda _m: None) -> List[tuple]:
    """For every {keep, map}: a new AKP version of the law of `map` where that atom is an A_REF with the kept id
    (insert-only). Returns [(instrument, source_mkp)] of the laws changed."""
    from .store import rows_from_db
    T = store.t(store.table)
    by_law: Dict[tuple, Dict[str, str]] = {}
    for m in mapping:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT instrument, version_id, source_mkp FROM {} WHERE akp_id=%s AND type='A'
                AND is_latest AND validation_status='APPROVED'""").format(T), (m["map"],))
            law = cur.fetchone()
            cur.execute(sql.SQL("""SELECT 1 FROM {} WHERE akp_id=%s AND type='A' AND is_latest
                AND validation_status='APPROVED'""").format(T), (m["keep"],))
            kept = cur.fetchone()
        if not law:
            note(f"SAME NOT APPLIED        : {m['map']} -> {m['keep']} - {m['map']} is no longer a latest atom")
            continue
        if not kept:
            note(f"SAME NOT APPLIED        : {m['map']} -> {m['keep']} - {m['keep']} is no longer a latest atom")
            continue
        by_law.setdefault(tuple(law), {})[m["map"]] = m["keep"]
    done = []
    for (instrument, version, source_mkp), changes in by_law.items():
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT mkp, type, akp_id, concept, results, mkp_id, mkp_version, tier, source_hash,
                CASE WHEN type = 'A_REF' THEN akp_id END, concept_embedding::text, atom_embedding::text FROM {} WHERE instrument=%s AND version_id=%s
                AND source_mkp=%s AND is_latest AND validation_status='APPROVED' ORDER BY ordinal""").format(T),
                        (instrument, version, source_mkp))
            recs = cur.fetchall()
        if not recs:
            continue
        rows = rows_from_db([r[:5] for r in recs])
        present = {r["ELEMENT"] for r in rows}
        vectors, applied = {}, {}
        for i, (row, rec) in enumerate(zip(rows, recs)):
            if rec[9]:
                row["SAME_AKP_ID"] = rec[9]
            keep = changes.get(row["ELEMENT"]) if row["TYPE"] == "A" else None
            if keep and keep not in present:
                row["_facts"] = field_list(row, "facts")
                row["TYPE"] = "A_REF"
                row["SAME_AKP_ID"] = keep
                applied[row["ELEMENT"]] = keep
                present.add(keep)
            elif keep:
                note(f"SAME NOT APPLIED        : {row['ELEMENT']} -> {keep} - {keep} is already in {source_mkp}")
            if row["TYPE"] == "A" and (rec[10] or rec[11]):       # the embeddings stay with the new version
                vectors[i] = (json.loads(rec[10]) if rec[10] else None, json.loads(rec[11]) if rec[11] else None)
        if not applied:
            continue
        rewrite(rows, applied)                                   # its groups / LE / CT / TC use the kept id
        rec = recs[0]
        with conn.cursor() as cur:
            ver = store.save_version(cur, instrument, version, source_mkp, rec[5], rec[6], rec[8], rows, vectors, rec[7])
            registry.save(cur, rows, instrument, version, source_mkp, ver)
        conn.commit()
        for old, keep in applied.items():
            note(f"SAME                    : {old} = {keep} - {source_mkp} AKP version {ver}")
        done.append((instrument, source_mkp))
    return done
