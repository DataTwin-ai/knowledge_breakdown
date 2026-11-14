"""RELATIONSHIP / page: the Fact Network page, drawn only from the latest kp_relation rows (one HTML file).

The page reads nothing else: the law name of an MKP is its citation, taken from its own PART_OF / USED_IN rows.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

TEMPLATE = Path(__file__).resolve().parent / "fact_network.html"


def short(ident: str) -> str:
    return ident.split("::", 1)[1] if "::" in ident else ident


def write(rows: List[dict], path: Path) -> Path:
    cite: Dict[str, str] = {}
    for r in rows:
        if r["to_type"] == "MKP" and r["to_name"]:
            cite.setdefault(r["to_id"], r["to_name"])
        elif r["from_type"] == "MKP" and r["from_name"]:
            cite.setdefault(r["from_id"], r["from_name"])
    # a TSG and its governance master share one id (TSG01_01): the page keeps them apart, the SQL shows the real id
    key = lambda t, i: f"GM:{i}" if t == "GOVERNANCE_MASTER" else i
    data = [{"molecule": r["mkp"], "citation": cite.get(r["mkp"], r["mkp"]),
             "from_type": r["from_type"], "from_id": key(r["from_type"], r["from_id"]), "from_raw": r["from_id"],
             "from_element": short(r["from_id"]),
             "from_label": r["from_name"] or "", "relation": r["relation"],
             "to_type": r["to_type"], "to_id": key(r["to_type"], r["to_id"]), "to_raw": r["to_id"],
             "to_element": short(r["to_id"]), "to_label": r["to_name"] or "",
             "link": r["link"], "via": r["via"] or "", "hops": (r["via"].count(" > ") + 1) if r["via"] else ""}
            for r in rows]
    text = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", text) \
        .replace("__BUILT__", datetime.now().strftime("%d-%m-%Y %H:%M"))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
