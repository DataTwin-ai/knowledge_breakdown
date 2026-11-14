"""Fact catalog / router: which laws can be checked with the facts a document gives (the fact flow).

route(available, catalog) -> per TSG:
   READY    every fact its laws use is available (or can be derived from available facts)
   PARTIAL  some laws can run; the missing facts are listed (ask them, or look them up)
   NOT_RUN  none of its facts are on the document
Inside a READY / PARTIAL TSG the engine still walks the TSG's governance master step by step (the law has an
order); the router only decides WHICH governance masters run and says what is missing.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

from .catalog import tsg_needs


def expand(available: Iterable[str], catalog: List[dict]) -> set:
    """Available facts + every DERIVED fact whose inputs are all available (repeated until nothing new)."""
    have = set(available)
    derived = [(f["fact_id"], set(f["derived_from"])) for f in catalog if f["kind"] == "DERIVED" and f["derived_from"]]
    changed = True
    while changed:
        changed = False
        for fid, inputs in derived:
            if fid not in have and inputs <= have:
                have.add(fid)
                changed = True
    return have


def route(available: Iterable[str], catalog: List[dict], names: Dict[str, str] = None) -> List[dict]:
    have = expand(available, catalog)
    kinds = {f["fact_id"]: f["kind"] for f in catalog}
    out = []
    for tsg, needs in sorted(tsg_needs(catalog).items()):
        got = needs & have
        missing = sorted(needs - have)
        status = "READY" if not missing else "PARTIAL" if got else "NOT_RUN"
        out.append({"tsg": tsg, "name": (names or {}).get(tsg, ""), "status": status, "facts": len(needs),
                    "available": sorted(got), "missing": missing,
                    "missing_by_kind": {k: [m for m in missing if kinds.get(m) == k]
                                        for k in ("DOCUMENT", "LOOKUP", "ASK", "DERIVED")
                                        if any(kinds.get(m) == k for m in missing)}})
    order = {"READY": 0, "PARTIAL": 1, "NOT_RUN": 2}
    return sorted(out, key=lambda r: (order[r["status"]], r["tsg"]))
