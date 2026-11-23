"""DARP LAW ARC - OBSERVER: one interactive page of the governance masters (separate script, 2026-11-24).

    python observer_engine.py                          Governance_Master/observer.html (open it in a browser)
    python observer_engine.py --dir Governance_Master_Old_2 --out Output/observer_old2.html
    python observer_engine.py --no-db                  only the files (no fact origin / reference status)

Reads (nothing is written back):
    <governance master dir>/TGxx/TSGxx_rulebook.yaml + report/TSGxx_report.xlsx, fact_catalog.yaml
    Config/fact_origin.json (documents) + table fact_origin (where each fact comes from)
    mkp_ref (the references each law makes: SELF / CROSS, part used, INSERTED / PENDING)
The page: Document and TG / TSG dropdowns, centre line Fact / Atom / MKP / Product, click a node to follow its nerve,
Back to TSG, Full connect, the details of the clicked node, and plain-words definitions. One file, no server.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

import kp_settings
from kp_console import flow
from observer_ss import data as od


def _db(cfg: dict, origin_table: str) -> tuple:
    """(fact origin rows, mkp_ref rows) - empty when the database cannot be read (the page still works)."""
    try:
        from psycopg2 import sql
        from db_insert import get_connection, get_db_schema
        conn = get_connection()
    except Exception as exc:
        flow(f"OBSERVER : database not read ({type(exc).__name__}) - no fact origin / reference status on the page")
        return {}, []
    schema = get_db_schema()
    origin, refs = {}, []
    T = lambda n: sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(n))
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(sql.SQL("SELECT * FROM {}").format(T(origin_table)))
                cols = [c[0] for c in cur.description]
                origin = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
            except Exception:
                conn.rollback()
                flow(f"OBSERVER : table {origin_table} not found - run python fact_origin_engine.py to tag the facts")
            try:
                link_t = (cfg.get("refs") or {}).get("link_table", "mkp_ref")
                cur.execute(sql.SQL("""SELECT mkp_element, scope, ref_id, target_key, source_key, target_status
                    FROM {}""").format(T(link_t)))
                refs = [dict(zip(["mkp_element", "scope", "ref_id", "target_key", "source_key", "target_status"], r))
                        for r in cur.fetchall()]
            except Exception:
                conn.rollback()
    finally:
        conn.close()
    return origin, refs


def run(gm_dir: Optional[str] = None, out: Optional[str] = None, use_db: bool = True) -> Path:
    cfg = kp_settings.load()
    gdir = kp_settings.resolve(gm_dir) if gm_dir else kp_settings.resolve(cfg["governance_master"]["output_dir"])
    if not gdir.is_dir():
        raise SystemExit(f"{gdir} does not exist - run main.py (governance master) first, or give --dir")
    fo_path = kp_settings.resolve("Config/fact_origin.json")
    fo = json.loads(fo_path.read_text(encoding="utf-8-sig")) if fo_path.is_file() else {}
    documents = {k.upper(): {"about": v.get("about", ""), "fields": v.get("fields", [])}
                 for k, v in (fo.get("documents") or {}).items() if not k.startswith("_")}
    origin, refs = _db(cfg, fo.get("table", "fact_origin")) if use_db else ({}, [])
    graph = od.build(gdir, origin=origin, documents=documents, refs=refs, catalog=gdir / "fact_catalog.yaml")
    if not graph["tsgs"]:
        raise SystemExit(f"no TSG rulebook yaml in {gdir}")
    page = (Path(__file__).resolve().parent / "observer_ss" / "page.html").read_text(encoding="utf-8")
    html = page.replace("/*DATA*/null", od.to_json(graph))
    path = kp_settings.resolve(out) if out else gdir / "observer.html"
    kp_settings.require(cfg, "WRITE", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    n = graph["nodes"]
    flow(f"OBSERVER : {len(graph['tsgs'])} TSG(s), {sum(1 for x in n.values() if x['type'] == 'F')} facts, "
         f"{sum(1 for x in n.values() if x['type'] == 'A')} atoms, {len(graph['modules'])} laws, "
         f"{len(origin)} fact origin(s), {len(refs)} reference(s) -> {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LAW ARC observer - one interactive page of the governance masters")
    ap.add_argument("--dir", help="governance master folder (default: governance_master.output_dir)")
    ap.add_argument("--out", help="the html file (default: <dir>/observer.html)")
    ap.add_argument("--no-db", action="store_true", help="do not read the database")
    a = ap.parse_args()
    run(a.dir, a.out, not a.no_db)
