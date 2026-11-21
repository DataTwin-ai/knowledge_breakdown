"""Reference design 2026-11-19 (no database): law a (7(2)(b)) needs the exact part of an outside document
(Notification 14/2017, mkp_ref.source_key). The part has NO GKP row of its own (no TSG copy any more): the governance
master must still write it in full in law a's yaml (REFERENCE) and wire the exact atom that names it."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_gm_build import _no_spine_cfg  # noqa: E402

from governance_master_ss.build import build  # noqa: E402
from governance_master_ss.source import Snapshot  # noqa: E402

LAW, PART = "CGSTA17_S7_SS2_Cb#TEXT", "N14_2017_P1#TEXT"


def _row(inst, key, mkp_id, i, kind, el, results):
    return {"instrument": inst, "version_id": "V_TEST", "source_mkp": key, "mkp_id": mkp_id, "ordinal": i,
            "type": kind, "element": el, "concept": "NA", "results": results, "akp_id": el, "same_akp_id": None}


def snapshot():
    akp = [
        _row("CGSTA17", LAW, "M_GANS", 1, "MKP", "M_GANS", 'citation: "Section 7(2)(b)"; citation_key: CGSTA17_S7_SS2_Cb; '
                                                           'part: TEXT; knowledge_type: DEEMING'),
        _row("CGSTA17", LAW, "M_GANS", 2, "A", "M_GANS_A_GOVT", 'statement: "Done by a government"; order: LOW; '
                                                                'facts: [F_GOVT]; rule: "F_GOVT = TRUE"'),
        _row("CGSTA17", LAW, "M_GANS", 3, "A", "M_GANS_A_NOTIF", 'statement: "The activity is notified"; order: LOW; '
                                                                 'facts: [F_NOTIF]; rule: "F_NOTIF = TRUE"; '
                                                                 'outside_refs: [N_CTR_14_2017]'),
        _row("CGSTA17", LAW, "M_GANS", 4, "LE", "M_GANS_LE_NS", 'outcome: NOT_A_SUPPLY; '
                                                                'expr: "M_GANS_A_GOVT AND M_GANS_A_NOTIF"; nl: "x"'),
        _row("N14_2017", PART, "M_PFNS", 1, "MKP", "M_PFNS", 'citation: "Notification 14/2017 para 1"; '
                                                            'citation_key: N14_2017_P1; part: TEXT; knowledge_type: DEEMING'),
        _row("N14_2017", PART, "M_PFNS", 2, "A", "M_PFNS_A_PUB", 'statement: "Panchayat or Municipality function"; '
                                                                'order: LOW; facts: [F_PUBLIC_FUNCTION]; '
                                                                'rule: "F_PUBLIC_FUNCTION = TRUE"'),
        _row("N14_2017", PART, "M_PFNS", 3, "PD", "M_PFNS_PD_NOTIFIED", 'nl: "the activity is a notified one"'),
        _row("N14_2017", PART, "M_PFNS", 4, "LE", "M_PFNS_LE_N", 'outcome: NOTIFIED_ACTIVITY; produces: M_PFNS_PD_NOTIFIED; '
                                                                'expr: "M_PFNS_A_PUB"; nl: "y"'),
    ]
    facts = {f: {"name": f, "entity": "SUPPLY", "data_type": "BOOL"} for f in ("F_GOVT", "F_NOTIF", "F_PUBLIC_FUNCTION")}
    gkp = [{"source_mkp": LAW, "akp_element": el, "akp_type": "A", "tg_id": "TG01", "tsg_id": "TSG01_01",
            "status": "APPROVED"} for el in ("M_GANS_A_GOVT", "M_GANS_A_NOTIF")]          # the part has no GKP row
    s = Snapshot(akp=akp, facts=facts, gkp=gkp, time_facts=[], questions={}, definitions=[])
    s.references = [{"source_mkp": LAW, "ref_id": "N_CTR_14_2017", "mention": "Notification No. 14/2017",
                     "relation": "REFERS", "source_key": "N14_2017_P1"}]
    return s


def test_outside_part_comes_in_full_by_reference():
    res = build(snapshot(), "TSG01_01", "Supply", "TG01", "SUPPLY", _no_spine_cfg(), {}, "v", depth=1)
    laws = {x["mkp"]: x["reason"] for x in res.report.get("included", [])}
    assert PART in laws and laws[PART].startswith("REFERENCE N_CTR_14_2017")
    assert any(p.startswith("M_PFNS_A_PUB") for p in res.rulebook["predicates"])   # the part's own check, in full


def test_only_the_marked_atom_is_wired_to_the_part():
    rb = build(snapshot(), "TSG01_01", "Supply", "TG01", "SUPPLY", _no_spine_cfg(), {}, "v", depth=1).rulebook
    expr = str(rb["outcomes"]["M_GANS_LE_NS"]["expr"])
    assert "M_GANS_A_GOVT" in expr and "M_GANS_A_NOTIF" not in expr     # the notified atom = the part's step
    assert any(t.startswith("M_PFNS_PD") for t in expr.replace("(", " ").replace(")", " ").split())
