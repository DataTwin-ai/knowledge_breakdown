"""AKP updates of 2026-11-16 (no database, no model): name match before the model with Python's SAME gate,
fact concepts in the search, SAME by law and concept, short LK ids, akp.group_names."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from akp_engine import _same_band  # noqa: E402
from akp_ss.factcheck import check, name_match  # noqa: E402
from akp_ss.facts import _concept  # noqa: E402
from akp_ss.ids import abbreviate  # noqa: E402
from akp_ss.store import group_names  # noqa: E402

KNOWN = {"F_SV": {"name": "Supply Value", "type": "AMOUNT", "aliases": [], "entity": "SUPPLY", "options": []},
         "F_FOS": {"name": "Form Of Supply", "type": "CODE", "aliases": ["Supply Form"], "entity": "SUPPLY",
                   "options": ["SALE", "BARTER", "OTHER"]}}


def fact(el, results, concept="NA"):
    return {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": el, "CONCEPT": concept, "RESULTS": results}


def atom(el, results):
    return {"MKP": "M_X", "TYPE": "A", "ELEMENT": el, "CONCEPT": "", "RESULTS": results}


# ---------------------------------------------------------------- layer 2 before the model
def test_same_name_same_type_is_reused_without_a_model_call():
    rows = [fact("F_SUPPLY_VALUE", 'name: "Supply Values"; entity: SUPPLY; type: AMOUNT'),
            atom("A_X", 'statement: "x"; facts: [F_SUPPLY_VALUE]; rule: "F_SUPPLY_VALUE > 0"')]
    called = []
    decisions, asked = check(rows, KNOWN, lambda t: None, lambda v, cv=None: [], lambda r: called.append(1), {})
    assert called == [] and asked == 0
    assert decisions == [{"fact": "F_SUPPLY_VALUE", "name": "Supply Values", "decision": "SAME", "existing": "F_SV",
                          "reason": "same name as a registered fact (Python, no model)"}]
    assert [r["ELEMENT"] for r in rows] == ["F_SV", "A_X"] and "F_SV > 0" in rows[1]["RESULTS"]


def test_saved_alias_counts_and_options_must_fit():
    ok = [fact("F_SF", 'name: "Supply Form"; entity: SUPPLY; type: CODE; options: [SALE]')]
    assert name_match(ok, KNOWN)[0]["existing"] == "F_FOS"
    bad = [fact("F_SF", 'name: "Supply Form"; entity: SUPPLY; type: CODE; options: [GIFT]')]
    d = name_match(bad, KNOWN)[0]
    assert d["decision"] == "NEW" and "GIFT" in d["reason"] and bad[0]["ELEMENT"] == "F_SF"


def test_same_name_other_type_is_a_different_fact_at_save_too():
    rows = [fact("F_SUPPLY_VALUE", 'name: "Supply Value"; type: CODE; options: [HIGH, LOW]')]
    assert name_match(rows, KNOWN)[0]["decision"] == "NEW"
    assert abbreviate(rows, "M_X", KNOWN)[0] == {"F_SUPPLY_VALUE": "F_SV2"}       # own id, not F_SV
    same = [fact("F_SUPPLY_VALUE", 'name: "Supply Value"; type: AMOUNT')]
    assert abbreviate(same, "M_X", KNOWN)[0] == {"F_SUPPLY_VALUE": "F_SV"}


def test_save_step_keeps_row_positions():
    rows = [fact("F_SV", 'name: "Supply Value"; type: AMOUNT'),
            fact("F_SUPPLY_VALUE", 'name: "Supply Value"; type: AMOUNT')]
    name_match(rows, KNOWN, drop_duplicates=False)
    assert len(rows) == 2


# ---------------------------------------------------------------- fact concept
def test_fact_concept_is_read_and_searched():
    assert _concept({"CONCEPT": "Supply Form"}) == "Supply Form" and _concept({"CONCEPT": "NA"}) == ""
    rows = [fact("F_NATURE", 'name: "Nature Of Supply"; entity: SUPPLY; type: CODE; options: [SALE]', "Supply Form")]
    seen = []

    def nearest(v, cv=None):
        seen.append((v, cv))
        return []
    check(rows, KNOWN, lambda t: f"vec:{t}", nearest, lambda r: "", {})
    assert seen == [("vec:Nature Of Supply", "vec:Supply Form")]


def test_model_request_carries_the_concept():
    rows = [fact("F_NATURE", 'name: "Nature Of Supply"; entity: SUPPLY; type: CODE', "Supply Form")]
    got = []
    cand = {"fact_id": "F_FOS", "name": "Form Of Supply", "concept": "Supply Form", "entity": "SUPPLY",
            "type": "CODE", "options": [], "similarity": 0.95}
    check(rows, KNOWN, lambda t: t, lambda v, cv=None: [cand],
          lambda r: got.append(r) or json.dumps({"facts": []}), {})
    assert got[0]["new_facts"][0]["concept"] == "Supply Form"


# ---------------------------------------------------------------- SAME by law and concept
def test_same_band_keeps_a_candidate_found_by_concept():
    rows = [atom("A_N", 'statement: "a"; facts: [F_A]; rule: "F_A = 1"')]
    cands = [{"element": "A_N", "candidates": [
        {"akp_id": "M_Y_A_1", "results": 'facts: [F_B]', "similarity": 0.75, "concept_similarity": 0.95},
        {"akp_id": "M_Y_A_2", "results": 'facts: [F_B]', "similarity": 0.85, "concept_similarity": 0.50},
        {"akp_id": "M_Y_A_3", "results": 'facts: [F_B]', "similarity": 0.93, "concept_similarity": 0.10}]}]
    kept = [c["akp_id"] for c in _same_band(rows, cands, 0.90, 0.92)[0]["candidates"]]
    assert kept == ["M_Y_A_1", "M_Y_A_3"]


# ---------------------------------------------------------------- ids + groups
def test_link_rows_are_named_by_their_relation():
    rows = [{"MKP": "M_IOSFC", "TYPE": "LK", "ELEMENT": "LK_T_CGSTA17_S7_SS1_Cb_AMD1", "CONCEPT": "NA",
             "RESULTS": "relation: AMENDS; target: CGSTA17_S7_SS1_Cb"},
            {"MKP": "M_IOSFC", "TYPE": "LK", "ELEMENT": "LK_2", "CONCEPT": "NA", "RESULTS": "relation: AMENDS"}]
    assert abbreviate(rows, "M_IOSFC", {})[0] == {"LK_T_CGSTA17_S7_SS1_Cb_AMD1": "M_IOSFC_LK_AMENDS",
                                                 "LK_2": "M_IOSFC_LK_AMENDS2"}


def test_group_names_of_atoms_only():
    assert json.loads(group_names(atom("A", 'statement: "x"; groups: [TSG01_01, TSG03_04, BAD]'))) == ["TSG01_01", "TSG03_04"]
    assert json.loads(group_names(atom("A", 'statement: "x"'))) == []
    assert group_names({"TYPE": "G", "RESULTS": "groups: [TSG01_01]"}) is None


# ---------------------------------------------------------------- options worded differently (2026-11-16 run)
SCS = {"F_SCS": {"name": "Supply Consideration Status", "type": "CODE", "aliases": [], "entity": "SUPPLY",
                 "options": ["PAID", "PAYABLE", "NONE"]}}


def test_same_name_other_option_words_goes_to_the_model_and_is_mapped():
    rows = [fact("F_SUPPLY_CONSIDERATION", 'name: "Supply Consideration Status"; entity: SUPPLY; type: CODE; '
                 'options: [PAID, AGREED_TO_BE_PAID, NONE]', "Consideration Status"),
            atom("A_NONE", "statement: \"x\"; facts: [F_SUPPLY_CONSIDERATION]; rule: \"F_SUPPLY_CONSIDERATION = 'NONE'\""),
            atom("A_AGREED", "statement: \"y\"; facts: [F_SUPPLY_CONSIDERATION]; "
                 "rule: \"F_SUPPLY_CONSIDERATION = 'AGREED_TO_BE_PAID'\"")]
    sent = []
    answer = {"facts": [{"fact": "F_SUPPLY_CONSIDERATION", "decision": "SAME", "existing": "F_SCS",
                         "option_map": {"AGREED_TO_BE_PAID": "PAYABLE"}, "reason": "same input"}]}
    decisions, asked = check(rows, SCS, lambda t: None, lambda v, cv=None: [],
                             lambda r: sent.append(r) or json.dumps(answer), {})
    assert asked == 1 and sent[0]["candidates"]["F_SUPPLY_CONSIDERATION"][0]["fact_id"] == "F_SCS"
    assert decisions[0]["decision"] == "SAME" and decisions[0]["option_map"] == {"AGREED_TO_BE_PAID": "PAYABLE"}
    assert "F_SCS = 'PAYABLE'" in rows[2]["RESULTS"] and "F_SCS = 'NONE'" in rows[1]["RESULTS"]
    assert "options: [PAID, PAYABLE, NONE]" in rows[0]["RESULTS"]


def test_an_unmapped_option_is_still_refused():
    rows = [fact("F_X", 'name: "Supply Consideration Status"; entity: SUPPLY; type: CODE; options: [GIFT]')]
    answer = {"facts": [{"fact": "F_X", "decision": "SAME", "existing": "F_SCS", "reason": "same"}]}
    decisions, _ = check(rows, SCS, lambda t: None, lambda v, cv=None: [], lambda r: json.dumps(answer), {})
    assert decisions[0]["decision"] == "NEW" and "not mapped" in decisions[0]["reason"]
    answer["facts"][0]["option_map"] = {"GIFT": "NEW"}            # a real new value: added to the fact
    rows = [fact("F_X", 'name: "Supply Consideration Status"; entity: SUPPLY; type: CODE; options: [GIFT]')]
    decisions, _ = check(rows, SCS, lambda t: None, lambda v, cv=None: [], lambda r: json.dumps(answer), {})
    assert decisions[0]["decision"] == "SAME" and rows[0]["ELEMENT"] == "F_SCS"


def test_concept_without_small_words_and_warnings():
    from akp_ss.facts import concept_warnings
    assert _concept({"CONCEPT": "Form Of Supply"}) == "Form Supply"
    w = concept_warnings([fact("F_A", 'name: "Form Of Supply"', "Form Of Supply"),
                          fact("F_B", 'name: "Description Of Supplier Activity"', "Description Supplier Business Activity"),
                          fact("F_C", 'name: "Date Of Invoice"'),
                          fact("F_D", 'name: "Supply Consideration Status"', "Consideration Status")])
    assert [x.split(":")[0] for x in w] == ["F_A", "F_B", "F_C"]
