"""A new PDF of a document that was done before (e.g. an updated CGST Act).

The MKP version of a document is the PDF itself (version_id = "V_" + the start of its hash), so a new PDF starts a
new version. Without this step every law of the new PDF would go through the LLMs again and the old version's rows
would stay is_latest next to the new ones (the governance master would read both).

carry_forward() runs once per document and new version, when MkpRun starts:
  1. the previous version = the latest rows of this document saved under another version_id;
  2. the IKP nodes of the old and the new PDF are compared (status, heading, own text, amendments);
  3. a law whose node did not change is copied to the new version as it is: its MKP + phrases, AKP atoms, fact links,
     time facts, GKP groups and references (insert only, no LLM). Its cluster's BATCH / SUM rows go with it;
  4. a law whose node changed, or that is new, is not copied: MKP / AKP / GKP make it again when its unit runs;
  5. the old version's rows get is_latest = false (kept, not deleted), so every reader sees only the new PDF and a
     law removed from the new PDF drops out.
When the unit of a copied law runs, MKP still compares the saved text with the new PDF (cache_state), so a change
the node check missed (e.g. a parent's lead-in) is made again.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from psycopg2 import sql

_done = set()
_lock = threading.Lock()


def _t(schema: str, name: str):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _exists(cur, schema: str, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f'"{schema}"."{name}"',))
    return cur.fetchone()[0] is not None


def _columns(cur, schema: str, name: str) -> List[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (schema, name))
    return [r[0] for r in cur.fetchall()]


def _copy(cur, schema: str, name: str, where: str, params: tuple, new_values: Dict[str, object]) -> int:
    """INSERT the rows matching `where` again with new_values (version_id ...); times become now()."""
    if not _exists(cur, schema, name):
        return 0
    cols = _columns(cur, schema, name)
    exprs, args = [], []
    for c in cols:
        if c in new_values:
            exprs.append(sql.Placeholder())
            args.append(new_values[c])
        elif c in ("inserted_at", "created_at", "updated_at"):
            exprs.append(sql.SQL("now()"))
        else:
            exprs.append(sql.Identifier(c))
    q = sql.SQL("INSERT INTO {t} ({cols}) SELECT {exprs} FROM {t} WHERE " + where + " ON CONFLICT DO NOTHING").format(
        t=_t(schema, name), cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols), exprs=sql.SQL(", ").join(exprs))
    cur.execute(q, tuple(args) + tuple(params))
    return cur.rowcount or 0


def _retire(cur, schema: str, name: str, old: List[str]) -> int:
    if not _exists(cur, schema, name) or "is_latest" not in _columns(cur, schema, name):
        return 0
    cur.execute(sql.SQL("UPDATE {} SET is_latest=false WHERE version_id = ANY(%s) AND is_latest").format(
        _t(schema, name)), (old,))
    return cur.rowcount or 0


def carry_forward(conn, code: str, new_version: str, new_ikp: int, run_id: str, schema: str, mkp_table: str,
                  ikp_prefix: str, names: dict, say=print) -> Optional[dict]:
    """names: the other tables (db_migrate._names). Returns the counts, or None when there is no older version."""
    key = (schema, mkp_table, code, new_version)
    with _lock:
        if key in _done:
            return None
        report = _carry(conn, code, new_version, new_ikp, run_id, schema, mkp_table, ikp_prefix, names)
        _done.add(key)
    if report:
        say(f"NEW PDF    : {code} {report['old']} -> {new_version}: {report['carried']} law(s) unchanged, copied "
            f"without LLM; {report['redo']} changed (made again when their part runs); "
            f"{report['removed']} not in the new PDF (dropped)")
        say("NEW PDF    : run the whole document once, then the governance master with tsgs \"ALL\" so every "
            "group is rebuilt from the new PDF")
    return report


def _carry(conn, code, new_version, new_ikp, run_id, schema, mkp_table, ikp_prefix, names) -> Optional[dict]:
    M = _t(schema, mkp_table)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('carry'), hashtext(%s))", (code,))
            cur.execute(sql.SQL("SELECT version_id, max(inserted_at) FROM {} WHERE type='MKP' AND is_latest "
                                "AND split_part(element, '_', 1) = %s AND version_id <> %s AND version_id <> 'LEGACY' "
                                "GROUP BY version_id ORDER BY 2 DESC").format(M), (code, new_version))
            olds = [r[0] for r in cur.fetchall()]
            if not olds:
                conn.rollback()
                return None
            old = olds[0]
            nodes, versions = f"{ikp_prefix}_nodes", f"{ikp_prefix}_versions"
            cur.execute(sql.SQL("SELECT version_no FROM {} WHERE doc=%s AND upper(left(pdf_sha256, 16)) = %s "
                                "ORDER BY version_no DESC LIMIT 1").format(_t(schema, versions)), (code, old[2:].upper()))
            r = cur.fetchone()
            same_nodes = set()
            if r:                                        # nodes whose words did not change between the two PDFs
                sig = "md5(concat_ws('|', {a}.status, {a}.heading, {a}.own_text, {a}.amendments::text))"
                cur.execute(sql.SQL("SELECT n.element FROM {t} n JOIN {t} o ON o.doc = n.doc AND o.element = n.element "
                                    "WHERE n.doc=%s AND n.version_no=%s AND o.version_no=%s AND "
                                    + sig.format(a="n") + " = " + sig.format(a="o")).format(t=_t(schema, nodes)),
                            (code, new_ikp, r[0]))
                same_nodes = {x[0] for x in cur.fetchall()}
            cur.execute(sql.SQL("SELECT DISTINCT element FROM {} WHERE version_id=%s AND is_latest AND type='MKP'").format(M),
                        (old,))
            old_mkps = {x[0] for x in cur.fetchall()}
            cur.execute(sql.SQL("SELECT DISTINCT element FROM {} WHERE version_id=%s AND is_latest").format(M),
                        (new_version,))
            have = {x[0] for x in cur.fetchall()}
            cur.execute(sql.SQL("SELECT element FROM {} WHERE doc=%s AND version_no=%s").format(_t(schema, nodes)),
                        (code, new_ikp))
            new_nodes = {x[0] for x in cur.fetchall()}
            carry = sorted(e for e in old_mkps if e.split("#", 1)[0] in same_nodes and e not in have)
            removed = sum(1 for e in old_mkps if e.split("#", 1)[0] not in new_nodes)
            out = {"old": old, "carried": len(carry), "removed": removed,
                   "redo": len(old_mkps) - len(carry) - removed - len(old_mkps & have)}
            if carry:
                v = {"version_id": new_version}
                _copy(cur, schema, mkp_table, "version_id=%s AND is_latest AND type NOT IN ('BATCH','SUM') "
                      "AND element = ANY(%s)", (old, carry), {**v, "run_id": run_id, "source_version": str(new_ikp)})
                _copy(cur, schema, mkp_table, "version_id=%s AND is_latest AND type IN ('BATCH','SUM') AND cluster IN "
                      "(SELECT cluster FROM " + f'"{schema}"."{mkp_table}"' + " WHERE version_id=%s AND element = ANY(%s)) "
                      "AND cluster NOT IN (SELECT cluster FROM " + f'"{schema}"."{mkp_table}"' +
                      " WHERE version_id=%s AND is_latest AND type='BATCH')",
                      (old, old, carry, new_version), {**v, "run_id": run_id, "source_version": str(new_ikp)})
                for table, col, latest in ((names["akp"], "source_mkp", True), (names["fact_edge"], "source_mkp", True),
                                           (names["time_fact"], "amendment_mkp", False), (names["gkp"], "source_mkp", True),
                                           (names["ref_link"], "mkp_element", False)):
                    _copy(cur, schema, table, "version_id=%s" + (" AND is_latest" if latest else "")
                          + f" AND {col} = ANY(%s)", (old, carry), v)
            for table in (mkp_table, names["akp"], names["fact_edge"], names["gkp"]):
                _retire(cur, schema, table, olds)
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
