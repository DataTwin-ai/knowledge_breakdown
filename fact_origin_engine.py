"""DARP LAW ARC - FACT ORIGIN: where every fact's value comes from and how to get it (separate script, 2026-11-23).

    python fact_origin_engine.py                     every fact that has no origin yet (AI, then Python checks it)
    python fact_origin_engine.py --facts F_SSS,F_DOF only these facts (reviewed ones are never changed)
    python fact_origin_engine.py --redo              every fact that is not reviewed, again
    python fact_origin_engine.py --dry-run           show the answers, write nothing
    python fact_origin_engine.py --export            Output/fact_origin_review.xlsx - every row, for review
    python fact_origin_engine.py --import            read that Excel back: rows with reviewed = yes are saved as the
                                                     person's (origin_by PERSON) and the AI never changes them again

Reads the facts AKP made (akp_fact_new), the laws that use them (akp_atom_fact_new -> MKP text) and their TSGs
(approved GKP). Writes ONE row per fact in the table fact_origin (Config/fact_origin.json): document, method
(FIELD / DERIVED / LOOKUP / READ / ASK), how_to_get, uses, as_of, level, fallback, when_missing, cost, ai_level,
example, confidence, sensitive. A fact reused by many laws keeps one origin. Nothing else in the database, AKP or
the governance master is changed; the model and the document list are in Config/fact_origin.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import kp_settings
from fact_origin_ss import origin as fo
from kp_console import flow, log


def settings() -> dict:
    path = kp_settings.resolve("Config/fact_origin.json")
    if not path.is_file():
        raise SystemExit(f"{path} is missing - the document list and the model of the fact origin")
    st = json.loads(path.read_text(encoding="utf-8-sig"))
    docs = {k.upper(): v for k, v in (st.get("documents") or {}).items() if not k.startswith("_")}
    if not docs:
        raise SystemExit("Config/fact_origin.json documents is empty")
    st["documents"] = docs
    return st


def _facts(conn, schema: str, cfg: dict, per_fact: int) -> tuple:
    """(facts, laws per fact) - the fact catalog's own reader (laws + TSGs), plus the law text for the AI."""
    from fact_catalog_ss.catalog import read
    t = cfg["governance_master"]["tables"]
    catalog = read(conn, schema, {"fact": t["fact"], "atom_fact": t["atom_fact"], "akp": t["akp"], "gkp": t["gkp"]}, {})
    from fact_origin_ss.store import OriginStore
    elements = sorted({l["mkp"] for f in catalog for l in f["laws"][:per_fact]})
    text = OriginStore(conn, schema).law_text((cfg.get("mkp") or {}).get("table", t.get("mkp", "mkp")), elements)
    laws = {}
    for f in catalog:
        seen, out = set(), []
        for l in f["laws"]:
            if l["mkp"] in seen:
                continue
            seen.add(l["mkp"])
            out.append({"mkp": l["mkp"].split("#")[0], "tsgs": l["tsgs"], "text": text.get(l["mkp"], "")})
            if len(out) >= per_fact:
                break
        laws[f["fact_id"]] = out
    return catalog, laws


def _model(st: dict, cfg: dict):
    from llm_runtime import call_llm, create_llm_runtime
    m = st.get("model") or {}
    rt = dict(create_llm_runtime(m.get("llm", "CLAUDE"), kp_settings.build_paths(cfg)))
    rt["model"] = m.get("model", "claude-sonnet-5-5")
    return lambda prompt: call_llm(rt, prompt)[0], rt["model"]


def tag(facts: Optional[List[str]] = None, redo: bool = False, dry_run: bool = False, ask=None) -> List[dict]:
    cfg, st = kp_settings.load(), settings()
    from db_insert import get_connection, get_db_schema
    from fact_origin_ss.store import OriginStore
    conn = get_connection()
    schema = get_db_schema()
    store = OriginStore(conn, schema, st.get("table", "fact_origin"))
    saved: List[dict] = []
    try:
        store.ensure()
        have = store.rows()
        catalog, laws = _facts(conn, schema, cfg, int(st.get("laws_per_fact", 3)))
        ids = {f["fact_id"] for f in catalog}
        todo = [f for f in catalog if not (have.get(f["fact_id"]) or {}).get("reviewed")
                and (redo or f["fact_id"] not in have)]
        if facts:
            want = {x.strip() for x in facts if x.strip()}
            todo = [f for f in catalog if f["fact_id"] in want and not (have.get(f["fact_id"]) or {}).get("reviewed")]
            missing = want - ids
            if missing:
                flow(f"FACT ORIGIN : not facts in {cfg['governance_master']['tables']['fact']}: {', '.join(sorted(missing))}")
        flow(f"FACT ORIGIN : {len(catalog)} fact(s), {len(have)} with an origin - {len(todo)} to tag now")
        if not todo:
            return []
        if ask is None:
            ask, model = _model(st, cfg)
        else:
            model = "test"
        size = max(1, int(st.get("batch_size", 15)))
        for i in range(0, len(todo), size):
            batch = todo[i:i + size]
            prompt = fo.build_prompt(st["documents"], batch, laws)
            try:
                answers = fo.parse(ask(prompt))
            except Exception as exc:          # this batch only: the next run tries it again
                flow(f"FACT ORIGIN : batch {i // size + 1} not tagged - {type(exc).__name__}: {str(exc)[:150]}")
                continue
            by_id = {str(a.get("fact_id") or "").strip(): a for a in answers}
            rows = []
            for f in batch:
                a = by_id.get(f["fact_id"])
                if not a:
                    log(f"FACT ORIGIN {f['fact_id']}: no answer from the AI - tried again next run")
                    continue
                row, problems = fo.check(a, st["documents"], ids)
                row.update(fact_id=f["fact_id"], origin_by="AI", reviewed=False, model=model)
                rows.append(row)
                flow(f"  {f['fact_id']:<10} {row['document']:<17} {row['method']:<8} ai {row['ai_level']:<4} "
                     f"{row['confidence']:<6} {f['name'][:40]}" + (f"  CHECK: {problems[0][:60]}" if problems else ""))
            if not dry_run:
                store.save(rows)
            saved += rows
        flow(f"FACT ORIGIN : {len(saved)} fact(s) tagged" + (" (dry run - nothing written)" if dry_run else
             f" - review them: python fact_origin_engine.py --export"))
        return saved
    finally:
        conn.close()


