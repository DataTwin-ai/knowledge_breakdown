"""2026-11-21 (no database): self reference wiring, found on the 7(2) -> Schedule III test run.

bug 1: "specified in Schedule III" is answered by Schedule III's ENTRIES only - not by its Explanations / definitions
bug 2: an atom inside Schedule III that names "Schedule III" keeps its own check (it is not ANY of its own document)
bug 3: an LK target written as words ("sub-section (1)") is matched through mkp_ref (SELF) -> the override is made
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_gm_build import _no_spine_cfg  # noqa: E402

from governance_master_ss.build import build, lk_target, bundles_of  # noqa: E402
from governance_master_ss.source import Snapshot  # noqa: E402

S72A, S71A = "CGSTA17_S7_SS2_Ca#TEXT", "CGSTA17_S7_SS1_Ca#TEXT"
EN1, EN2, EXP1 = "CGSTA17_SCHIII_EN1#TEXT", "CGSTA17_SCHIII_EN2#TEXT", "CGSTA17_SCHIII_EN8_Cb_E1#TEXT"


def _row(key, mkp_id, i, kind, el, results):
    return {"instrument": "CGSTA17", "version_id": "V_TEST", "source_mkp": key, "mkp_id": mkp_id, "ordinal": i,
            "type": kind, "element": el, "concept": "NA", "results": results, "akp_id": el, "same_akp_id": None}


def _law(key, m, citation, kt, atoms, le_expr):
    ck = key.split("#")[0]
    rows = [_row(key, m, 0, "MKP", m, f'citation: "{citation}"; citation_key: {ck}; part: TEXT; knowledge_type: {kt}')]
    for i, (aid, res) in enumerate(atoms, 1):
        rows.append(_row(key, m, i, "A", aid, res))
    rows.append(_row(key, m, 10, "PD", f"{m}_PD_R", 'nl: "result"'))
    rows.append(_row(key, m, 11, "LE", f"{m}_LE_R", f'outcome: R_{m}; produces: {m}_PD_R; expr: "{le_expr}"; nl: "x"'))
    return rows


def snapshot():
    akp = []
    akp += _law(S72A, "M_ANGNS", "Section 7(2)(a)", "EXCEPTION",
                [("M_ANGNS_A_ASISIII", 'statement: "Specified in Schedule III"; order: HIGH; handler: heavy; '
                                       'facts: [F_DOAOT]; reference: [Schedule III]; reference_keys: [CGSTA17_SCHIII]')],
                "M_ANGNS_A_ASISIII")
    akp.append(_row(S72A, "M_ANGNS", 12, "LK", "M_ANGNS_LK_OVERRIDES",
                    'relation: OVERRIDES; status: UNRESOLVED_RETAINED; target: "sub-section (1)"; from: M_ANGNS_LE_R'))
    akp += _law(S71A, "M_SIAFOS", "Section 7(1)(a)", "DEFINITION",
                [("M_SIAFOS_A_SALE", 'statement: "It is a sale"; order: LOW; facts: [F_FOS]; rule: "F_FOS = \'SALE\'"')],
                "M_SIAFOS_A_SALE")
    akp += _law(EN1, "M_ESTE", "Schedule III para 1", "EXCEPTION",
                [("M_ESTE_A_EMP", 'statement: "Employee to employer"; order: LOW; facts: [F_PRR]; '
                                  'rule: "F_PRR = \'EMPLOYEE_EMPLOYER\'"')], "M_ESTE_A_EMP")
    akp += _law(EN2, "M_SBCOT", "Schedule III para 2", "EXCEPTION",
                [("M_SBCOT_A_CRT", 'statement: "Service by a court"; order: LOW; facts: [F_PBT]; '
                                   'rule: "F_PBT = \'COURT\'"')], "M_SBCOT_A_CRT")
    akp += _law(EXP1, "M_DOCFP2", "Schedule III Explanation 1", "DEFINITION",
                [("M_DOCFP2_A_BIDC", 'statement: "The body is a District Court"; order: LOW; facts: [F_BD]; '
                                     'rule: "F_BD = \'DISTRICT_COURT\'"; reference: [SCHEDULE III]; '
                                     'reference_keys: [CGSTA17_SCHIII]')], "M_DOCFP2_A_BIDC")
    facts = {f: {"name": f, "entity": "SUPPLY", "data_type": "TEXT"} for f in ("F_DOAOT", "F_FOS", "F_PRR", "F_PBT", "F_BD")}
    gkp = [{"source_mkp": k, "akp_element": a, "akp_type": "A", "tg_id": "TG01", "tsg_id": "TSG01_03", "status": "APPROVED"}
           for k, a in ((S72A, "M_ANGNS_A_ASISIII"), (S71A, "M_SIAFOS_A_SALE"), (EN1, "M_ESTE_A_EMP"),
                        (EN2, "M_SBCOT_A_CRT"), (EXP1, "M_DOCFP2_A_BIDC"))]
    s = Snapshot(akp=akp, facts=facts, gkp=gkp, time_facts=[], questions={}, definitions=[])
    s.self_refs = [{"source_mkp": S72A, "mention": "Schedule III", "target_key": "CGSTA17_SCHIII"},
                   {"source_mkp": S72A, "mention": "sub-section (1)", "target_key": "CGSTA17_S7_SS1"}]
    return s


def _rb():
    return build(snapshot(), "TSG01_03", "Neither supply", "TG01", "SUPPLY", _no_spine_cfg(), {}, "v", depth=1).rulebook


def test_bug1_schedule_reference_takes_entries_not_explanations():
    rb = _rb()
    members = rb["groups"]["M_ANGNS_A_ASISIII"]["members"]
    assert "M_ESTE_PD_R" in members and "M_SBCOT_PD_R" in members
    assert "M_DOCFP2_PD_R" not in members                    # Explanation 1 ("court" includes ...) is not an entry


def test_bug2_atom_inside_schedule_keeps_its_own_check():
    rb = _rb()
    assert rb["predicates"]["M_DOCFP2_A_BIDC"]["rule"] == "F_BD = 'DISTRICT_COURT'"
    assert "M_DOCFP2_A_BIDC" not in rb["groups"]


def test_bug3_words_target_found_through_mkp_ref():
    s = snapshot()
    b = bundles_of(s)[S72A]
    lk = next(x for x in b.of("LK"))
    assert lk_target(s, b, lk) == "CGSTA17_S7_SS1"
    rb = _rb()
    assert any(o.startswith("IH_") for o in rb.get("overrides", {}))       # 7(2)(a) now overrides 7(1)


def test_words_with_no_saved_key_stay_as_they_are():
    s = snapshot()
    s.self_refs = []
    b = bundles_of(s)[S72A]
    assert lk_target(s, b, next(x for x in b.of("LK"))) == "sub-section (1)"
