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
            if not dry_run and found:
                cur.execute(sql.SQL("DELETE FROM {} WHERE ").format(table) + where, params)
        conn.commit()
    finally:
        conn.close()
    flow(f"{len(found)} MKP(s) {'found' if dry_run else 'deleted with their AKP / GKP / references / cached answers'}")
    return len(found)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Delete MKPs and everything made from them")
    ap.add_argument("--mkp", default="", help="one MKP element, e.g. CGSTA17_S7_SS1_Ca#TEXT")
    ap.add_argument("--unit", default="", help="a key prefix, e.g. CGSTA17_S7")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    delete(a.mkp, a.unit, a.dry_run)
