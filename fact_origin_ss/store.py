"""Fact origin - PostgreSQL table fact_origin (one row per fact; created if missing, nothing else is changed)."""
from __future__ import annotations

from typing import Dict, Iterable, List

from psycopg2 import sql

from fact_origin_ss.origin import COLUMNS


class OriginStore:
    def __init__(self, conn, schema: str, table: str = "fact_origin"):
        self.conn, self.schema, self.table = conn, schema, table

    def t(self, name: str = ""):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name or self.table))

    def ensure(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                fact_id text PRIMARY KEY, document text NOT NULL DEFAULT '', method text NOT NULL DEFAULT '',
                how_to_get text NOT NULL DEFAULT '', uses text NOT NULL DEFAULT '', as_of text NOT NULL DEFAULT '',
                level text NOT NULL DEFAULT '', fallback text NOT NULL DEFAULT '', when_missing text NOT NULL DEFAULT '',
                cost text NOT NULL DEFAULT '', ai_level text NOT NULL DEFAULT '', example text NOT NULL DEFAULT '',
                confidence text NOT NULL DEFAULT '', sensitive boolean NOT NULL DEFAULT false,
                origin_by text NOT NULL DEFAULT 'AI', reviewed boolean NOT NULL DEFAULT false,
                note text NOT NULL DEFAULT '', model text NOT NULL DEFAULT '',
                inserted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
                        .format(self.t()))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (document)").format(
                sql.Identifier(f"{self.table}_document_idx"), self.t()))
        self.conn.commit()

    def rows(self) -> Dict[str, dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT {} FROM {} ORDER BY fact_id").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS), self.t()))
            out = {r[0]: dict(zip(COLUMNS, r)) for r in cur.fetchall()}
        self.conn.commit()
        return out

    def save(self, rows: Iterable[dict], overwrite_reviewed: bool = False) -> int:
        """Insert / update. A reviewed row is changed only when overwrite_reviewed (the person's own import)."""
        n = 0
        cols = [c for c in COLUMNS if c != "fact_id"]
        with self.conn.cursor() as cur:
            for r in rows:
                vals = [r.get("fact_id")] + [r.get(c) if r.get(c) is not None else ("" if c not in
                                             ("sensitive", "reviewed") else False) for c in cols]
                guard = sql.SQL("") if overwrite_reviewed else sql.SQL(" WHERE NOT o.reviewed")
                cur.execute(sql.SQL("""INSERT INTO {t} AS o ({all}) VALUES ({ph}) ON CONFLICT (fact_id) DO UPDATE SET
                    {sets}, updated_at = now(){guard}""").format(
                    t=self.t(), all=sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS),
                    ph=sql.SQL(", ").join(sql.Placeholder() for _ in COLUMNS),
                    sets=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in cols),
                    guard=guard), vals)
                n += cur.rowcount
        self.conn.commit()
        return n

    def law_text(self, mkp_table: str, elements: List[str]) -> Dict[str, str]:
        """The text of the laws (latest MKP) that use the facts - context for the AI."""
        if not elements:
            return {}
        with self.conn.cursor() as cur:
            try:
                cur.execute(sql.SQL("""SELECT DISTINCT ON (element) element, results FROM {} WHERE type = 'MKP'
                    AND is_latest AND element = ANY(%s) ORDER BY element""").format(self.t(mkp_table)),
                            (list(elements),))
                rows = cur.fetchall()
            except Exception:
                self.conn.rollback()
                return {}
        self.conn.commit()
        from transport import get_field
        return {e: " ".join(str(get_field({"RESULTS": r or ""}, "text") or "").split()) for e, r in rows}
