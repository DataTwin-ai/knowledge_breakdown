"""DARP LAW ARC - FACT CATALOG: the fact flow on top of the governance masters (no LLM).

    python fact_catalog_engine.py                             build Governance_Master/fact_catalog.yaml + tables
    python fact_catalog_engine.py --facts F_INVOICE_DATE,F_SUPPLIER_STATE,F_HSN
                                                              which TSGs those facts can check (READY / PARTIAL)

Built after the governance masters (main.py, permission.json run.fact_catalog). For every fact: what it is, how it
is obtained (DOCUMENT / DERIVED / LOOKUP / ASK, Config/fact_kinds.json) and every law (atom, MKP, TSG) that uses
it - "invoice date is a fact; these are the laws it can check". The governance master yaml files keep the engine's
format; the catalog is the extra file that picks which of them run for a document.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

import yaml
from psycopg2 import sql

import kp_settings
from fact_catalog_ss.catalog import read
from fact_catalog_ss.router import route
from kp_console import flow


def _tables(cfg: dict) -> dict:
    t = cfg["governance_master"]["tables"]
    fact = t.get("fact", "akp_fact")
    return {"fact": fact, "atom_fact": fact.replace("_fact", "_atom_fact") if fact.endswith("_fact") else fact + "_edge",
            "akp": t.get("akp", "akp"), "gkp": t.get("gkp", "gkp")}


def _kinds(cfg: dict) -> dict:
    path = kp_settings.resolve(cfg["fact_catalog"].get("fact_kinds_file", "Config/fact_kinds.json"))
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def _names(cfg: dict) -> dict:
    from gkp_ss import taxonomy as tax
    _, subs = tax.load(kp_settings.resolve(cfg["gkp"]["taxonomy_file"]))
    return {s["tsg_id"]: s["name"] for s in subs}


def build_catalog(cfg: Optional[dict] = None, conn=None) -> dict:
    cfg = cfg or kp_settings.load()
    fc = cfg["fact_catalog"]
    own = conn is None
    if own:
        from db_insert import get_connection
        conn = get_connection()
    from db_insert import get_db_schema
    schema = get_db_schema()
    try:
        catalog = read(conn, schema, _tables(cfg), _kinds(cfg))
        names = _names(cfg)
        out_dir = kp_settings.resolve(cfg["governance_master"]["output_dir"])
        kp_settings.require(cfg, "WRITE", out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / fc.get("file", "fact_catalog.yaml")
        doc = {"about": "Every fact and the laws it can check. kind = how the fact is obtained "
                        "(DOCUMENT / DERIVED / LOOKUP / ASK); reviewed = false means the kind is a default - "
                        "confirm it in Config/fact_kinds.json.",
               "facts": [{"fact_id": f["fact_id"], "name": f["name"], "entity": f["entity"], "data_type": f["data_type"],
                          "kind": f["kind"], "how": f["how"], "reviewed": f["reviewed"],
                          "derived_from": f["derived_from"], "checks": f["checks"],
                          "tsgs": [{"tsg": t, "name": names.get(t, ""), "atoms": n} for t, n in sorted(f["tsgs"].items())],
                          "laws": f["laws"]} for f in catalog]}
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
        T = lambda n: sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(n))
        with conn.cursor() as cur:            # derived tables: rebuilt whole each time (no history needed)
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (fact_id text PRIMARY KEY, name text, entity text,
                data_type text, kind text, how text, reviewed boolean, derived_from jsonb, python_checks integer,
                judge_checks integer, tsgs jsonb, updated_at timestamptz NOT NULL DEFAULT now())""").format(T(fc["table"])))
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (fact_id text NOT NULL, tsg_id text NOT NULL,
                source_mkp text NOT NULL, atom text NOT NULL, check_by text NOT NULL)""").format(T(fc["index_table"])))
            cur.execute(sql.SQL("DELETE FROM {}").format(T(fc["table"])))
            cur.execute(sql.SQL("DELETE FROM {}").format(T(fc["index_table"])))
            links = 0
            for f in catalog:
                cur.execute(sql.SQL("""INSERT INTO {} (fact_id, name, entity, data_type, kind, how, reviewed, derived_from,
                    python_checks, judge_checks, tsgs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)""").format(
                    T(fc["table"])), (f["fact_id"], f["name"], f["entity"], f["data_type"], f["kind"], f["how"],
                                      f["reviewed"], json.dumps(f["derived_from"]), f["checks"]["python"],
                                      f["checks"]["judge"], json.dumps(f["tsgs"])))
                for law in f["laws"]:
                    for t in law["tsgs"] or [""]:
                        cur.execute(sql.SQL("INSERT INTO {} (fact_id, tsg_id, source_mkp, atom, check_by) "
                                            "VALUES (%s,%s,%s,%s,%s)").format(T(fc["index_table"])),
                                    (f["fact_id"], t, law["mkp"], law["atom"], law["check"]))
                        links += 1
        conn.commit()
        return {"facts": len(catalog), "links": links, "file": str(path), "catalog": catalog}
    finally:
        if own:
            conn.close()


def which_laws(facts: List[str], cfg: Optional[dict] = None) -> List[dict]:
    cfg = cfg or kp_settings.load()
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        catalog = read(conn, get_db_schema(), _tables(cfg), _kinds(cfg))
    finally:
        conn.close()
    return route(facts, catalog, _names(cfg))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LAW ARC fact catalog")
    ap.add_argument("--facts", default="", help="comma-separated fact ids a document gives")
    a = ap.parse_args()
    if a.facts:
        for r in which_laws([x.strip() for x in a.facts.split(",") if x.strip()]):
            flow(f"{r['status']:<8} {r['tsg']} {r['name'][:40]:<40} {len(r['available'])}/{r['facts']} facts"
                 + (f" - missing: {', '.join(r['missing'][:6])}" if r["missing"] else ""))
    else:
        r = build_catalog()
        flow(f"FACT CATALOG : {r['facts']} fact(s), {r['links']} law link(s) - {r['file']}")
