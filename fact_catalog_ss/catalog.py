"""Fact catalog / read the fact network from the database (no LLM).

One row per fact (akp_fact): what it is, how it is obtained (kind), and every law that uses it:
atom -> MKP (law) -> TSG (approved GKP groups). The same fact id across all laws is already made by AKP
(akp_ss/facts.py), so "invoice date" read once can check every law that uses it.

kind  how the validation gets the fact (Config/fact_kinds.json, reviewed by people; default from the data):
      DOCUMENT  on the document / register itself (invoice date, GSTIN, HSN, value)
      DERIVED   worked out by Python from other facts (inter-State from two state codes, age from a date)
      LOOKUP    from a table (rate from HSN, registration status from GSTIN)
      ASK       not on the document: asked from the user / ERP
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List

from psycopg2 import sql

KINDS = ("DOCUMENT", "DERIVED", "LOOKUP", "ASK")


def _t(schema: str, name: str):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _rows(conn, q, params=()):
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchall()


def read(conn, schema: str, tables: dict, kinds: dict) -> List[dict]:
    """tables: fact, atom_fact, akp, gkp table names. kinds: Config/fact_kinds.json content."""
    facts = {}
    for fid, name, entity, dtype, unit, options, derived, nl in _rows(conn, sql.SQL(
            "SELECT fact_id, name, entity, data_type, unit, options, derived_from, nl FROM {} ORDER BY fact_id").format(
            _t(schema, tables["fact"]))):
        derived = derived if isinstance(derived, list) else json.loads(derived or "[]")
        given = kinds.get(fid) or {}
        kind = str(given.get("kind") or ("DERIVED" if derived else "DOCUMENT")).upper()
        facts[fid] = {"fact_id": fid, "name": name, "entity": entity or "", "data_type": dtype or "", "unit": unit or "",
                      "options": options if isinstance(options, list) else json.loads(options or "[]"),
                      "derived_from": derived, "description": nl or "", "kind": kind if kind in KINDS else "DOCUMENT",
                      "how": given.get("how", ""), "reviewed": bool(given), "laws": [], "tsgs": {},
                      "checks": {"python": 0, "judge": 0}}
    # atom -> fact edges of the latest AKP versions, with the atom's order (LOW = Python, HIGH = judge);
    # akp_atom_fact.atom is the atom's abbreviated id = akp.akp_id (akp has no element column)
    edges = _rows(conn, sql.SQL("""SELECT e.fact_id, e.source_mkp, e.atom, COALESCE(a.results, '')
        FROM {} e LEFT JOIN {} a ON a.source_mkp = e.source_mkp AND a.akp_id = e.atom AND a.version_id = e.version_id
             AND COALESCE(a.is_latest, true)
        WHERE e.is_latest""").format(_t(schema, tables["atom_fact"]), _t(schema, tables["akp"])))
    groups = defaultdict(set)
    for mkp, atom, tsg in _rows(conn, sql.SQL("""SELECT source_mkp, akp_element, tsg_id FROM {}
        WHERE is_latest AND status = 'APPROVED' AND tsg_id <> ''""").format(_t(schema, tables["gkp"]))):
        groups[(mkp, atom)].add(tsg)
    for fid, mkp, atom, results in edges:
        f = facts.get(fid)
        if f is None:
            continue
        judge = "order: HIGH" in results or "HEAVY_MODEL" in results
        f["checks"]["judge" if judge else "python"] += 1
        tsgs = sorted(groups.get((mkp, atom), ()))
        f["laws"].append({"mkp": mkp, "atom": atom, "tsgs": tsgs, "check": "JUDGE" if judge else "PYTHON"})
        for t in tsgs:
            f["tsgs"][t] = f["tsgs"].get(t, 0) + 1
    return list(facts.values())


def tsg_needs(catalog: List[dict]) -> Dict[str, set]:
    """TSG -> the facts its laws use."""
    out: Dict[str, set] = defaultdict(set)
    for f in catalog:
        for t in f["tsgs"]:
            out[t].add(f["fact_id"])
    return dict(out)
