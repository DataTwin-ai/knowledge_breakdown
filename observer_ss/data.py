"""Observer - the data: governance master yaml + reports + fact catalog + fact origin + references (2026-11-24).

Everything the page shows comes from files and tables that already exist; nothing is written back.
    Governance_Master/TGxx/TSGxx_rulebook.yaml   facts (with their level), atoms (predicates), groups, law expressions
                                                 (outcomes / overrides), products, compounds
    Governance_Master/TGxx/report/TSGxx_report.xlsx  which MKP each molecule (M_...) is, and why each law is in
    Governance_Master/fact_catalog.yaml          fact entity / data type (optional)
    fact_origin table + Config/fact_origin.json  where each fact comes from (optional - empty until tagged)
    mkp_ref                                      references of each law and their status (optional)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

ID = re.compile(r"\b(?:M_[A-Z0-9_]+|IH_[A-Z0-9_]+|F_[A-Z0-9_]+)\b")
FACT = re.compile(r"\bF_[A-Z0-9_]+\b")
DOC_NAMES = {"CGSTA17": "CGST Act", "IGSTA17": "IGST Act", "UTGSTA17": "UTGST Act", "CGSTR17": "CGST Rules"}
_PART = [(r"SCH([IVXL]+)", "Sch. {}"), (r"S(\d+[A-Z]*)", "s.{}"), (r"SS(\d+[A-Z]?)", "({})"), (r"SR(\d+[A-Z]?)", "({})"),
         (r"C(\d+[A-Z]*|[a-z]+)", "({})"), (r"SC([ivxl]+)", "({})"), (r"EN(\d+)", "para {}"), (r"P(\d+)", "proviso {}"),
         (r"E(\d+)", "Expl. {}"), (r"R(\d+[A-Z]*)", "rule {}")]


def doc_of(element: str) -> str:
    return (element or "").split("_", 1)[0]


def citation(element: str) -> str:
    """CGSTA17_SCHIII_EN8_Cb_E1#TEXT -> 'CGST Act Sch. III para 8(b) Expl. 1'."""
    key = (element or "").split("#")[0]
    parts = key.split("_")
    if not parts or not parts[0]:
        return ""
    out = DOC_NAMES.get(parts[0], parts[0])
    for p in parts[1:]:
        for rx, fmt in _PART:
            m = re.fullmatch(rx, p)
            if m:
                w = fmt.format(m.group(1))
                out += w if w.startswith("(") else " " + w
                break
        else:
            out += " " + p
    return out


def module_of(node_id: str) -> str:
    """M_SIAFOS_A_SIAS -> M_SIAFOS; IH_ANGNS_SIAFOS -> M_ANGNS."""
    if node_id.startswith("IH_"):
        return "M_" + node_id.split("_")[1]
    m = re.match(r"(M_[A-Z0-9]+)_(A|G|LE|PD|OR|CT|TC)_", node_id)
    return m.group(1) if m else ""


def _levels(raw: str) -> Dict[str, str]:
    """The fact level from the yaml comments the governance master writes (level 1 gate / level 2 / LAST)."""
    level, out = "", {}
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("# ----"):
            low = s.lower()
            level = "L1" if "level 1" in low else "L2" if "level 2" in low else "L3" if "level 3" in low \
                else "LAST" if "last" in low else "L?"
            continue
        m = re.match(r"\s{2}(F_[A-Z0-9_]+):", line)
        if m and level:
            out[m.group(1)] = level
    return out


def _report(path: Path) -> dict:
    """module -> MKP element; MKP element -> why it is in (MEMBER / REFERENCE ...)."""
    out = {"module": {}, "why": {}, "same": {}}
    if not path.is_file():
        return out
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        if "Laws" in wb.sheetnames:
            for r in list(wb["Laws"].iter_rows(values_only=True))[1:]:
                if r and r[0]:
                    out["why"][str(r[0])] = str(r[1] or "")
        if "Molecules" in wb.sheetnames:
            for r in list(wb["Molecules"].iter_rows(values_only=True))[1:]:
                if not r or not r[0] or not r[1]:
                    continue
                out["module"].setdefault(str(r[1]), str(r[0]))
                # a law's atom that the governance master writes under another law's id = a SAME atom
                if r[2] == "A" and r[3] and r[4] and r[3] != r[4]:
                    out["same"].setdefault(str(r[4]), set()).add(str(r[3]))
    except Exception:
        pass
    return out


