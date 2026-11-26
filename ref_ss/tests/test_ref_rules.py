"""Acts -> Rules references (2026-11-25) - no database, no model, no internet. Tiny laws only.

Real case: CGST Act s.16(2)(aa) leaves the ITC condition to the Rules ("as may be prescribed") -> rule 36(4).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("psycopg2")

from ref_ss import auto  # noqa: E402
from ref_ss.detect import detect  # noqa: E402


class Rules:
    """Three rules of a tiny Rules document (what IkpIndex gives)."""
    def __init__(self):
        self.doc = "CGSTR17"
        t = {"CGSTR17": "Central Goods and Services Tax Rules, 2017",
             "CGSTR17_R36": "36. Documentary requirements and conditions for claiming input tax credit.",
             "CGSTR17_R36_SR1": "(1) The input tax credit shall be availed on the basis of an invoice ...",
             "CGSTR17_R36_SR4": "(4) No input tax credit shall be availed unless the details of the invoice have "
                                "been furnished in FORM GSTR-1 and communicated in FORM GSTR-2B.",
             "CGSTR17_R42": "42. Manner of determination of input tax credit in respect of inputs or input services.",
             "CGSTR17_R80": "80. Annual return. (1) Every registered person ... under section 44 ..."}
        self.order = list(t)
        par = {"CGSTR17_R36_SR1": "CGSTR17_R36", "CGSTR17_R36_SR4": "CGSTR17_R36"}
        self.nodes = {k: {"level": "RULE", "parent": par.get(k, "CGSTR17")} for k in t}
        self._t = t

    def text(self, k, _pages=False):
        return self._t[k]


def test_as_may_be_prescribed_in_an_act_is_one_rules_reference():
    text = "(aa) the details of the invoice have been furnished by the supplier in the statement of outward " \
           "supplies and communicated to the recipient in the manner as may be prescribed;"
    found = [r for r in detect("CGSTA17", text, rules_code="CGSTR17") if r["scope"] == "CROSS"]
    assert len(found) == 1 and found[0]["ref_id"] == "CGSTR17" and found[0]["relation"] == "PRESCRIBED_BY"
    assert found[0]["kind"] == "RULES" and found[0]["target_key"] == "" and "prescribed" in found[0]["mention"]


def test_no_prescribed_reference_without_the_setting_in_the_rules_or_when_a_rule_is_named():
    assert not detect("CGSTA17", "as may be prescribed", rules_code=None)
    assert not detect("CGSTR17", "as may be prescribed", rules_code="CGSTR17")
    named = detect("CGSTA17", "the manner prescribed in rule 36 of the Central Goods and Services Tax Rules, 2017",
                   rules_code="CGSTR17")
    assert [r["relation"] for r in named if r["scope"] == "CROSS"] == ["REFERS"]


def test_prescribed_rule_is_found_in_two_small_model_steps_and_only_known_keys_count():
    asked = []

    def ask(prompt):
        req = json.loads(prompt.split("\nINPUT:\n", 1)[1])
        asked.append([p["key"] for p in req["parts"]])
        if len(asked) == 1:
            return json.dumps({"keys": ["CGSTR17_R36"], "reason": "ITC conditions"})
        return json.dumps({"keys": ["CGSTR17_R36_SR4", "CGSTR17_R99"], "reason": "GSTR-2B condition"})
    citing = {"key": "CGSTA17_S16_SS2_Caa", "text": "communicated ... as may be prescribed",
              "mention": "as may be prescribed", "relation": "PRESCRIBED_BY", "target": ""}
    keys, how = auto.find_part(Rules(), citing, 40, ask, "P")
    assert keys == ["CGSTR17_R36_SR4"] and how.startswith("model")
    assert asked[0] == ["CGSTR17_R36", "CGSTR17_R42", "CGSTR17_R80"]            # step 1: rule headings only
    assert asked[1] == ["CGSTR17_R36", "CGSTR17_R36_SR1", "CGSTR17_R36_SR4"]    # step 2: that rule's parts


def test_python_finds_the_rule_that_names_the_section_without_a_model():
    keys, how = auto.find_part(Rules(), {"key": "CGSTA17_S44", "relation": "PRESCRIBED_BY", "mention": "x",
                                         "target": ""}, 40)
    assert keys == ["CGSTR17_R80"] and how.startswith("python")


def test_a_saved_wider_part_gives_way_to_the_exact_part_the_law_names():
    idx = Rules()
    assert auto.narrowest(["CGSTR17_R36"], "CGSTR17_R36_SR4", idx) == ["CGSTR17_R36_SR4"]
    assert auto.narrowest(["CGSTR17_R36_SR4"], "CGSTR17_R36_SR4", idx) == ["CGSTR17_R36_SR4"]
    assert auto.narrowest(["CGSTR17_R42"], "", idx) == ["CGSTR17_R42"]


class Cur:
    """The approved AKPs of the Rules, by source_mkp (what the SELECT in part_done sees)."""
    def __init__(self, have):
        self.have, self.rows = have, []

    def execute(self, _q, params):
        _inst, key, _like, _key = params
        self.rows = [(h,) for h in self.have if h == key or h.startswith(key + "_") or key.startswith(h + "_")]

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, have):
        self.have = have

    def cursor(self):
        return Cur(self.have)

    def commit(self):
        pass


def test_a_part_is_done_only_when_it_or_every_part_of_it_has_an_akp():
    idx = Rules()
    assert not auto.part_done(Conn(["CGSTR17_R36_SR4"]), "s", "akp", "CGSTR17", ["CGSTR17_R36"], idx)
    assert auto.part_done(Conn(["CGSTR17_R36_SR1", "CGSTR17_R36_SR4"]), "s", "akp", "CGSTR17", ["CGSTR17_R36"], idx)
    assert auto.part_done(Conn(["CGSTR17_R36"]), "s", "akp", "CGSTR17", ["CGSTR17_R36_SR4"], idx)   # a part above
    assert auto.part_done(Conn(["CGSTR17_R36_SR4"]), "s", "akp", "CGSTR17", ["CGSTR17_R36"])         # old callers


def _atom(el, statement):
    return {"MKP": "M_ITC", "TYPE": "A", "ELEMENT": el, "CONCEPT": "c",
            "RESULTS": f'statement: "{statement}"; facts: [F_X]; rule: "F_X = TRUE"'}


def test_the_atom_that_names_the_rule_holds_the_exact_part_id():
    rows = [_atom("A_INV", "The recipient holds a tax invoice"),
            _atom("A_R36", "The conditions in rule 36 of the Rules are met")]
    notes = auto.mark_outside(rows, [{"ref_id": "CGSTR17", "mention": "rule 36", "target_key": "CGSTR17_R36",
                                      "relation": "REFERS"}])
    assert "outside_refs: [CGSTR17_R36]" in rows[1]["RESULTS"] and "outside_refs" not in rows[0]["RESULTS"]
    assert any("A_R36 holds CGSTR17_R36" in n for n in notes)


def test_prescribed_goes_to_the_atom_that_says_prescribed_and_holds_the_rules_id():
    rows = [_atom("A_INV", "The recipient holds a tax invoice"),
            _atom("A_2B", "The details are communicated to the recipient in the manner prescribed")]
    auto.mark_outside(rows, [{"ref_id": "CGSTR17", "mention": "as may be prescribed", "target_key": "",
                              "relation": "PRESCRIBED_BY"}])
    assert "outside_refs: [CGSTR17]" in rows[1]["RESULTS"] and "outside_refs" not in rows[0]["RESULTS"]
