"""2026-11-19 (no database, no model): one atom = one rule = one set of facts.

Real case: 39(1) reused 37(1)'s atom M_FOSD_A_PIISD ("The person is an Input Service Distributor") as an A_REF, but
kept its OWN fact F_PSTC (person's tax category), so one atom id pointed to F_PIISD in 37(1) and F_PSTC in 39(1).
Now an A_REF tests the saved atom's facts and rule, and a fact only that A_REF used is not registered.
Also: yes/no facts that are values of one attribute ("is ISD", "is non-resident") go back to the judge (FACT SHAPE).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

pytest.importorskip("psycopg2")

from akp_engine import _apply_same  # noqa: E402
from akp_ss.guard import flag_facts  # noqa: E402
from transport import get_field  # noqa: E402

KEPT = "M_FOSD_A_PIISD"
SAVED = ('statement: "The person is an Input Service Distributor"; facts: [F_PIISD]; rule: "F_PIISD = TRUE"')


def rows():
    return [
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_PSTC", "CONCEPT": "NA",
         "RESULTS": 'name: "Person Tax Category"; entity: PERSON; type: CODE; options: [ISD, NRTP]'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_RFF", "CONCEPT": "NA",
         "RESULTS": 'name: "Return Filing Frequency"; entity: RETURN; type: CODE'},
        {"MKP": "M_FORBRP", "TYPE": "A", "ELEMENT": "A_ISD", "CONCEPT": "ISD",
         "RESULTS": 'statement: "The person is an Input Service Distributor"; facts: [F_PSTC]; '
                    'rule: "F_PSTC = \'ISD\'"'},
        {"MKP": "M_FORBRP", "TYPE": "A", "ELEMENT": "A_FREQ", "CONCEPT": "Frequency",
         "RESULTS": 'statement: "A return is furnished for every month"; facts: [F_RFF]; rule: "F_RFF = \'MONTHLY\'"'},
        {"MKP": "M_FORBRP", "TYPE": "LE", "ELEMENT": "LE_MAIN", "CONCEPT": "NA",
         "RESULTS": 'outcome: RETURN_DUE; expr: "NOT A_ISD AND A_FREQ"'},
    ]


def same():
    cands = [{"element": "A_ISD", "candidates": [{"akp_id": KEPT, "results": SAVED, "similarity": 0.97}]}]
    match = [{"element": "A_ISD", "canonical": KEPT, "same_verdict": "TRUE", "reason": "same exception"}]
    return cands, match


def test_a_ref_tests_the_saved_atoms_facts_and_rule():
    r, (cands, match) = rows(), same()
    notes = []
    assert _apply_same(r, match, cands, note=notes.append) == 1
    ref = next(x for x in r if x["TYPE"] == "A_REF")
    assert ref["_facts"] == ["F_PIISD"]
    assert "facts: [F_PIISD]" in ref["RESULTS"] and get_field(ref, "rule") == "F_PIISD = TRUE"
    assert get_field(ref, "statement") == "The person is an Input Service Distributor"
    assert any("SAME FACTS" in n and "F_PIISD" in n for n in notes)


def test_a_fact_only_the_a_ref_used_is_not_registered():
    r, (cands, match) = rows(), same()
    notes = []
    _apply_same(r, match, cands, note=notes.append)
    facts = [x["ELEMENT"] for x in r if x["TYPE"] == "FACTS"]
    assert facts == ["F_RFF"]                                  # F_PSTC dropped, F_RFF still used by A_FREQ
    assert any("FACT DROPPED" in n and "F_PSTC" in n for n in notes)


def _atom(fid):
    return {"RESULTS": f'statement: "x"; facts: [{fid}]; rule: "{fid} = TRUE"'}


def _fact(entity, typ="BOOL"):
    return {"RESULTS": f"name: \"x\"; entity: {entity}; type: {typ}"}


def test_yes_no_facts_for_values_of_one_attribute_go_back():
    atoms = {"A_ISD": _atom("F_PIISD"), "A_NRTP": _atom("F_PINRT"), "A_S10": _atom("F_PPSTT")}
    facts = {"F_PIISD": _fact("PERSON"), "F_PINRT": _fact("PERSON"), "F_PPSTT": _fact("PERSON")}
    les = {"LE_MAIN": {"RESULTS": 'expr: "A_REG AND NOT (A_ISD OR A_NRTP OR A_S10)"'}}
    errs = flag_facts(atoms, {}, les, facts)
    assert errs and "FACT SHAPE" in errs[0] and "F_PIISD" in errs[0] and "F_PPSTT" in errs[0]
    groups = {"G_EXC": {"RESULTS": "logic: ANY; members: [A_ISD, A_NRTP]"}}
    assert flag_facts(atoms, groups, {}, facts)


def test_real_yes_no_inputs_and_and_groups_are_fine():
    atoms = {"A_ITC": _atom("F_ITC_AVAILED"), "A_REG": _atom("F_PERSON_REGISTERED")}
    facts = {"F_ITC_AVAILED": _fact("CREDIT"), "F_PERSON_REGISTERED": _fact("PERSON")}
    assert flag_facts(atoms, {}, {"LE": {"RESULTS": 'expr: "A_ITC AND A_REG"'}}, facts) == []
    one = {"A_CAT": {"RESULTS": 'statement: "x"; facts: [F_CAT]; rule: "F_CAT = \'ISD\'"'},
           "A_CAT2": {"RESULTS": 'statement: "x"; facts: [F_CAT]; rule: "F_CAT = \'NRTP\'"'}}
    assert flag_facts(one, {}, {"LE": {"RESULTS": 'expr: "A_CAT OR A_CAT2"'}},
                      {"F_CAT": _fact("PERSON", "CODE")}) == []
