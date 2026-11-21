"""Delete wrong MKPs and everything made from them, so the next run makes them again from the law.

    python kp_delete.py --mkp CGSTA17_S7_SS1_Ca#TEXT      one MKP
    python kp_delete.py --unit CGSTA17_S7                  every MKP of section 7 (prefix)
    python kp_delete.py --unit CGSTA17_S7 --dry-run        only show what would go

Needs permission.json access.database_delete = true. The database triggers (db_migrate.py) delete the AKP atoms,
fact links, GKP groups, governance master rows, cross references and cached LLM answers of each MKP. You can also
delete MKP rows yourself in pgAdmin / psql: the same triggers run.
Then run main.py again for that scope; the governance master of the affected TSGs is rebuilt at the end.
"""
from __future__ import annotations

import argparse

from psycopg2 import sql

import kp_settings
from kp_console import flow


def delete(element: str = "", unit: str = "", dry_run: bool = False) -> int:
    cfg = kp_settings.load()
    if not dry_run:
        kp_settings.require(cfg, "DELETE")
    if not element and not unit:
        raise ValueError("give --mkp or --unit")
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    table = sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(cfg["mkp"]["table"]))
    where = sql.SQL("type='MKP' AND element = %s") if element else sql.SQL(
        "type='MKP' AND (split_part(element, '#', 1) = %s OR element LIKE %s)")
    params = (element,) if element else (unit, unit + "\\_%")
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT DISTINCT element FROM {} WHERE ").format(table) + where + sql.SQL(" ORDER BY 1"),
                        params)
            found = [r[0] for r in cur.fetchall()]
            for e in found[:40]:
                flow(f"  {'would delete' if dry_run else 'delete'} : {e}")
            if len(found) > 40:
                flow(f"  ... and {len(found) - 40} more")
            others = _pointing_here(cur, cfg, found)
            if not dry_run and found:
                cur.execute(sql.SQL("DELETE FROM {} WHERE ").format(table) + where, params)
        conn.commit()
        if found and not dry_run:
            _references_back_to_pending(conn, cfg, found)
        for line in others:
            flow(line)
    finally:
        conn.close()
    flow(f"{len(found)} MKP(s) {'found' if dry_run else 'deleted with their AKP / GKP / references / cached answers'}")
    return len(found)


def _pointing_here(cur, cfg: dict, found: list) -> list:
    """Bug list F23: other laws whose SAME atom (A_REF) is an atom of a law being deleted lose that atom - listed,
    so they are run again (or mapped again by the next run's SAME check)."""
    from db_insert import get_db_schema
    akp = sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(cfg["akp"]["table"]))
    try:
        cur.execute(sql.SQL("""SELECT DISTINCT r.source_mkp, r.akp_id FROM {} r WHERE r.is_latest AND r.type='A_REF'
            AND NOT r.source_mkp = ANY(%s) AND r.akp_id IN (SELECT a.akp_id FROM {} a WHERE a.is_latest
            AND a.type='A' AND a.source_mkp = ANY(%s)) ORDER BY 1""").format(akp, akp), (found, found))
        rows = cur.fetchall()
    except Exception:
        cur.connection.rollback()
        return []
    return [f"  SAME atom lost : {m} used {a} of a deleted law - run {m.split('#')[0]} again "
            "(or the next run's SAME check maps it)" for m, a in rows]


def _references_back_to_pending(conn, cfg: dict, found: list) -> None:
    """Bug list F23 (user's rule): the references of OTHER laws to a deleted law are taken back - a self reference
    to it, or an outside-document part that was deleted, goes back to PENDING (not inserted); the link to the deleted
    part (source_key) is removed. The deleted law's own references go with it (db_migrate.py trigger)."""
    from db_insert import get_db_schema
    rc = cfg.get("refs") or {}
    schema = get_db_schema()
    link = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(rc.get("link_table", "mkp_ref")))
    units = sorted({f.split("#")[0] for f in found})
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""UPDATE {} SET source_key = '', target_status = 'PENDING' WHERE source_key <> ''
                AND EXISTS (SELECT 1 FROM unnest(string_to_array(source_key, ',')) k WHERE k = ANY(%s))""")
                        .format(link), (units,))
            parts = cur.rowcount
        conn.commit()
        from ref_ss.store import RefStore
        RefStore(conn, schema, rc.get("document_table", "ref_document"), rc.get("link_table", "mkp_ref")) \
            .update_target_status(cfg["akp"]["table"], (cfg.get("refs") or {}).get("parts_view", ""))
        flow(f"  references     : statuses checked again (laws pointing to a deleted law are PENDING now)"
             + (f"; {parts} outside-document part link(s) removed" if parts else ""))
    except Exception as exc:
        conn.rollback()
        flow(f"  references     : not updated ({type(exc).__name__}: {str(exc)[:120]}) - the next run updates them")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Delete MKPs and everything made from them")
    ap.add_argument("--mkp", default="", help="one MKP element, e.g. CGSTA17_S7_SS1_Ca#TEXT")
    ap.add_argument("--unit", default="", help="a key prefix, e.g. CGSTA17_S7")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    delete(a.mkp, a.unit, a.dry_run)
