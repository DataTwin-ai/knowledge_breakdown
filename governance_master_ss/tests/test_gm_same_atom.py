"""Governance master: a SAME atom is written ONCE (bug list A6, 2026-11-18) - no database.

9(5) proviso 2 (M_EOAPFT) saved "The person is an electronic commerce operator" as M_EOAPFT_A_PIECO. 9(5) proviso 1
(M_EORTL) has the same atom: it is saved as an A_REF row with that same id, and its LE uses that id. The rulebook
must hold ONE predicate M_EOAPFT_A_PIECO, used by both laws' outcomes - not a copy per law.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_gm_build import _no_spine_cfg  # noqa: E402

from governance_master_ss.build import build  # noqa: E402
from governance_master_ss.source import Snapshot  # noqa: E402

P2, P1 = "CGSTA17_S9_SS5_P2#TEXT", "CGSTA17_S9_SS5_P1#TEXT"
KEPT = "M_EOAPFT_A_PIECO"
ATOM = ("statement: \"The person is an electronic commerce operator\"; order: LOW; facts: [F_PECOS]; "
        "rule: \"F_PECOS = TRUE\"")


def _row(key, mkp_id, i, kind, el, results, same=None):
    return {"instrument": "CGSTA17", "version_id": "V_TEST", "source_mkp": key, "mkp_id": mkp_id, "ordinal": i,
            "type": kind, "element": el, "concept": "NA", "results": results,
            "akp_id": el, "same_akp_id": same}


def snapshot():
    akp = [
        _row(P2, "M_EOAPFT", 1, "MKP", "M_EOAPFT", 'citation: "Section 9(5) proviso 2"; citation_key: CGSTA17_S9_SS5_P2; '
                                                   'part: TEXT; knowledge_type: CONDITION'),
        _row(P2, "M_EOAPFT", 2, "A", KEPT, ATOM),
        _row(P2, "M_EOAPFT", 3, "A", "M_EOAPFT_A_OHNPP", 'statement: "No physical presence"; order: LOW; '
                                                        'facts: [F_OPP]; rule: "F_OPP = FALSE"'),
        _row(P2, "M_EOAPFT", 4, "LE", "M_EOAPFT_LE_APL", f'outcome: APPOINTED_PERSON_LIABLE; '
                                                       f'expr: "{KEPT} AND M_EOAPFT_A_OHNPP"; nl: "x"'),
        _row(P1, "M_EORTL", 1, "MKP", "M_EORTL", 'citation: "Section 9(5) proviso 1"; citation_key: CGSTA17_S9_SS5_P1; '
                                                 'part: TEXT; knowledge_type: CONDITION'),
        _row(P1, "M_EORTL", 2, "A_REF", KEPT, ATOM, same=KEPT),           # SAME atom: the saved id
        _row(P1, "M_EORTL", 3, "A", "M_EORTL_A_HR", 'statement: "Has a representative"; order: LOW; '
                                                   'facts: [F_RR]; rule: "F_RR = TRUE"'),
        _row(P1, "M_EORTL", 4, "LE", "M_EORTL_LE_RL", f'outcome: REPRESENTATIVE_LIABLE; '
                                                     f'expr: "{KEPT} AND M_EORTL_A_HR"; nl: "y"'),
    ]
    facts = {f: {"name": f, "entity": "PERSON", "data_type": "BOOL"} for f in ("F_PECOS", "F_OPP", "F_RR")}
    gkp = [{"source_mkp": k, "akp_element": el, "akp_type": t, "tg_id": "TG07", "tsg_id": "TSG07_03",
            "status": "APPROVED"}
           for k, el, t in ((P2, KEPT, "A"), (P2, "M_EOAPFT_A_OHNPP", "A"), (P1, KEPT, "A_REF"),
                            (P1, "M_EORTL_A_HR", "A"))]
    return Snapshot(akp=akp, facts=facts, gkp=gkp, time_facts=[], questions={}, definitions=[])


def test_same_atom_is_one_predicate_used_by_both_laws():
    rb = build(snapshot(), "TSG07_03", "E-commerce operator liability", "TG07", "PERSON", _no_spine_cfg(), {},
               "v", depth=1).rulebook
    preds = [p for p in rb["predicates"] if p.startswith("M_EOAPFT_A_PIECO")]
    assert preds == [KEPT]                                   # once, under the saved id - no M_EOAPFT_A_PIECO_<tag>
    exprs = " ".join(str(o.get("expr")) for o in rb["outcomes"].values())
    assert exprs.count(KEPT) >= 2                             # both laws' outcomes use it
