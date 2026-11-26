"""AKP / PostgreSQL.

akp            same table and columns as before, plus:
                        akp_version  1, 2, 3 ... per source MKP (a re-run saves the next version)
                        is_latest    true only for the newest version (older rows kept, validation_status SUPERSEDED,
                                     so GKP - which reads validation_status='APPROVED' - always sees the newest)
                        inserted_at  when the row was saved
                        mkp_version  the MKP version it was split from;  tier  SIMPLE / COMPLEX
akp_cache      candidates + Standard + comparison per MKP and input hash (as before)
akp_llm_cache  raw model answers (a crash never pays twice)
akp_time_fact      amendment MKPs as time facts on the provision they amend
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from psycopg2 import sql

from transport import get_field

from .parse import COLS, field_list


def group_names(row: dict) -> Optional[str]:
    """akp.group_names: the TSG ids of an atom (its groups field) as JSON; None for the other row types.
    A SAME atom (A_REF) has none here - GKP gives it the original atom's group (SAME_INHERITED)."""
    if row.get("TYPE") != "A":
        return None
    return json.dumps([g for g in field_list(row, "groups") if re.fullmatch(r"TSG\d+_\d+", g)])


def _vec(v) -> Optional[str]:
    return "[" + ",".join(str(float(x)) for x in v) + "]" if v else None


