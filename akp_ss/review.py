"""AKP / fact review - fact flow layer 4 (notes section 8): the regular clean-up over the whole fact table.

    python akp_engine.py --fact-review                 measures + pairs of facts that may be one fact (model review)
    python akp_engine.py --fact-merge F_OLD=F_KEEP     a person approved it: F_OLD becomes an alias of F_KEEP

Review (no change to any law):
  measures   reuse rate (fact links that point to a fact first made by another MKP), new facts per MKP,
             facts used by only one atom
  pairs      facts >= review_min_similarity apart with the same type -> the fact-check model (same prompt as
             layer 3) answers SAME / DERIVED / NEW; the list is printed and saved in akp_cache
             (source_mkp FACT_REVIEW) and shown on the "Fact review" sheet of akp.xlsx
Merge (insert-only): every MKP whose latest AKP reads F_OLD gets a new AKP version with F_OLD rewritten to F_KEEP
(the old version stays, is_latest = false); F_KEEP gets F_OLD's name as an alias; F_OLD is marked
possible_duplicate_of = F_KEEP. Nothing is deleted.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Dict, List

from psycopg2 import sql

from .factcheck import parse
from .ids import rewrite

REVIEW_KEY = "FACT_REVIEW"


def measures(conn, registry) -> Dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT count(*), count(*) FILTER (WHERE f.first_source_mkp IS DISTINCT FROM e.source_mkp),
            count(DISTINCT e.source_mkp) FROM {} e JOIN {} f ON f.fact_id = e.fact_id WHERE e.is_latest""")
                    .format(registry.t(registry.edge_t), registry.t(registry.fact_t)))
        links, reused, mkps = cur.fetchone()
        cur.execute(sql.SQL("""SELECT count(*), count(*) FILTER (WHERE n <= 1) FROM (SELECT f.fact_id,
            count(DISTINCT e.source_mkp || ':' || e.atom) n FROM {} f LEFT JOIN {} e ON e.fact_id = f.fact_id AND e.is_latest
            GROUP BY f.fact_id) x""").format(registry.t(registry.fact_t), registry.t(registry.edge_t)))
        facts, single = cur.fetchone()
    return {"facts": int(facts or 0), "fact_links": int(links or 0),
            "reuse_rate": round(float(reused or 0) / links, 3) if links else 0.0,
            "new_facts_per_mkp": round(float(facts or 0) / mkps, 2) if mkps else 0.0,
            "facts_used_by_one_atom": int(single or 0)}


def pairs(conn, registry, min_similarity: float, limit: int = 200) -> List[dict]:
    """Pairs of registered facts that may be one fact: same type, similarity >= min_similarity - by name + nl or by
    concept (the higher of both, like the fact check)."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT * FROM (SELECT a.fact_id, a.name, a.entity, a.data_type, a.options, a.nl,
                b.fact_id, b.name, b.entity, b.data_type, b.options, b.nl, b.aliases,
                GREATEST(CASE WHEN a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                              AND vector_dims(a.embedding) = vector_dims(b.embedding)
                              THEN 1 - (a.embedding <=> b.embedding) END,
                         CASE WHEN a.concept_embedding IS NOT NULL AND b.concept_embedding IS NOT NULL
                              AND vector_dims(a.concept_embedding) = vector_dims(b.concept_embedding)
                              THEN 1 - (a.concept_embedding <=> b.concept_embedding) END) AS sim,
                a.concept, b.concept
            FROM {} a JOIN {} b ON a.fact_id < b.fact_id AND a.data_type IS NOT DISTINCT FROM b.data_type
            WHERE a.possible_duplicate_of IS DISTINCT FROM b.fact_id AND b.possible_duplicate_of IS DISTINCT FROM a.fact_id
            ) x WHERE sim >= %s ORDER BY sim DESC LIMIT %s""")
                    .format(registry.t(registry.fact_t), registry.t(registry.fact_t)), (min_similarity, limit))
        rows = cur.fetchall()
    return [{"a": {"fact": r[0], "name": r[1], "concept": r[14] or "", "entity": r[2], "type": r[3], "options": r[4],
                   "nl": r[5]},
             "b": {"fact_id": r[6], "name": r[7], "concept": r[15] or "", "entity": r[8], "type": r[9],
                   "options": r[10], "nl": r[11], "aliases": r[12], "similarity": round(float(r[13]), 4)}} for r in rows]


