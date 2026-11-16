"""GKP / Python score - every atom's group is checked, the LLM only when Python is not sure (notes section 1).

For each TSG the AKP gave an atom, Python looks at free clues:
    1. embeddings  the atom and concept embeddings against every TSG embedding (gkp_taxonomy): the similarity
                   with this TSG, and whether this TSG is among the confirm_top_n closest TSGs
    2. MKP agrees  the MKP step gave the same TSG to the law (mkp.group_names)
    3. anchor      the atom's law is a key law (anchor) of this TSG
    4. facts       the atom's facts are already used by atoms of this TSG
HIGH = similarity >= confirm_min_similarity AND among the closest AND at least one of 2 / 3 / 4
     -> APPROVED by Python (AKP_GROUP_CONFIRMED), no LLM.
Anything else -> the GKP verifiers decide (AKP_GROUP_CONFIRMED_LLM when they keep the TSG, AKP_GROUP_CHANGED when
they choose another). A small share of HIGH atoms (audit_share, chosen by the atom id, so a re-run picks the same
ones) also goes to the verifiers, to see that the range still holds; with `calibrate` every atom goes to them.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Tuple

CONFIRMED = "AKP_GROUP_CONFIRMED"
CONFIRMED_LLM = "AKP_GROUP_CONFIRMED_LLM"
CHANGED = "AKP_GROUP_CHANGED"


def clues(tsg: str, sims: Dict[str, float], top: List[str], mkp_groups: List[str], sources: List[str]) -> dict:
    return {"similarity": round(float(sims.get(tsg) or 0.0), 4), "closest": tsg in top,
            "mkp_agrees": tsg in (mkp_groups or []),
            "anchor": any(s.startswith("anchor") for s in sources or []),
            "facts": any(s.startswith("fact ") for s in sources or [])}


def value(c: dict) -> float:
    """One number for the report and the calibration: the similarity plus 0.05 per supporting clue."""
    return round(c["similarity"] + 0.05 * sum(bool(c[k]) for k in ("closest", "mkp_agrees", "anchor", "facts")), 4)


def is_high(c: dict, min_similarity: float) -> bool:
    return c["similarity"] >= min_similarity and c["closest"] and (c["mkp_agrees"] or c["anchor"] or c["facts"])


def audited(akp_id: str, share: float) -> bool:
    """The same atoms every run (by id), about `share` of them."""
    if share <= 0:
        return False
    h = int(hashlib.sha256(str(akp_id).encode("utf-8")).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < share


def why(c: dict) -> str:
    parts = [f"similarity {c['similarity']:.2f}" + (" (among the closest)" if c["closest"] else " (not among the closest)")]
    parts += [k.replace("_", " ") for k in ("mkp_agrees", "anchor", "facts") if c[k]]
    return ", ".join(parts)


def calibration(rows: List[Tuple[float, bool]], buckets=(0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)) -> List[dict]:
    """(python score, the LLM kept the AKP's TSG) pairs -> agreement per score floor, for setting
    confirm_min_similarity: choose the lowest floor where the LLM agrees almost always (e.g. 98-99%)."""
    out = []
    for floor in buckets:
        part = [ok for sc, ok in rows if sc >= floor]
        out.append({"from_score": floor, "atoms": len(part),
                    "llm_agrees": round(sum(part) / len(part), 3) if part else None})
    return out
