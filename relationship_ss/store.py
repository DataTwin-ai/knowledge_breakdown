"""RELATIONSHIP / store: reads the latest AKP / fact link / GKP / governance master rows and keeps kp_relation.

kp_relation is versioned per MKP (column mkp), insert only:
    - each MKP has a fingerprint of what it is built from (AKP version and rows, fact links, GKP groups,
      governance masters). It is saved on the MKP's rows;
    - a run compares the fingerprints of the database with the saved ones in ONE query per table and reads only the
      MKPs that are new or changed; an unchanged MKP is not read at all;
    - a changed MKP: its rows get is_latest = false and the new rows are inserted as version n+1;
    - an MKP no longer in the database (deleted / not latest): its rows get is_latest = false.
Deleting an MKP (kp_delete.py) deletes its kp_relation rows through the cascade in db_migrate.py.
"""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Tuple

from psycopg2 import sql
from psycopg2.extras import execute_values

from .build import BUILD_VERSION, COLS

ROW_COLS = ("mkp", "instrument", "version_id", "version", "akp_version", "fingerprint") + COLS


class RelationStore:
    def __init__(self, conn, schema: str, table: str, names: Dict[str, str]):
        """names: akp, fact_edge, fact, gkp, gm (table names)."""
        self.conn, self.schema, self.table, self.n = conn, schema, table, names

    def t(self, name: str = None):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name or self.table))

    def _has(self, cur, name: str) -> bool:
        cur.execute("SELECT to_regclass(%s)", (f'"{self.schema}"."{name}"',))
        return cur.fetchone()[0] is not None

    def _cols(self, cur, name: str) -> set:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                    (self.schema, name))
        return {r[0] for r in cur.fetchall()}

    def ensure(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                rel_id bigserial PRIMARY KEY, mkp text NOT NULL, instrument text,
                from_type text NOT NULL, from_id text NOT NULL, from_name text, relation text NOT NULL,
                to_type text NOT NULL, to_id text NOT NULL, to_name text, link text NOT NULL DEFAULT 'DIRECT', via text,
                version integer NOT NULL, akp_version integer, version_id text, fingerprint text NOT NULL,
                is_latest boolean NOT NULL DEFAULT true, inserted_at timestamptz NOT NULL DEFAULT now())""").format(self.t()))
            # a kp_relation made by the first builder: same rows, older column names (kept, rebuilt on this run)
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                        (self.schema, self.table))
            have = {r[0] for r in cur.fetchall()}
            for old, new in (("molecule", "mkp"), ("from_label", "from_name"), ("to_label", "to_name")):
                if old in have and new not in have:
                    cur.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(
                        self.t(), sql.Identifier(old), sql.Identifier(new)))
            for col, typ in (("rel_id", "bigserial"), ("link", "text NOT NULL DEFAULT 'DIRECT'"), ("via", "text"),
                             ("handler", "text"), ("in_force", "text")):
                if col not in have:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + typ).format(
                        self.t(), sql.Identifier(col)))
            for col in ("citation", "from_element", "to_element", "hops"):        # never in a delivered version
                if col in have:
                    cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(self.t(), sql.Identifier(col)))
            for name in ("mol", "to", "from", "rel"):
                cur.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(self.schema, f"{self.table}_{name}_idx")))
            for name, cols in (("mkp", "(mkp, is_latest)"), ("to", "(to_id, relation) WHERE is_latest"),
                               ("from", "(from_id) WHERE is_latest"), ("rel", "(relation, link) WHERE is_latest")):
                cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} " + cols).format(
                    sql.Identifier(f"{self.table}_{name}_ix"), self.t()))
        self.conn.commit()

    # ------------------------------------------------------------ what changed (cheap: grouped queries only)
    def current(self, prefix: str = "") -> Dict[str, Tuple[str, str, int]]:
        """molecule -> (fingerprint, version_id, akp_version) of the latest rows in the database."""
        like = (prefix or "") + "%"
        parts: Dict[str, List[str]] = {}
        info: Dict[str, Tuple[str, int]] = {}
        with self.conn.cursor() as cur:
            if not self._has(cur, self.n["akp"]):
                return {}
            cur.execute(sql.SQL("""SELECT source_mkp, max(version_id), max(akp_version), count(*),
                                          md5(string_agg(type || ':' || akp_id, ','
                                                         ORDER BY type, akp_id))
                                   FROM {} WHERE is_latest AND source_mkp LIKE %s GROUP BY source_mkp""").format(
                self.t(self.n["akp"])), (like,))
            for m, v, av, n, h in cur.fetchall():
                info[m] = (v, int(av or 0))
                parts[m] = [f"akp {av} {n} {h}"]
            for key, q in (
                ("fact_edge", "SELECT source_mkp, md5(string_agg(atom || '>' || fact_id, ',' ORDER BY atom, fact_id)) "
                              "FROM {} WHERE is_latest AND source_mkp LIKE %s GROUP BY source_mkp"),
                ("gkp", "SELECT source_mkp, md5(string_agg(akp_element || '>' || tsg_id || ':' || COALESCE(tsg_name, ''), ',' "
                        "ORDER BY akp_element, tsg_id)) FROM {} WHERE is_latest AND status = 'APPROVED' AND tsg_id <> '' "
                        "AND source_mkp LIKE %s GROUP BY source_mkp"),
                ("gm", "SELECT source_mkp, md5(string_agg(DISTINCT tsg_id, ',')) FROM {} "
                       "WHERE source_mkp LIKE %s GROUP BY source_mkp")):
                if not self._has(cur, self.n[key]):
                    continue
                cur.execute(sql.SQL(q).format(self.t(self.n[key])), (like,))
                for m, h in cur.fetchall():
                    if m in parts:
                        parts[m].append(f"{key} {h}")
        return {m: (hashlib.sha256(("v" + BUILD_VERSION + "|" + "|".join(sorted(p))).encode()).hexdigest(), *info[m])
                for m, p in parts.items()}

    def saved(self, prefix: str = "") -> Dict[str, str]:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT DISTINCT mkp, fingerprint FROM {} WHERE is_latest AND mkp LIKE %s")
                        .format(self.t()), ((prefix or "") + "%",))
            return {m: fp for m, fp in cur.fetchall()}

    # ------------------------------------------------------------ the source rows of some MKPs
    def source(self, molecules: List[str]) -> Dict[str, dict]:
        out = {m: {"akp": [], "edges": [], "gkp": [], "gm": []} for m in molecules}
        facts, same_ids = set(), set()
        with self.conn.cursor() as cur:
            mid_col = sql.SQL("mkp_id") if "mkp_id" in self._cols(cur, self.n["akp"]) else sql.SQL("NULL")
            cur.execute(sql.SQL("SELECT source_mkp, type, akp_id, concept, results, CASE WHEN type = 'A_REF' THEN akp_id END, {} FROM {} "
                                "WHERE is_latest AND source_mkp = ANY(%s) ORDER BY source_mkp, ordinal")
                        .format(mid_col, self.t(self.n["akp"])), (molecules,))
            for m, t, el, c, res, same, mid in cur.fetchall():
                out[m]["akp"].append({"type": t, "element": el, "concept": c, "results": res, "same_akp_id": same})
                if mid:
                    out[m].setdefault("mkp_id", mid)        # the MKP's short id (M_SIAFOS) names its node
                if same:
                    same_ids.add(same)
            if self._has(cur, self.n["fact_edge"]):
                cur.execute(sql.SQL("SELECT DISTINCT source_mkp, atom, fact_id FROM {} WHERE is_latest AND source_mkp = ANY(%s)")
                            .format(self.t(self.n["fact_edge"])), (molecules,))
                for m, a, f in cur.fetchall():
                    out[m]["edges"].append({"atom": a, "fact_id": f})
                    facts.add(f)
            if self._has(cur, self.n["gkp"]):
                cur.execute(sql.SQL("SELECT DISTINCT source_mkp, akp_element, tsg_id, tsg_name, tg_id, tg_name FROM {} "
                                    "WHERE is_latest AND status = 'APPROVED' AND tsg_id <> '' AND source_mkp = ANY(%s)")
                            .format(self.t(self.n["gkp"])), (molecules,))
                for m, el, tsg, tn, tg, tgn in cur.fetchall():
                    out[m]["gkp"].append({"akp_element": el, "tsg_id": tsg, "tsg_name": tn, "tg_id": tg, "tg_name": tgn})
            if self._has(cur, self.n["gm"]):
                cur.execute(sql.SQL("SELECT DISTINCT source_mkp, tsg_id, tsg_name FROM {} WHERE source_mkp = ANY(%s)")
                            .format(self.t(self.n["gm"])), (molecules,))
                for m, tsg, tn in cur.fetchall():
                    out[m]["gm"].append({"tsg_id": tsg, "tsg_name": tn})
            for r in (x for v in out.values() for x in v["akp"] if x["type"] == "A"):
                from .build import fields, items
                facts.update(items(fields(r["results"] or "").get("facts")))
            names: Dict[str, str] = {}
            if facts and self._has(cur, self.n["fact"]):
                concept = sql.SQL("concept") if "concept" in self._cols(cur, self.n["fact"]) else sql.SQL("''")
                cur.execute(sql.SQL("SELECT fact_id, name, {} FROM {} WHERE fact_id = ANY(%s)").format(
                    concept, self.t(self.n["fact"])), (sorted(facts),))
                # the fact's 2-3 word concept goes with its name: "Form Of Supply · Supply Form"
                same = lambda a, b: " ".join(w for w in str(a).lower().split() if w not in ("of", "the")) == \
                    " ".join(w for w in str(b).lower().split() if w not in ("of", "the"))
                names = {f: f"{n} · {c}" if c and not same(c, n) else n for f, n, c in cur.fetchall()}
            same: Dict[str, dict] = {}
            if same_ids:                         # A_REF: the atom it is the SAME as (in another MKP)
                cur.execute(sql.SQL("SELECT DISTINCT ON (akp_id) akp_id, source_mkp, akp_id, results FROM {} "
                                    "WHERE is_latest AND akp_id = ANY(%s) ORDER BY akp_id, akp_version DESC")
                            .format(self.t(self.n["akp"])), (sorted(same_ids),))
                from .build import fields
                for aid, m, el, res in cur.fetchall():
                    same[aid] = {"source_mkp": m, "element": el, "label": fields(res or "").get("statement", "")}
        for v in out.values():
            v["names"], v["same"] = names, same
        return out

    # ------------------------------------------------------------ write
    def save(self, built: Dict[str, Tuple[List[dict], str, str, int]]) -> None:
        """built: mkp -> (rows, fingerprint, version_id, akp_version). Retire + insert, one transaction."""
        if not built:
            return
        try:
            with self.conn.cursor() as cur:
                mols = sorted(built)
                cur.execute(sql.SQL("SELECT mkp, max(version) FROM {} WHERE mkp = ANY(%s) GROUP BY mkp")
                            .format(self.t()), (mols,))
                last = {m: int(v) for m, v in cur.fetchall()}
                cur.execute(sql.SQL("UPDATE {} SET is_latest = false WHERE mkp = ANY(%s) AND is_latest")
                            .format(self.t()), (mols,))
                values = []
                for m in mols:
                    rows, fp, vid, av = built[m]
                    inst = m.split("_", 1)[0]
                    values += [(m, inst, vid, last.get(m, 0) + 1, av, fp) + tuple(r[c] for c in COLS) for r in rows]
                if values:
                    execute_values(cur, sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                        self.t(), sql.SQL(", ").join(map(sql.Identifier, ROW_COLS))).as_string(cur), values, page_size=1000)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def retire(self, molecules: Iterable[str]) -> int:
        mols = sorted(molecules)
        if not mols:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("UPDATE {} SET is_latest = false WHERE mkp = ANY(%s) AND is_latest").format(self.t()),
                        (mols,))
            n = cur.rowcount
        self.conn.commit()
        return n

    def now(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT now()")
            return cur.fetchone()[0]

    def latest_rows(self, fresh_since=None) -> List[dict]:
        """The latest rows; fresh = the MKP's links were (re)built at or after fresh_since (this run)."""
        cols = ("mkp", "instrument") + COLS
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT {}, inserted_at >= %s FROM {} WHERE is_latest ORDER BY mkp, from_type, from_id")
                        .format(sql.SQL(", ").join(map(sql.Identifier, cols)), self.t()),
                        (fresh_since or "infinity",))
            return [dict(zip(cols + ("fresh",), r)) for r in cur.fetchall()]
