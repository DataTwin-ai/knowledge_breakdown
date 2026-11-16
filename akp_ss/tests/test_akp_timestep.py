"""AKP time step: old / new law with valid_from / valid_to (no database, no model)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from akp_ss import timestep  # noqa: E402
from akp_ss.guard import engine_ready  # noqa: E402

LAW = {"fact": "F_DOI", "name": "Date Of Invoice"}
M = "M_CLRM"


def table():
    return [
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_OPTED", "CONCEPT": "NA", "RESULTS": 'name: "Opted"; type: BOOL'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_TURNOVER", "CONCEPT": "NA", "RESULTS": 'name: "Turnover"; type: AMOUNT'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_OPT", "CONCEPT": "",
         "RESULTS": 'name: "Person Opts For Composition"; statement: "The person opts to pay tax under sub-section (1) of section 9"; '
                    'order: LOW; handler: python; facts: [F_OPTED]; rule: "F_OPTED = TRUE"'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_TO", "CONCEPT": "",
         "RESULTS": 'statement: "Turnover is not above the limit"; order: LOW; handler: python; facts: [F_TURNOVER]; '
                    'rule: "F_TURNOVER <= 15000000"'},
        {"MKP": M, "TYPE": "LE", "ELEMENT": "LE_CLRM", "CONCEPT": "", "RESULTS": 'outcome: COMPOSITION; expr: "A_OPT AND A_TO"'},
    ]


CHANGES = [{"target": "CGSTA17_S10_SS1", "time_fact_id": "T_AMD1", "kind": "SUBSTITUTED", "effective_from": "2019-02-01",
            "replaced_text": "in lieu of the tax payable by him", "note": "Substituted ..."},
           {"target": "CGSTA17_S10", "time_fact_id": "T_AMD2", "kind": "INSERTED", "effective_from": "2019-02-01",
            "replaced_text": None, "note": "Inserted ..."},
           {"target": "CGSTA17_S10_SS1", "time_fact_id": "T_OLD", "kind": "INSERTED", "effective_from": "2017-07-01",
            "replaced_text": None, "note": "before GST"},
           {"target": "CGSTA17_S11", "time_fact_id": "T_OTHER", "kind": "INSERTED", "effective_from": "2019-02-01",
            "replaced_text": None, "note": "other provision"}]


def test_changes_for_this_provision_after_gst_start():
    ch = timestep.changes_for("CGSTA17_S10_SS1", CHANGES, "2017-07-01")
    assert [c["id"] for c in ch] == ["T_AMD1", "T_AMD2"]      # parent change counts; pre-GST and other law do not


def test_substitution_and_inserted_condition():
    rows = table()
    changes = timestep.changes_for("CGSTA17_S10_SS1", CHANGES, "2017-07-01")
    answer = {"changes": [
        {"id": "T_AMD1", "kind": "SUBSTITUTED", "new_atoms": ["A_OPT"],
         "old_atoms": [{"replaces": "A_OPT", "name": "Person Opts In Lieu Of Tax", "statement": "The person opts to pay, "
                        "in lieu of the tax payable by him, an amount", "order": "LOW", "handler": "python",
                        "facts": ["F_OPTED"], "rule": "F_OPTED = TRUE"}]},
        {"id": "T_AMD2", "kind": "INSERTED", "new_atoms": ["A_TO"], "role": "CONDITION"}]}
    out, notes = timestep.run(rows, changes, lambda: json.dumps(answer), LAW, lambda t: engine_ready(t))
    by = {r["ELEMENT"]: r for r in out}
    assert "valid_from: 2019-02-01" in by["A_OPT"]["RESULTS"]
    assert "valid_to: 2019-01-31" in by["A_OPT_OLD"]["RESULTS"]
    assert "members: [A_OPT, A_OPT_OLD]" in by["G_T_OPT"]["RESULTS"]
    assert "valid_to: 2019-01-31" in by["A_TO_PRE"]["RESULTS"] and 'rule: "F_DOI IS_SET"' in by["A_TO_PRE"]["RESULTS"]
    assert 'expr: "G_T_OPT AND G_T_TO"' in by["LE_CLRM"]["RESULTS"]        # the groups take the atoms' place
    assert "F_DOI" in by and engine_ready(out) == []
    assert not any("NOT USED" in n for n in notes)


def test_omitted_condition_is_anded_in():
    rows = table()
    changes = [{"id": "T_AMD3", "kind": "OMITTED", "effective_from": "2020-01-01", "replaced_text": "not a manufacturer", "note": ""}]
    answer = {"changes": [{"id": "T_AMD3", "kind": "OMITTED", "old_atoms": [
        {"joins": "LE_CLRM", "role": "CONDITION", "name": "Not A Manufacturer", "statement": "The person is not a manufacturer",
         "order": "HIGH", "handler": "light", "facts": ["F_OPTED"], "interpretation": "Is the person a manufacturer?"}]}]}
    out, notes = timestep.run(rows, changes, lambda: json.dumps(answer), LAW, lambda t: engine_ready(t))
    by = {r["ELEMENT"]: r for r in out}
    assert "valid_to: 2019-12-31" in by["A_OMITTED"]["RESULTS"] and "valid_from: 2020-01-01" in by["A_OMITTED_POST"]["RESULTS"]
    assert 'expr: "(A_OPT AND A_TO) AND G_T_OMITTED"' in by["LE_CLRM"]["RESULTS"]


def test_engine_errors_keep_the_current_law():
    rows = table()
    changes = [{"id": "T_AMD1", "kind": "SUBSTITUTED", "effective_from": "2019-02-01", "replaced_text": "old", "note": ""}]
    answer = {"changes": [{"id": "T_AMD1", "kind": "SUBSTITUTED", "new_atoms": ["A_OPT"], "old_atoms": [
        {"replaces": "A_OPT", "statement": "old", "order": "LOW", "handler": "python", "facts": ["F_OPTED"], "rule": "F_NOPE = TRUE"}]}]}
    out, notes = timestep.run(rows, changes, lambda: json.dumps(answer), LAW, lambda t: engine_ready(t))
    assert out is rows and any("NOT USED" in n for n in notes)


def test_none_and_unknown_atoms_change_nothing():
    rows = table()
    changes = [{"id": "T_AMD1", "kind": "SUBSTITUTED", "effective_from": "2019-02-01", "replaced_text": "", "note": ""}]
    out, notes = timestep.run(rows, changes, lambda: json.dumps({"changes": [{"id": "T_AMD1", "kind": "NONE", "reason": "renumbering"}]}),
                              LAW, lambda t: engine_ready(t))
    assert [r["RESULTS"] for r in out] == [r["RESULTS"] for r in table()]


def test_old_atoms_must_hold_the_old_words():
    rows = table()
    changes = [{"id": "T_AMD1", "kind": "SUBSTITUTED", "effective_from": "2019-02-01",
                "replaced_text": "in lieu of the tax payable by him", "note": ""}]
    answer = {"changes": [{"id": "T_AMD1", "kind": "SUBSTITUTED", "new_atoms": ["A_OPT"], "old_atoms": [
        {"replaces": "A_OPT", "statement": "The person chooses something else", "order": "LOW", "handler": "python",
         "facts": ["F_OPTED"], "rule": "F_OPTED = TRUE"}]}]}
    out, notes = timestep.run(rows, changes, lambda: json.dumps(answer), LAW, lambda t: engine_ready(t))
    by = {r["ELEMENT"]: r for r in out}
    assert "A_OPT_OLD" not in by and "valid_from" not in by["A_OPT"]["RESULTS"] and any("old words" in n for n in notes)