class AkpStore:
    def __init__(self, conn, schema: str, table: str, cache_table: str, mkp_table: str, llm_table: str,
                 time_table: str):
        """Every table name from Config/settings.json akp (table, cache_table, mkp_table, llm_cache_table,
        time_fact_table) - none is made here."""
        self.conn, self.schema = conn, schema
        self.table, self.cache_table, self.mkp_table = table, cache_table, mkp_table
        self.llm_table, self.time_table = llm_table, time_table

    @classmethod
    def from_settings(cls, conn, schema: str, s: dict) -> "AkpStore":
        return cls(conn, schema, s["table"], s["cache_table"], s["mkp_table"], s["llm_cache_table"], s["time_fact_table"])

    def t(self, name: str):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def run(self, q, params=None, fetch=False):
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall() if fetch else None

    def ensure(self) -> None:
        """Create / upgrade the tables (once while the worker pool runs: db_migrate.run_ensure)."""
        from db_migrate import run_ensure
        run_ensure(("AKP", self.schema, self.table), self._ensure)

    def _ensure(self) -> None:
        T = self.t(self.table)
        stmts = [
            sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"),
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                instrument text NOT NULL, version_id text NOT NULL, source_mkp text NOT NULL, mkp_id text,
                akp_id text NOT NULL, type text NOT NULL, source_hash text NOT NULL, ordinal integer NOT NULL,
                mkp text NOT NULL, concept text NOT NULL DEFAULT '', results text NOT NULL,
                concept_embedding vector, atom_embedding vector,
                validation_status text NOT NULL DEFAULT 'APPROVED', created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, ordinal))""").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_id text").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS akp_version integer NOT NULL DEFAULT 1").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS is_latest boolean NOT NULL DEFAULT true").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS inserted_at timestamptz NOT NULL DEFAULT now()").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_version integer").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS tier text").format(T),
            # akp_id: the abbreviated id of every row (notes section 3; there is no separate element column).
            # SAME: a SAME atom is type A_REF and its akp_id IS the saved atom's id (2026-11-19: no same_akp_id column)
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS akp_id text").format(T),
            # the TSG ids of each atom (its groups field), like mkp.group_names; the confirmed group is in gkp
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS group_names jsonb").format(T),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
                source_mkp text NOT NULL, input_sha256 text NOT NULL, results_json jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, input_sha256))""").format(self.t(self.cache_table)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (cache_key text PRIMARY KEY, model text, response text,
                inserted_at timestamptz NOT NULL DEFAULT now())""").format(self.t(self.llm_table)),
            # the MKP an answer belongs to: deleting that MKP also deletes its cached answers
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS unit_key text").format(self.t(self.llm_table)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
                amendment_mkp text NOT NULL, time_fact_id text NOT NULL, target_citation_key text NOT NULL,
                kind text, amended_by text, instrument_date date, effective_from date, replaced_text text, note text,
                inserted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, amendment_mkp))""").format(self.t(self.time_table)),
            # the day the amending law was brought into force, when it differs from effective_from (notes section 6)
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS in_force_from date").format(self.t(self.time_table)),
            # MKP table: versions (MKP adds these too; harmless if present)
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS is_latest boolean NOT NULL DEFAULT true").format(self.t(self.mkp_table)),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_version integer NOT NULL DEFAULT 1").format(self.t(self.mkp_table)),
        ]
        existing = {(t, c) for t, c in self.run(sql.SQL(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema=%s"), (self.schema,), fetch=True)}
        for s in stmts:
            text = s.as_string(self.conn)
            m = re.search(r'ALTER TABLE "[^"]+"\."([^"]+)" ADD COLUMN IF NOT EXISTS (\w+)', text)
            if m and (m.group(1), m.group(2)) in existing:
                continue                     # no ALTER (and no table lock) when the column is already there
            self.run(s)
        if (self.table, "element") in existing:       # a table made before the abbreviated ids: element is no longer written
            self.run(sql.SQL("ALTER TABLE {} ALTER COLUMN element DROP NOT NULL").format(T))
        # primary key must include akp_version so versions can live side by side
        rows = self.run(sql.SQL("""SELECT c.conname, array_agg(a.attname::text) FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conrelid = %s::regclass AND c.contype='p' GROUP BY c.conname"""),
            (f'"{self.schema}"."{self.table}"',), fetch=True)
        if rows and "akp_version" not in rows[0][1]:
            self.run(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(T, sql.Identifier(rows[0][0])))
            self.run(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY (instrument, version_id, source_mkp, akp_version, ordinal)").format(T))
        self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (instrument, version_id, source_mkp) WHERE is_latest").format(
            sql.Identifier(f"{self.table}_latest_idx"), T))
        self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (akp_id) WHERE is_latest").format(
            sql.Identifier(f"{self.table}_akp_id_idx"), T))
        self.conn.commit()

    # ---------------- caches
    def llm_get(self, key: str) -> Optional[str]:
        r = self.run(sql.SQL("SELECT response FROM {} WHERE cache_key=%s").format(self.t(self.llm_table)), (key,), fetch=True)
        return r[0][0] if r else None

    def llm_put(self, key: str, model: str, response: str, unit_key: str = "") -> None:
        self.run(sql.SQL("INSERT INTO {} (cache_key, model, response, unit_key) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING")
                 .format(self.t(self.llm_table)), (key, model, response, unit_key or None))
        self.conn.commit()

    def cache_get(self, instrument, version, source_mkp, fingerprint) -> Optional[dict]:
        r = self.run(sql.SQL("SELECT results_json FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                             "AND input_sha256=%s").format(self.t(self.cache_table)),
                     (instrument, version, source_mkp, fingerprint), fetch=True)
        return r[0][0] if r else None

    def old_cache(self, table: str, instrument, version, source_mkp, fingerprint) -> Optional[dict]:
        """The answer saved for this same MKP input (same fingerprint) in the OLD AKP cache table
        (akp.reuse_old_cache_table, 2026-11-19). None when that table or that row is not there."""
        try:
            r = self.run(sql.SQL("SELECT results_json FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                                 "AND input_sha256=%s").format(self.t(table)),
                         (instrument, version, source_mkp, fingerprint), fetch=True)
        except Exception:
            self.conn.rollback()
            return None
        return r[0][0] if r else None

    def old_fact_defs(self, table: str, ids: List[str]) -> Dict[str, dict]:
        """2026-11-19: the OLD fact table's definitions of these ids (old-cache reuse) - an old atom may read a fact it
        did not declare (it reused it from the old fact list). {} when the table or the ids are not there."""
        if not table or not ids:
            return {}
        try:
            r = self.run(sql.SQL("SELECT fact_id, name, entity, data_type, unit, options, evidence, derived_from, nl, "
                                 "concept FROM {} WHERE fact_id = ANY(%s)").format(self.t(table)), (list(ids),), fetch=True)
        except Exception:
            self.conn.rollback()
            return {}
        cols = ("fact_id", "name", "entity", "type", "unit", "options", "evidence", "derived_from", "nl", "concept")
        return {x[0]: dict(zip(cols, x)) for x in r}

    def outside_refs(self, link_table: str, version, source_mkp) -> List[dict]:
        """The outside documents this MKP names (mkp_ref, scope CROSS): [{ref_id, mention, target_key, relation}] -
        reference design item 7 (target_key / relation 2026-11-25: the exact part of an Act / Rules, PRESCRIBED_BY)."""
        try:
            r = self.run(sql.SQL("SELECT DISTINCT ref_id, mention, target_key, relation FROM {} WHERE version_id=%s "
                                 "AND mkp_element=%s AND scope='CROSS' ORDER BY ref_id, target_key").format(
                self.t(link_table)), (version, source_mkp), fetch=True)
        except Exception:
            self.conn.rollback()
            return []
        return [{"ref_id": a, "mention": b or "", "target_key": c or "", "relation": d or ""} for a, b, c, d in r]

    def cache_put(self, instrument, version, source_mkp, fingerprint, data: dict, commit: bool = True) -> None:
        self.run(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, input_sha256, results_json)
            VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT (instrument, version_id, source_mkp, input_sha256)
            DO UPDATE SET results_json=EXCLUDED.results_json, updated_at=now()""").format(self.t(self.cache_table)),
                 (instrument, version, source_mkp, fingerprint, json.dumps(data, ensure_ascii=False, default=str)))
        if commit:
            self.conn.commit()

    # ---------------- reads
    def latest(self, instrument, version, source_mkp) -> Optional[dict]:
        r = self.run(sql.SQL("SELECT akp_version, source_hash, max(mkp_version), "
                             "count(*) FILTER (WHERE type IN ('A','A_REF')) FROM {} WHERE instrument=%s AND version_id=%s "
                             "AND source_mkp=%s AND is_latest AND validation_status='APPROVED' GROUP BY akp_version, source_hash "
                             "ORDER BY akp_version DESC LIMIT 1").format(self.t(self.table)),
                     (instrument, version, source_mkp), fetch=True)
        return {"akp_version": r[0][0], "source_hash": r[0][1], "mkp_version": r[0][2], "atoms": int(r[0][3] or 0)} if r else None

    def atom_ids(self) -> set:
        """The akp_ids of every saved atom (latest, approved, type A) right now - what a worker's SAME check saw, so
        the save can find the atoms other workers saved after it (bug list A5, 2026-11-19)."""
        return {r[0] for r in self.run(sql.SQL("SELECT akp_id FROM {} WHERE type='A' AND is_latest "
                                               "AND validation_status='APPROVED'").format(self.t(self.table)), fetch=True)}

    def same_as_latest(self, cur, instrument, version, source_mkp, rows: List[dict]) -> Optional[int]:
        """The latest approved akp_version when `rows` say exactly the same (type, akp_id, results), else None."""
        cur.execute(sql.SQL("SELECT akp_version, type, akp_id, results FROM {} WHERE instrument=%s AND version_id=%s "
                            "AND source_mkp=%s AND is_latest AND validation_status='APPROVED' ORDER BY ordinal")
                    .format(self.t(self.table)), (instrument, version, source_mkp))
        old = cur.fetchall()
        if not old:
            return None
        norm = lambda s: " ".join(str(s or "").split())
        a = sorted((k, e, norm(r)) for _, k, e, r in old)
        b = sorted((r["TYPE"], r["ELEMENT"], norm(r.get("RESULTS"))) for r in rows)
        return int(old[0][0]) if a == b else None

    def touch_mkp_version(self, cur, instrument, version, source_mkp, mkp_version, rows=None, vectors=None) -> None:
        """Same AKP again: no new AKP version. Record the MKP version it was made from, and refresh the atom
        embeddings (they are search data, not content - e.g. after a change of embedding model)."""
        cur.execute(sql.SQL("UPDATE {} SET mkp_version=%s WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                            "AND is_latest").format(self.t(self.table)), (mkp_version, instrument, version, source_mkp))
        for i, row in enumerate(rows or []):
            cv, av = (vectors or {}).get(i, (None, None)) if row["TYPE"] == "A" else (None, None)
            if cv or av:
                cur.execute(sql.SQL("UPDATE {} SET concept_embedding=COALESCE(%s::vector, concept_embedding), "
                                    "atom_embedding=COALESCE(%s::vector, atom_embedding) WHERE instrument=%s AND version_id=%s "
                                    "AND source_mkp=%s AND akp_id=%s AND type='A' AND is_latest").format(self.t(self.table)),
                            (_vec(cv), _vec(av), instrument, version, source_mkp, row["ELEMENT"]))

    def mkp_id(self, version, source_mkp) -> Optional[tuple]:
        r = self.run(sql.SQL("SELECT mkp_id, mkp_version FROM {} WHERE type='MKP' AND version_id=%s AND element=%s AND is_latest "
                             "AND mkp_id IS NOT NULL AND BTRIM(mkp_id) <> '' ORDER BY mkp_version DESC LIMIT 1")
                     .format(self.t(self.mkp_table)), (version, source_mkp), fetch=True)
        return r[0] if r else None

    def same_candidates(self, source_mkp: str, rows: List[dict], vectors: Dict[int, tuple], limit: int = 5,
                        min_similarity: float = 0.90, concept_min: float = 2.0,
                        concept_floor: float = 2.0) -> List[dict]:
        """Existing canonical atoms (latest, approved, TYPE A) for every new atom - by law AND concept (notes
        section 7): found by its statement (atom_embedding, similarity > min_similarity), or by its concept
        (concept_embedding >= concept_min) when its statement is at least concept_floor alike. Each candidate has
        both similarities and the law text of its MKP; the SAME model decides."""
        found = []
        for i, row in enumerate(rows):
            if row["TYPE"] != "A" or i not in vectors or not vectors[i][1]:
                continue
            cv, av = vectors[i][0], vectors[i][1]
            got: Dict[str, dict] = {}
            for col, v in (("atom_embedding", av), ("concept_embedding", cv)):
                if not v:
                    continue
                try:
                    matches = self.run(sql.SQL("""SELECT a.akp_id, a.concept, a.results, a.instrument, a.version_id,
                        a.source_mkp, 1 - (a.atom_embedding <=> %s::vector),
                        CASE WHEN %s::vector IS NOT NULL AND a.concept_embedding IS NOT NULL
                             AND vector_dims(a.concept_embedding) = %s THEN 1 - (a.concept_embedding <=> %s::vector) END,
                        (SELECT m.results FROM {m} m WHERE m.type='MKP' AND m.element=a.source_mkp
                         AND m.version_id=a.version_id AND COALESCE(m.is_latest, true) LIMIT 1)
                        FROM {t} a WHERE a.type='A' AND a.validation_status='APPROVED' AND a.is_latest
                        AND a.source_mkp<>%s AND a.atom_embedding IS NOT NULL AND vector_dims(a.atom_embedding)=%s
                        AND a.{c} IS NOT NULL AND vector_dims(a.{c})=%s
                        ORDER BY a.{c} <=> %s::vector LIMIT %s""").format(
                        m=self.t(self.mkp_table), t=self.t(self.table), c=sql.Identifier(col)),
                        (_vec(av), _vec(cv), len(cv or []), _vec(cv), source_mkp, len(av), len(v), _vec(v), limit),
                        fetch=True)
                except Exception:
                    self.conn.rollback()
                    matches = []
                for aid, concept, results, inst, ver, src, score, cscore, mkp_results in matches:
                    if score is None or aid in got:
                        continue
                    score, cscore = float(score), (float(cscore) if cscore is not None else 0.0)
                    by_concept = cscore >= concept_min and score >= concept_floor
                    if score <= min_similarity and not by_concept:
                        continue
                    got[aid] = {"akp_id": aid, "element": aid, "concept": concept, "results": results,
                                "instrument": inst, "version": ver, "source_mkp": src,
                                "source_text": get_field({"RESULTS": mkp_results or ""}, "text"),
                                "similarity": round(score, 6), "concept_similarity": round(cscore, 6),
                                "by_concept": by_concept and score <= min_similarity}
            cands = sorted(got.values(), key=lambda c: -max(c["similarity"], c["concept_similarity"]))[:limit]
            found.append({"element": row["ELEMENT"], "concept": row["CONCEPT"], "new_results": row["RESULTS"],
                          "candidates": cands})
        return found

    # ---------------- save one MKP as the next AKP version (caller commits)
    def save_version(self, cur, instrument, version, source_mkp, mkp_id, mkp_version, fingerprint, rows: List[dict],
                     vectors: Dict[int, tuple], tier: str) -> int:
        T = self.t(self.table)
        cur.execute(sql.SQL("SELECT COALESCE(MAX(akp_version), 0) FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s")
                    .format(T), (instrument, version, source_mkp))
        akp_version = cur.fetchone()[0] + 1
        cur.execute(sql.SQL("""UPDATE {} SET is_latest=false,
            validation_status = CASE WHEN validation_status='APPROVED' THEN 'SUPERSEDED' ELSE validation_status END
            WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND is_latest""").format(T),
                    (instrument, version, source_mkp))
        for i, row in enumerate(rows):
            cv, av = vectors.get(i, (None, None)) if row["TYPE"] == "A" else (None, None)
            cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, mkp_id, source_hash, ordinal, mkp, type,
                akp_id, concept, results, concept_embedding, atom_embedding, validation_status, akp_version, is_latest,
                inserted_at, mkp_version, tier, group_names) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s::vector,%s::vector,'APPROVED',%s,true,now(),%s,%s,%s::jsonb)""").format(T),
                        (instrument, version, source_mkp, mkp_id, fingerprint, i, row["MKP"], row["TYPE"], row["ELEMENT"],
                         row["CONCEPT"], row["RESULTS"], _vec(cv), _vec(av), akp_version, mkp_version, tier,
                         group_names(row)))
        return akp_version

    def time_facts(self, instrument, version) -> List[dict]:
        """Saved amendments (akp_time_fact) of this document, in the time_fact() form."""
        try:
            rows = self.run(sql.SQL("""SELECT time_fact_id, target_citation_key, kind, effective_from, replaced_text, note
                FROM {} WHERE instrument=%s AND version_id=%s""").format(self.t(self.time_table)), (instrument, version),
                            fetch=True)
        except Exception:
            self.conn.rollback()
            return []
        return [{"time_fact_id": r[0], "target": r[1], "kind": r[2], "effective_from": str(r[3]) if r[3] else None,
                 "replaced_text": r[4], "note": r[5]} for r in rows]

    def save_time_fact(self, cur, instrument, version, source_mkp, tf: dict) -> None:
        cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, amendment_mkp, time_fact_id, target_citation_key, kind,
            amended_by, instrument_date, effective_from, in_force_from, replaced_text, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (instrument, version_id, amendment_mkp) DO UPDATE SET target_citation_key=EXCLUDED.target_citation_key,
            kind=EXCLUDED.kind, amended_by=EXCLUDED.amended_by, instrument_date=EXCLUDED.instrument_date,
            effective_from=EXCLUDED.effective_from, in_force_from=EXCLUDED.in_force_from,
            replaced_text=EXCLUDED.replaced_text, note=EXCLUDED.note,
            inserted_at=now()""").format(self.t(self.time_table)),
                    (instrument, version, source_mkp, tf["time_fact_id"], tf["target"], tf["kind"], tf["amended_by"],
                     tf["instrument_date"], tf["effective_from"], tf.get("in_force_from"), tf["replaced_text"], tf["note"]))

    # ---------------- embeddings left empty earlier (Vertex down) -> filled now
    def backfill(self, instrument: str, embed: Callable, limit: int = 2000) -> int:
        rows = self.run(sql.SQL("""SELECT ctid::text, concept, results FROM {} WHERE instrument=%s AND type='A' AND is_latest
            AND (atom_embedding IS NULL OR concept_embedding IS NULL) LIMIT %s""").format(self.t(self.table)),
                        (instrument, limit), fetch=True)
        done = 0
        for ctid, concept, results in rows:
            r = {"RESULTS": results}
            try:
                cv = embed(f"{concept} - {get_field(r, 'family')}")
                av = embed(get_field(r, "statement"))
            except Exception:
                break
            if not cv or not av:
                break
            self.run(sql.SQL("UPDATE {} SET concept_embedding=%s::vector, atom_embedding=%s::vector WHERE ctid=%s::tid")
                     .format(self.t(self.table)), (_vec(cv), _vec(av), ctid))
            done += 1
        self.conn.commit()
        return done

    # ---------------- for Excel
    def export_rows(self):
        std = self.run(sql.SQL("""SELECT instrument, source_mkp, mkp_id, mkp, type, akp_id, concept, results, akp_version,
            inserted_at, akp_id, CASE WHEN type = 'A_REF' THEN akp_id END, COALESCE(group_names::text, '') FROM {} WHERE is_latest AND validation_status='APPROVED' ORDER BY instrument, source_mkp, ordinal""")
                       .format(self.t(self.table)), fetch=True)
        cache = self.run(sql.SQL("""SELECT DISTINCT ON (instrument, source_mkp) instrument, source_mkp, results_json FROM {}
            ORDER BY instrument, source_mkp, updated_at DESC""").format(self.t(self.cache_table)), fetch=True)
        times = self.run(sql.SQL("""SELECT instrument, amendment_mkp, target_citation_key, kind, amended_by, instrument_date, effective_from,
            replaced_text, note
            FROM {} ORDER BY instrument, target_citation_key""").format(self.t(self.time_table)), fetch=True)
        return std, cache, times


def rows_from_db(records) -> List[dict]:
    return [dict(zip(COLS, r)) for r in records]
