"""SAME atom (bug list A, 2026-11-18) - no database, no model.

Real case: 291 SAME checks in the runs of 16-17 Nov mapped 0 atoms. The SAME prompt told the model to answer with a
long `canonical` (instrument::version::source_mkp::atom) the script never sent, and Python only accepted the bare
akp_id, so every SAME answer was dropped without a log line. Now:
  - the answer may name the candidate by its akp_id or by the old long form;
  - an exact copy (same statement, facts and rule) is SAME by Python, without the model;
  - a SAME atom is saved as A_REF with the SAVED atom's id, and this law's groups / LE use that id;
  - every SAME answer that is not applied is logged with its reason.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

pytest.importorskip("psycopg2")

from akp_engine import _apply_same, _kept_id, _python_same  # noqa: E402
from akp_ss.ids import abbreviate, check  # noqa: E402

M = "M_EORTL"
KEPT = "M_EOAPFT_A_PIECO"
STMT = "The person is an electronic commerce operator"


def rows():
    return [
        {"MKP": M, "TYPE": "MKP", "ELEMENT": M, "CONCEPT": "NA", "RESULTS": 'citation: "Section 9(5) proviso 1"'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_PERSON_ECO_STATUS", "CONCEPT": "NA",
         "RESULTS": 'name: "Person Electronic Commerce Operator Status"; entity: PERSON; type: BOOL'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_OPERATOR", "CONCEPT": "Operator",
         "RESULTS": f'name: "Person Is Electronic Commerce Operator"; statement: "{STMT}"; '
                    'facts: [F_PERSON_ECO_STATUS]; rule: "F_PERSON_ECO_STATUS = TRUE"'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_NO_PRESENCE", "CONCEPT": "Presence",
         "RESULTS": 'name: "Operator Lacks Physical Presence"; statement: "No physical presence"; '
                    'facts: [F_PERSON_ECO_STATUS]; rule: "F_PERSON_ECO_STATUS = FALSE"'},
        {"MKP": M, "TYPE": "G", "ELEMENT": "G_ECO", "CONCEPT": "NA",
         "RESULTS": "logic: ALL; members: [A_OPERATOR, A_NO_PRESENCE]"},
        {"MKP": M, "TYPE": "LE", "ELEMENT": "LE_REP", "CONCEPT": "NA",
         "RESULTS": 'outcome: REPRESENTATIVE_LIABLE; expr: "G_ECO AND A_OPERATOR"'},
    ]


def candidates(results=None):
    saved = results or (f'statement: "{STMT}"; facts: [F_PECOS]; rule: "F_PECOS = TRUE"')
    return [{"element": "A_OPERATOR", "candidates": [
        {"akp_id": KEPT, "element": KEPT, "results": saved, "similarity": 0.99, "concept_similarity": 0.95},
        {"akp_id": "M_DONS_A_TIAS", "element": "M_DONS_A_TIAS", "results": 'statement: "Other"', "similarity": 0.91,
         "concept_similarity": 0.5}]}]


def test_kept_id_accepts_short_and_old_long_form():
    ids = {KEPT, "M_DONS_A_TIAS"}
    assert _kept_id(KEPT, ids) == KEPT
    assert _kept_id(f"CGSTA17::V_1::CGSTA17_S9_SS5_P2#TEXT::{KEPT}", ids) == KEPT     # the old prompt's form
    assert _kept_id("M_NOT_SENT_A_X", ids) == ""


def test_long_canonical_answer_is_applied_now():
    t, notes = rows(), []
    m = [{"element": "A_OPERATOR", "canonical": f"CGSTA17::V_1::CGSTA17_S9_SS5_P2#TEXT::{KEPT}",
          "same_verdict": "TRUE", "reason": "same condition, same subject, same input"}]
    assert _apply_same(t, m, candidates(), note=notes.append) == 1
    row = next(r for r in t if r["ELEMENT"] == "A_OPERATOR")
    assert row["TYPE"] == "A_REF" and row["SAME_AKP_ID"] == KEPT
    assert STMT in row["RESULTS"]                         # this law's wording kept as a record
    assert any(n.startswith("SAME ") and KEPT in n for n in notes)


def test_new_law_uses_the_saved_id_in_its_groups_and_le():
    t = rows()
    _apply_same(t, [{"element": "A_OPERATOR", "canonical": KEPT, "same_verdict": "TRUE", "reason": "same"}],
                candidates())
    mapping, _ = abbreviate(t, M, {})
    assert mapping["A_OPERATOR"] == KEPT                  # M_EORTL_A_... is never made
    by = {r["ELEMENT"]: r for r in t}
    assert by[KEPT]["TYPE"] == "A_REF"
    g = next(r for r in t if r["TYPE"] == "G")
    le = next(r for r in t if r["TYPE"] == "LE")
    assert KEPT in g["RESULTS"] and KEPT in le["RESULTS"]
    assert check(t, M) == []                               # an A_REF may carry another law's id


def test_dropped_answers_are_logged_with_their_reason():
    t, notes = rows(), []
    bad = [{"element": "A_OPERATOR", "canonical": "M_NOT_SENT_A_X", "same_verdict": "TRUE", "reason": "x"},
           {"element": "A_OPERATOR", "canonical": KEPT, "same_verdict": "TRUE", "reason": ""}]
    assert _apply_same(t, bad, candidates(), note=notes.append) == 0
    assert any("is not one of the candidates" in n for n in notes)
    assert any("no reason given" in n for n in notes)


def test_not_same_answer_changes_nothing():
    t = rows()
    assert _apply_same(t, [{"element": "A_OPERATOR", "canonical": KEPT, "same_verdict": "FALSE",
                            "reason": "different subject"}], candidates()) == 0
    assert all(r["TYPE"] != "A_REF" for r in t)


def test_exact_copy_is_same_by_python_without_the_model():
    t = rows()
    exact = f'statement: "{STMT}."; facts: [F_PERSON_ECO_STATUS]; rule: "F_PERSON_ECO_STATUS = TRUE"'
    got = _python_same(t, candidates(exact))
    assert got and got[0]["canonical"] == KEPT and got[0]["found_by"] == "python"
    assert _python_same(t, candidates()) == []             # other fact ids: the model decides


def test_two_atoms_of_one_law_never_take_the_same_saved_id():
    t, notes = rows(), []
    c = candidates() + [{"element": "A_NO_PRESENCE", "candidates": candidates()[0]["candidates"]}]
    m = [{"element": "A_OPERATOR", "canonical": KEPT, "same_verdict": "TRUE", "reason": "same"},
         {"element": "A_NO_PRESENCE", "canonical": KEPT, "same_verdict": "TRUE", "reason": "same"}]
    assert _apply_same(t, m, c, note=notes.append) == 1
    assert any("already used by another atom" in n for n in notes)