def review(conn, registry, store, ask: Callable[[dict], str], min_similarity: float, batch: int = 20) -> dict:
    """Measures + the merge list for a person. ask(request) -> raw answer of the fact-check model."""
    found = pairs(conn, registry, min_similarity)
    out = []
    for i in range(0, len(found), batch):
        part = found[i:i + batch]
        req = {"source": {"source_mkp": REVIEW_KEY, "text": "Two registered facts that may be the same input."},
               "new_facts": [dict(p["a"], used_by=[]) for p in part],
               "candidates": {p["a"]["fact"]: [p["b"]] for p in part}}
        try:
            answers = {a.get("fact"): a for a in parse(ask(req))}
        except Exception as exc:                 # one bad answer does not stop the review
            answers = {p["a"]["fact"]: {"decision": "UNKNOWN", "reason": f"{type(exc).__name__}: {str(exc)[:100]}"} for p in part}
        for p in part:
            a = answers.get(p["a"]["fact"], {})
            out.append({"fact": p["a"]["fact"], "name": p["a"]["name"], "other": p["b"]["fact_id"],
                        "other_name": p["b"]["name"], "similarity": p["b"]["similarity"],
                        "decision": str(a.get("decision", "UNKNOWN")).upper(),
                        "derivation": a.get("derivation", ""), "reason": a.get("reason", "")})
    result = {"measures": measures(conn, registry), "pairs": out}
    digest = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
    store.cache_put("ALL", REVIEW_KEY, REVIEW_KEY, digest, result)
    return result


def latest_review(store) -> dict:
    r = store.run(sql.SQL("""SELECT results_json FROM {} WHERE instrument='ALL' AND version_id=%s AND source_mkp=%s
        ORDER BY updated_at DESC LIMIT 1""").format(store.t(store.cache_table)), (REVIEW_KEY, REVIEW_KEY), fetch=True)
    return r[0][0] if r else {}


def merge(conn, registry, store, old: str, keep: str) -> List[str]:
    """F_old -> F_keep in every latest AKP that reads it, as a new AKP version (insert-only). Returns the MKPs."""
    known = registry.all_ids()
    if old not in known or keep not in known:
        raise ValueError(f"both facts must be registered: {old}, {keep}")
    if (known[old].get("type") or "") != (known[keep].get("type") or ""):
        raise ValueError(f"{old} is {known[old].get('type')} and {keep} is {known[keep].get('type')} - not merged")
    from .store import rows_from_db
    from transport import get_field
    from governance_master_ss import rules as R
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT options FROM {} WHERE fact_id=%s").format(registry.t(registry.fact_t)), (keep,))
        got = cur.fetchone()
    keep_options = {str(x).upper() for x in ((got[0] if got else None) or [])}
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT DISTINCT instrument, version_id, source_mkp FROM {} WHERE is_latest AND fact_id=%s""")
                    .format(registry.t(registry.edge_t)), (old,))
        mkps = cur.fetchall()
    # 1. prepare every law first: nothing is saved when one of them cannot be merged
    prepared = []
    for instrument, version, source_mkp in mkps:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT mkp, type, akp_id, concept, results, mkp_id, mkp_version, tier, source_hash,
                same_akp_id, concept_embedding::text, atom_embedding::text FROM {} WHERE instrument=%s AND version_id=%s
                AND source_mkp=%s AND is_latest AND validation_status='APPROVED' ORDER BY ordinal""").format(store.t(store.table)),
                        (instrument, version, source_mkp))
            recs = cur.fetchall()
        if not recs:
            continue
        keep_recs = [r for r in recs if not (r[1] == "FACTS" and r[2] == old)]   # the kept fact's definition stands
        rows = rows_from_db([r[:5] for r in keep_recs])
        vectors = {}
        for i, (row, rec) in enumerate(zip(rows, keep_recs)):
            if rec[9]:
                row["SAME_AKP_ID"] = rec[9]
            if row["TYPE"] == "A" and (rec[10] or rec[11]):     # the embeddings stay with the new version (GKP)
                vectors[i] = (json.loads(rec[10]) if rec[10] else None, json.loads(rec[11]) if rec[11] else None)
        rewrite(rows, {old: keep})
        bad = sorted({v for r in rows if r["TYPE"] in ("A", "A_REF")
                      for v in R.values_used(get_field(r, "rule")).get(keep, set())} - keep_options)
        if bad and keep_options:
            raise ValueError(f"{source_mkp} compares {old} with {bad}, which are not options of {keep} "
                             f"{sorted(keep_options)} - nothing merged")
        prepared.append((instrument, version, source_mkp, recs[0], rows, vectors))
    # 2. save: one new AKP version per law (insert-only)
    done = []
    for instrument, version, source_mkp, rec, rows, vectors in prepared:
        with conn.cursor() as cur:
            ver = store.save_version(cur, instrument, version, source_mkp, rec[5], rec[6], rec[8], rows, vectors, rec[7])
            registry.save(cur, rows, instrument, version, source_mkp, ver)
        conn.commit()
        done.append(source_mkp)
    with conn.cursor() as cur:                   # the old id stays (history), marked as the same as the kept one
        cur.execute(sql.SQL("UPDATE {} SET possible_duplicate_of=%s, updated_at=now() WHERE fact_id=%s")
                    .format(registry.t(registry.fact_t)), (keep, old))
        cur.execute(sql.SQL("""UPDATE {} SET aliases = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]')
            FROM jsonb_array_elements(aliases || %s::jsonb) x), updated_at=now() WHERE fact_id=%s""")
                    .format(registry.t(registry.fact_t)),
                    (json.dumps([known[old].get("name") or old] + list(known[old].get("aliases") or [])), keep))
    conn.commit()
    return done