EXPORT_COLS = ["fact_id", "name", "entity", "data_type", "tsgs", "laws"] + [c for c in fo.COLUMNS if c != "fact_id"]


def export() -> Path:
    cfg, st = kp_settings.load(), settings()
    from db_insert import get_connection, get_db_schema
    from fact_origin_ss.store import OriginStore
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    conn = get_connection()
    schema = get_db_schema()
    try:
        store = OriginStore(conn, schema, st.get("table", "fact_origin"))
        store.ensure()
        have = store.rows()
        catalog, laws = _facts(conn, schema, cfg, 50)
    finally:
        conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "fact origin"
    ws.append(EXPORT_COLS)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    low = PatternFill("solid", fgColor="FCE4D6")
    for f in sorted(catalog, key=lambda x: ((have.get(x["fact_id"]) or {}).get("document", "~"), x["fact_id"])):
        o = have.get(f["fact_id"]) or {}
        row = {"fact_id": f["fact_id"], "name": f["name"], "entity": f["entity"], "data_type": f["data_type"],
               "tsgs": ", ".join(sorted(f["tsgs"])), "laws": ", ".join(l["mkp"] for l in laws.get(f["fact_id"], []))}
        row.update({c: o.get(c, "") for c in fo.COLUMNS if c != "fact_id"})
        row["reviewed"] = "yes" if o.get("reviewed") else "no"
        row["sensitive"] = "yes" if o.get("sensitive") else "no"
        ws.append([row.get(c, "") for c in EXPORT_COLS])
        if o.get("confidence") == "low" or not o:
            ws.cell(ws.max_row, EXPORT_COLS.index("confidence") + 1).fill = low
    widths = {"fact_id": 10, "name": 34, "how_to_get": 70, "fallback": 40, "note": 50, "laws": 40, "uses": 30}
    for i, c in enumerate(EXPORT_COLS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(c, 14)
        for r in range(2, ws.max_row + 1):
            ws.cell(r, i).alignment = Alignment(wrap_text=c in widths, vertical="top")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    path = kp_settings.resolve(st.get("review_file", "Output/fact_origin_review.xlsx"))
    kp_settings.require(cfg, "WRITE", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    flow(f"FACT ORIGIN : {len(catalog)} fact(s) -> {path} - fix any column, set reviewed = yes, then "
         "python fact_origin_engine.py --import")
    return path


def import_review(path: Optional[str] = None) -> int:
    cfg, st = kp_settings.load(), settings()
    from openpyxl import load_workbook
    p = Path(path) if path else kp_settings.resolve(st.get("review_file", "Output/fact_origin_review.xlsx"))
    ws = load_workbook(p, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h or "").strip() for h in rows[0]]
    from db_insert import get_connection, get_db_schema
    from fact_origin_ss.store import OriginStore
    conn = get_connection()
    try:
        store = OriginStore(conn, get_db_schema(), st.get("table", "fact_origin"))
        store.ensure()
        keep = []
        for r in rows[1:]:
            d = {h: ("" if v is None else str(v).strip()) for h, v in zip(head, r)}
            if not d.get("fact_id") or d.get("reviewed", "").lower() not in ("yes", "y", "true", "1"):
                continue
            row, problems = fo.check(dict(d, sensitive=d.get("sensitive", "").lower() in ("yes", "true", "1")),
                                     st["documents"], set())
            row.update(fact_id=d["fact_id"], origin_by="PERSON", reviewed=True, confidence="high",
                       note=d.get("note", ""), model=d.get("model", ""))
            if problems:
                flow(f"  {d['fact_id']:<10} saved as you wrote it - note: {problems[0][:90]}")
            keep.append(row)
        n = store.save(keep, overwrite_reviewed=True)
    finally:
        conn.close()
    flow(f"FACT ORIGIN : {n} reviewed fact(s) saved from {p.name} - the AI never changes them again")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LAW ARC fact origin - where each fact's value comes from")
    ap.add_argument("--facts", help="comma separated fact ids (only these)")
    ap.add_argument("--redo", action="store_true", help="tag again every fact that is not reviewed")
    ap.add_argument("--dry-run", action="store_true", help="show the answers, write nothing")
    ap.add_argument("--export", action="store_true", help="write the review Excel")
    ap.add_argument("--import", dest="do_import", action="store_true", help="read the reviewed Excel back")
    ap.add_argument("--file", help="the Excel to import (default Config/fact_origin.json review_file)")
    a = ap.parse_args()
    if a.export:
        export()
    elif a.do_import:
        import_review(a.file)
    else:
        tag(a.facts.split(",") if a.facts else None, a.redo, a.dry_run)
