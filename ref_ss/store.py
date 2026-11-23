"""Cross references / PostgreSQL.

ref_document  one row per outside document (fixed ID from ref_ss/ids.py): kind, number, year, link, status
              status: PENDING -> LINK_FOUND / LINK_BAD -> DOWNLOADED -> VERIFIED -> INSERTED
                      NOT_FOUND, FAILED  (amending documents are no longer SKIPPED: notes section 13)
              link_status (set while splitting by ref_ss/link.py, or by ref_engine.py): OK / BAD / NOT_FOUND
              The document status says only whether we have the document's file - not whether a law of it is made.
mkp_ref       which MKP refers to which document (and to which provision of it): one row per reference.
              target_status: INSERTED when the exact clause it points to has an approved, latest AKP (the AKP
              table of Config/settings.json, 2026-11-19 - per clause, not per document), else PENDING
              (update_target_status, at the end of every run); QUEUED while the automatic step after the
              workers runs it, FAILED when that run failed (bug list C3). source_key: the exact part of an outside
              document the law needs (its IKP key), found after the download.
              Deleting an MKP deletes its mkp_ref rows (db_migrate.py trigger); the ref_document row stays.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from psycopg2 import sql

FINAL = ("INSERTED",)
# "Inserted by" / "Substituted vide" ... in front of a document's name (bug list C14)
TITLE_VERB = r"^\s*(?:inserted|substituted|omitted|added|amended|renumbered)\s+(?:by|vide)\s+"


def clean_title(title: str) -> str:
    import re
    return re.sub(TITLE_VERB, "", str(title or ""), flags=re.I).strip()
RANK = {"PENDING": 0, "SKIPPED": 0, "LINK_BAD": 1, "NOT_FOUND": 1, "FAILED": 1, "LINK_FOUND": 2, "DOWNLOADED": 3,
        "VERIFIED": 4, "QUEUED": 5, "INSERTED": 6}


class RefStore:
    def __init__(self, conn, schema: str = "public", doc_table: str = "ref_document", link_table: str = "mkp_ref"):
        self.conn, self.schema, self.doc_t, self.link_t = conn, schema, doc_table, link_table

    def t(self, name: str):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def ensure(self) -> None:
        """Create / upgrade the tables (once while the worker pool runs: db_migrate.run_ensure)."""
        from db_migrate import run_ensure
        run_ensure(("REF", self.schema, self.doc_t), self._ensure)

    def _ensure(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                ref_id text PRIMARY KEY, kind text NOT NULL, title text NOT NULL DEFAULT '', number integer,
                year integer, series text, link text NOT NULL DEFAULT '', link_status text NOT NULL DEFAULT '',
                status text NOT NULL DEFAULT 'PENDING', doc_code text NOT NULL DEFAULT '', local_file text NOT NULL DEFAULT '',
                first_seen_in text NOT NULL DEFAULT '', mentions integer NOT NULL DEFAULT 0, note text NOT NULL DEFAULT '',
                inserted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
                        .format(self.t(self.doc_t)))
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                instrument text NOT NULL, version_id text NOT NULL, mkp_element text NOT NULL, unit_key text NOT NULL,
                ref_id text NOT NULL, scope text NOT NULL, target_key text NOT NULL DEFAULT '', relation text NOT NULL,
                mention text NOT NULL DEFAULT '', where_found text NOT NULL DEFAULT '',
                inserted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (version_id, mkp_element, scope, ref_id, target_key))""").format(self.t(self.link_t)))
            # the status of the exact law each reference points to (INSERTED = it has a saved MKP; else PENDING)
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS target_status text NOT NULL DEFAULT 'PENDING'")
                        .format(self.t(self.link_t)))
            # bug list C: the exact part of an outside document a law needs (its IKP key), set after the workers
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS source_key text NOT NULL DEFAULT ''")
                        .format(self.t(self.link_t)))
            # bug list C14: a title saved with the footnote's verb in front ("Inserted by The Central ...")
            cur.execute(sql.SQL(r"""UPDATE {} SET title = regexp_replace(title, %s, '', 'i'), updated_at = now()
                WHERE title ~* %s""").format(self.t(self.doc_t)), (TITLE_VERB, TITLE_VERB))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (ref_id)").format(
                sql.Identifier(f"{self.link_t}_ref_idx"), self.t(self.link_t)))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (status)").format(
                sql.Identifier(f"{self.doc_t}_status_idx"), self.t(self.doc_t)))
        self.conn.commit()

    def save(self, cur, instrument: str, version: str, unit_key: str, by_mkp: Dict[str, List[dict]],
             held: Dict[str, str], push_amending: bool) -> int:
        """Replace the reference rows of these MKPs and upsert their documents. held: ref_id -> our doc code."""
        n = 0
        if not by_mkp:
            return 0
        cur.execute(sql.SQL("DELETE FROM {} WHERE version_id=%s AND mkp_element = ANY(%s)").format(self.t(self.link_t)),
                    (version, list(by_mkp)))
        docs = []
        for element, refs in sorted(by_mkp.items()):
            for r in refs:
                cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, mkp_element, unit_key, ref_id, scope,
                    target_key, relation, mention, where_found) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""").format(self.t(self.link_t)),
                            (instrument, version, element, unit_key, r["ref_id"], r["scope"], r["target_key"],
                             r["relation"], r["mention"], r["where"]))
                n += 1
                if r["scope"] == "CROSS":
                    docs.append((element, r))
        # documents in one fixed order: two workers never wait on each other's rows (no deadlock)
        for element, r in sorted(docs, key=lambda x: x[1]["ref_id"]):
            code = held.get(r["ref_id"], "")
            # amending documents are needed too (the law before / after a change, notes section 13): never skipped
            status = "INSERTED" if code else "PENDING"
            cur.execute(sql.SQL("""INSERT INTO {} (ref_id, kind, title, number, year, series, status, doc_code,
                first_seen_in, mentions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON CONFLICT (ref_id) DO UPDATE SET mentions = {}.mentions + 1, updated_at = now(),
                title = CASE WHEN length(EXCLUDED.title) > length({}.title) THEN EXCLUDED.title ELSE {}.title END,
                doc_code = CASE WHEN EXCLUDED.doc_code <> '' THEN EXCLUDED.doc_code ELSE {}.doc_code END,
                status = CASE WHEN EXCLUDED.status = 'INSERTED' THEN 'INSERTED'
                              WHEN {}.status = 'SKIPPED' AND EXCLUDED.status = 'PENDING' THEN 'PENDING'
                              ELSE {}.status END""").format(self.t(self.doc_t), *[sql.Identifier(self.doc_t)] * 6),
                        (r["ref_id"], r["kind"], clean_title(r["mention"]), r.get("number"), r.get("year"),
                         r.get("series"), status, code, element))
        return n

    def mark_inserted(self, ref_id: str, doc_code: str, title: str = "", kind: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""INSERT INTO {} (ref_id, kind, title, status, doc_code) VALUES (%s,%s,%s,'INSERTED',%s)
                ON CONFLICT (ref_id) DO UPDATE SET status='INSERTED', doc_code=EXCLUDED.doc_code, updated_at=now()""")
                        .format(self.t(self.doc_t)), (ref_id, kind or "DOCUMENT", title, doc_code))
        self.conn.commit()

    def set(self, ref_id: str, **fields) -> None:
        cols = [k for k in fields if k in ("link", "link_status", "status", "local_file", "note", "doc_code", "title")]
        if not cols:
            return
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("UPDATE {} SET {}, updated_at=now() WHERE ref_id=%s").format(
                self.t(self.doc_t), sql.SQL(", ").join(sql.SQL("{}=%s").format(sql.Identifier(c)) for c in cols)),
                [fields[c] for c in cols] + [ref_id])
        self.conn.commit()

    def pending(self, kinds: Iterable[str] = ("NOTIFICATION", "CIRCULAR", "AMENDING_ACT", "ACT", "RULES", "OTHER_ACT"),
                limit: int = 200) -> List[dict]:
        """Each outside document ONCE (ref_document has one row per document, however many laws cite it)."""
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT ref_id, kind, title, number, year, series, link, status, mentions FROM {}
                WHERE status IN ('PENDING','SKIPPED','LINK_BAD','LINK_FOUND','DOWNLOADED') AND kind = ANY(%s)
                ORDER BY mentions DESC, ref_id LIMIT %s""").format(self.t(self.doc_t)), (list(kinds), limit))
            cols = ["ref_id", "kind", "title", "number", "year", "series", "link", "status", "mentions"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def link_state(self, ref_ids: Iterable[str]) -> List[dict]:
        """The documents with their link and status (the link finder while splitting: ref_ss/link.py)."""
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT ref_id, kind, title, number, year, series, link, link_status, status, doc_code
                FROM {} WHERE ref_id = ANY(%s) ORDER BY ref_id""").format(self.t(self.doc_t)), (list(ref_ids),))
            cols = ["ref_id", "kind", "title", "number", "year", "series", "link", "link_status", "status", "doc_code"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        self.conn.commit()               # a read only: no open transaction is left while the web search runs
        return rows

    def needed_by(self, ref_id: str, mkp_table: str, limit: int = 3) -> List[dict]:
        """The laws (MKPs) that cite this document, with their text - for the content check of a download."""
        with self.conn.cursor() as cur:
            try:
                cur.execute(sql.SQL("""SELECT DISTINCT ON (l.mkp_element) l.mkp_element, l.mention, m.results
                    FROM {} l JOIN {} m ON m.element = l.mkp_element AND m.version_id = l.version_id
                    AND m.type = 'MKP' AND m.is_latest WHERE l.ref_id = %s ORDER BY l.mkp_element LIMIT %s""")
                            .format(self.t(self.link_t), self.t(mkp_table)), (ref_id, limit))
                rows = cur.fetchall()
            except Exception:
                self.conn.rollback()
                return []
        from transport import get_field
        return [{"mkp": e, "mention": m, "text": get_field({"RESULTS": r or ""}, "text")[:1500]} for e, m, r in rows]

    def update_target_status(self, akp_table: str, view: str = "") -> int:
        """mkp_ref.target_status for every reference, at the end of every run (reference design 2026-11-19):
        INSERTED when the exact clause it needs has an approved, latest AKP - target_key CGSTA17_SCHI needs an AKP of
        CGSTA17_SCHI or of a part of it; an outside document needs an AKP of every part in source_key (no source_key
        yet = PENDING). FAILED stays FAILED until inserted; else PENDING. view: the document -> inserted clauses view."""
        a = self.t(akp_table)
        done = sql.SQL(r"""EXISTS (SELECT 1 FROM {a} x WHERE x.is_latest AND x.validation_status = 'APPROVED'
            AND (split_part(x.source_mkp, '#', 1) = {k}
                 OR split_part(x.source_mkp, '#', 1) LIKE replace({k}, '_', '\_') || '\_%'))""")
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""UPDATE {l} SET target_status = CASE WHEN {done} THEN 'INSERTED'
                WHEN target_status = 'FAILED' THEN 'FAILED' ELSE 'PENDING' END WHERE target_key <> ''""").format(
                l=self.t(self.link_t), done=done.format(a=a, k=sql.SQL("target_key"))))
            n = cur.rowcount
            cur.execute(sql.SQL("""UPDATE {l} SET target_status = CASE WHEN COALESCE(source_key, '') <> ''
                AND NOT EXISTS (SELECT 1 FROM unnest(string_to_array(source_key, ',')) p WHERE NOT {done})
                THEN 'INSERTED' WHEN target_status = 'FAILED' THEN 'FAILED' ELSE 'PENDING' END
                WHERE target_key = ''""").format(l=self.t(self.link_t), done=done.format(a=a, k=sql.SQL("p"))))
            n += cur.rowcount
            if view:
                cur.execute(sql.SQL("""CREATE OR REPLACE VIEW {v} AS
                    SELECT d.ref_id, d.kind, d.title, d.doc_code, d.status AS document_status,
                           count(DISTINCT split_part(x.source_mkp, '#', 1)) AS inserted_clauses,
                           COALESCE(string_agg(DISTINCT split_part(x.source_mkp, '#', 1), ', '), '') AS clauses
                    FROM {d} d LEFT JOIN {a} x ON x.instrument = d.doc_code AND x.is_latest
                         AND x.validation_status = 'APPROVED'
                    WHERE d.doc_code <> ''
                    GROUP BY d.ref_id, d.kind, d.title, d.doc_code, d.status""").format(
                    v=self.t(view), d=self.t(self.doc_t), a=a))
        self.conn.commit()
        return n

    def for_mkp(self, version: str, element: str) -> List[dict]:
        """The references of one MKP: status = the status of the exact law it points to (target_status),
        doc_status = the status of the document's file (ref_document)."""
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT l.scope, l.ref_id, l.target_key, l.relation, l.mention, l.target_status,
                COALESCE(d.status, ''), COALESCE(d.doc_code, '')
                FROM {} l LEFT JOIN {} d ON d.ref_id = l.ref_id WHERE l.version_id=%s AND l.mkp_element=%s
                ORDER BY l.scope, l.ref_id""").format(self.t(self.link_t), self.t(self.doc_t)), (version, element))
            cols = ["scope", "ref_id", "target_key", "relation", "mention", "status", "doc_status", "doc_code"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def held_documents(cfg: dict, conn=None, schema: str = "public", ikp_prefix: str = "ikp") -> Dict[str, str]:
    """ref_id -> document code for every document whose IKP is saved (or, without a connection, that is on disk)."""
    docs = cfg.get("documents") or {}
    have = None
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT DISTINCT doc FROM {} WHERE is_latest").format(
                    sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(f"{ikp_prefix}_versions"))))
                have = {r[0] for r in cur.fetchall()}
        except Exception:
            conn.rollback()
    out = {}
    for code, d in docs.items():
        if have is not None and code not in have:
            continue
        if have is None and d.get("missing"):
            continue
        out[str(d.get("ref_id") or code)] = code
        out[code] = code
    return out