def rulebooks(gm_dir: Path) -> List[dict]:
    books = []
    for f in sorted(gm_dir.glob("TG*/TSG*_rulebook.yaml")):
        raw = f.read_text(encoding="utf-8")
        d = yaml.safe_load(raw) or {}
        head = raw.splitlines()[0] if raw else ""
        m = re.match(r"#\s*GOVERNANCE MASTER\s+(TSG\d+_\d+)\s+(.*?)\s+\((TG\d+)\s+(.*)\)\s*$", head)
        tsg = m.group(1) if m else f.name.split("_rulebook")[0]
        status = re.search(r"status\s+(\w+)", raw.splitlines()[1] if len(raw.splitlines()) > 1 else "")
        books.append({"tsg": tsg, "name": m.group(2) if m else str(d.get("name", "")), "tg": m.group(3) if m else f.parent.name,
                      "tg_name": m.group(4) if m else "", "status": status.group(1) if status else "", "data": d,
                      "levels": _levels(raw), "report": _report(f.parent / "report" / f"{tsg}_report.xlsx"),
                      "file": str(f)})
    return books


def build(gm_dir: Path, origin: Optional[Dict[str, dict]] = None, documents: Optional[Dict[str, dict]] = None,
          refs: Optional[List[dict]] = None, catalog: Optional[Path] = None) -> dict:
    """The whole graph for the page: nodes, edges (per TSG), modules, TSGs, fact origins, documents."""
    origin, documents, refs = origin or {}, documents or {}, refs or []
    nodes: Dict[str, dict] = {}
    edges: Dict[tuple, set] = {}
    modules: Dict[str, dict] = {}
    tsgs = []

    cur = {"tsg": ""}

    def node(nid, typ, **kw):
        n = nodes.setdefault(nid, {"id": nid, "type": typ, "tsgs": [], "module": module_of(nid)})
        if cur["tsg"] and cur["tsg"] not in n["tsgs"]:
            n["tsgs"].append(cur["tsg"])
        for k, v in kw.items():
            if v not in (None, "", [], {}):
                n[k] = v
        return n

    def edge(a, b, kind, tsg):
        edges.setdefault((a, b, kind), set()).add(tsg)

    cat = {}
    if catalog and catalog.is_file():
        for f in (yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}).get("facts", []):
            cat[f["fact_id"]] = f
    for b in rulebooks(gm_dir):
        d, t = b["data"], b["tsg"]
        cur["tsg"] = t
        rep = b["report"]
        laws = [{"mkp": k, "why": v, "module": next((m for m, e in rep["module"].items() if e == k), "")}
                for k, v in rep["why"].items()]
        tsgs.append({"id": t, "name": b["name"], "tg": b["tg"], "tg_name": b["tg_name"], "status": b["status"],
                     "laws": laws, "time": (d.get("time") or {}).get("law_date", "")})
        for mod, el in rep["module"].items():
            mm = modules.setdefault(mod, {"id": mod, "mkp": el, "cite": citation(el), "doc": doc_of(el), "tsgs": [],
                                          "why": {}})
            mm["why"][t] = rep["why"].get(el, "")
            if t not in mm["tsgs"]:
                mm["tsgs"].append(t)
        for fid, f in (d.get("facts") or {}).items():
            c = cat.get(fid, {})
            n = node(fid, "F", label=f.get("name", ""), dtype=f.get("type", ""), options=f.get("options"),
                     entity=c.get("entity"), level=b["levels"].get(fid, "L?"), tier=f.get("tier"))
            n.setdefault("level_by_tsg", {})[t] = b["levels"].get(fid, "L?")
        for aid, p in (d.get("predicates") or {}).items():
            node(aid, "A", label=p.get("statement", ""), order=p.get("order"), handler=p.get("handler"),
                 rule=p.get("rule"), interpretation=p.get("interpretation"), valid_from=p.get("valid_from"),
                 valid_to=p.get("valid_to"), if_false=p.get("if_false"))
            facts = set(FACT.findall(str(p.get("rule") or ""))) | set(p.get("grounding") or [])
            for fid in sorted(facts):
                node(fid, "F")
                edge(fid, aid, "fa", t)
            same = rep["same"].get(aid)
            if same:
                nodes[aid].setdefault("same_as", sorted(set(nodes[aid].get("same_as", [])) | same))
        for gid, g in (d.get("groups") or {}).items():
            members = g.get("members") or []
            is_ref = str(g.get("name", "")).startswith("Any of the referred steps")
            n = node(gid, "R" if is_ref else "G", label=g.get("name", ""), logic=g.get("logic"),
                     description=g.get("description"), members=members)
            n["type"] = "R" if is_ref else "G"           # the same id may have been met as an atom in an expr before
            for mem in members:
                if "_PD_" in mem:                                   # a referenced law's decision answers this check
                    kind = "self" if doc_of(modules.get(module_of(mem), {}).get("mkp", "")) == \
                        doc_of(modules.get(module_of(gid), {}).get("mkp", "")) else "cross"
                    node(mem, "PD")
                    edge(mem, gid, kind, t)
                    n.setdefault("ref_kind", kind)
                else:
                    node(mem, "G" if "_G_" in mem else "A")
                    edge(mem, gid, "ag", t)
        for key, typ in (("outcomes", "LE"), ("overrides", "OV")):
            for lid, o in (d.get(key) or {}).items():
                node(lid, typ, label=o.get("outcome", ""), expr=o.get("expr"), description=o.get("description"))
                for x in sorted(set(ID.findall(str(o.get("expr") or "")))):
                    if x.startswith("F_"):
                        continue
                    node(x, "G" if "_G_" in x else "A")
                    edge(x, lid, "al", t)
        for pid, p in (d.get("products") or {}).items():
            node(pid, "PD", label=p.get("statement", ""))
        for c in d.get("compounds") or []:
            for pr in c.get("produces") or []:
                node(pr["product"], "PD", question=c.get("question"), compound=c.get("id"))
                for src in pr.get("from") or []:
                    edge(src, pr["product"], "lp", t)
            for ov in c.get("overrides") or []:
                if ov in nodes:
                    nodes[ov]["overrides_in"] = c.get("question")
    # SAME atom: an atom that another law's group / law expression uses too (written once, shared)
    for (a, b, k), _ts in edges.items():
        if k in ("ag", "al") and nodes.get(a, {}).get("type") == "A":
            other = module_of(b)
            if other and other != module_of(a):
                nodes[a].setdefault("shared_with", [])
                if other not in nodes[a]["shared_with"]:
                    nodes[a]["shared_with"].append(other)
    # facts: where the value comes from
    for fid, n in nodes.items():
        if n["type"] == "F" and fid in origin:
            n["origin"] = {k: v for k, v in origin[fid].items() if k not in ("fact_id",) and v not in (None, "")}
    # references recorded by MKP (mkp_ref): shown on the law, with their status
    by_mkp = {m["mkp"]: mid for mid, m in modules.items()}
    for r in refs:
        mid = by_mkp.get(r.get("mkp_element"))
        if mid:
            modules[mid].setdefault("refs", []).append({k: r.get(k) for k in
                                                        ("scope", "ref_id", "target_key", "source_key", "target_status")})
    for mid, m in modules.items():
        m["refs"] = sorted(m.get("refs", []), key=lambda r: (r["scope"] or "", r["ref_id"] or ""))
    return {"tsgs": sorted(tsgs, key=lambda x: x["id"]), "nodes": nodes, "modules": modules, "documents": documents,
            "edges": [[a, b, k, sorted(ts)] for (a, b, k), ts in sorted(edges.items())]}


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")).replace("</", "<\\/")
