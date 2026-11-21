"""SAME check of SAVED atoms (bug list A4 / A5) - no database, no model.

Real pairs from relationship_data_v2 (17 Nov): the voucher atom of 12(4)(a) / 12(4)(b) / 13(4) is word for word the
same (SAME by Python); "the context does not require a different meaning of 'business'" vs "... of 'person'" is
90% alike but a different defined term (the model says NOT SAME).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("psycopg2")

from akp_ss import same_review as SR  # noqa: E402

V = 'statement: "The supply is a supply of vouchers by a supplier"; facts: [F_VSS]; rule: "F_VSS = TRUE"'


def pair(keep, mp, kres, mres, sim):
    return {"keep": keep, "keep_mkp": keep.split("_A_")[0] + "#TEXT", "keep_mkp_id": keep.split("_A_")[0],
            "keep_results": kres, "keep_text": "law k", "map": mp, "map_mkp": mp.split("_A_")[0] + "#TEXT",
            "map_mkp_id": mp.split("_A_")[0], "map_results": mres, "map_text": "law m", "similarity": sim,
            "concept_similarity": 0.9}


def pairs():
    return [
        pair("M_GVTOSR_A_SIVBS", "M_TOSSVI_A_SIVBS", V, V, 1.0),
        pair("M_TOSSVI_A_SIVBS", "M_VSTOSRD_A_SIVBS", V, V.replace("vouchers", "vouchers "), 0.999),
        pair("M_BIIA_A_CDNROM", "M_DOP_A_CDNROM",
             'statement: "The context does not require a different meaning of business"; facts: [F_DCS]',
             'statement: "The context does not require a different meaning of person"; facts: [F_DCS]', 0.93),
    ]


def fake_model(prompt: str) -> str:
    req = json.loads(prompt.split("\nINPUT:\n", 1)[1])
    return json.dumps({"answers": [{"pair": p["pair"], "same": "FALSE",
                                    "reason": "Different defined term: business vs person"} for p in req["pairs"]]})


def test_exact_copies_by_python_the_rest_by_the_model():
    asked = []
    got = SR.decide(pairs(), lambda p: asked.append(p) or fake_model(p), "PROMPT", batch=10)
    assert [p["ai"] for p in got] == ["SAME", "SAME", "NOT SAME"]
    assert [p["found_by"] for p in got] == ["python", "python", "model"]
    assert len(asked) == 1 and "M_DOP_A_CDNROM" in asked[0]       # only the non-copy went to the model


def test_no_answer_from_the_model_is_never_same():
    got = SR.decide(pairs()[2:], lambda p: "not json", "PROMPT")
    assert got[0]["ai"] == "NO ANSWER"


def test_plan_maps_every_later_atom_to_the_first_saved_one():
    got = SR.plan(SR.decide(pairs(), fake_model, "PROMPT"))
    m = {p["map"]: p["keep"] for p in got}
    assert m == {"M_TOSSVI_A_SIVBS": "M_GVTOSR_A_SIVBS",
                 "M_VSTOSRD_A_SIVBS": "M_GVTOSR_A_SIVBS"}          # chain A ~ B ~ C -> all to A; NOT SAME left out
    assert "M_GVTOSR_A_SIVBS" not in m                             # the kept atom is never mapped away


def test_excel_for_a_person_round_trip(tmp_path):
    got = SR.decide(pairs(), fake_model, "PROMPT")
    f = SR.write_excel(tmp_path / "same_review.xlsx", got)
    from openpyxl import load_workbook
    wb = load_workbook(f)
    ws = wb.active
    assert [c.value for c in ws[1]] == SR.COLUMNS
    ws.cell(row=2, column=len(SR.COLUMNS)).value = "yes"           # the person approves row 1 only
    wb.save(f)
    assert SR.read_approved(f) == [{"keep": "M_GVTOSR_A_SIVBS", "map": "M_TOSSVI_A_SIVBS",
                                    "reason": "approved by a person", "found_by": "person"}]
