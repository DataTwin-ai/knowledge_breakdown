"""References after the workers (bug list C, 2026-11-18) - no database, no model, no internet.

Real case: 2(17)(h) of the CGST Act was substituted by the CGST (Amendment) Act, 2018 (No. 31 of 2018). Only the
part of that Act which changes 2(17)(h) is needed - not its 200+ pages.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("psycopg2")

from ref_ss import auto  # noqa: E402
from ref_ss.store import clean_title  # noqa: E402


class Idx:
    """The parts of a small amending Act (what IkpIndex gives)."""
    def __init__(self):
        self.doc = "ACT_31_2018"
        t = {"ACT_31_2018": "The Central Goods and Services Tax (Amendment) Act, 2018",
             "ACT_31_2018_S1": "1. Short title and commencement.",
             "ACT_31_2018_S2": "2. In section 2 of the Central Goods and Services Tax Act, 2017 (hereafter the principal Act),",
             "ACT_31_2018_S2_Ca": "(a) in clause (4), for the words \"the Appellate Authority\" ...",
             "ACT_31_2018_S2_Cb": "(b) in clause (17), for sub-clause (h), the following sub-clause shall be substituted, "
                                  "namely: (h) activities of a race club ...",
             "ACT_31_2018_S3": "3. In section 7 of the principal Act, in sub-section (1), in clause (d), ..."}
        self.order = list(t)
        self.nodes = {k: {"level": "SECTION"} for k in t}
        self._t = t

    def text(self, k, _pages=False):
        own = self._t[k]
        if k == "ACT_31_2018_S2":                       # a section's text holds its clauses
            own += " " + self._t["ACT_31_2018_S2_Ca"] + " " + self._t["ACT_31_2018_S2_Cb"]
        if k == "ACT_31_2018_S2_Cb":                    # a clause is read with its lead-in
            own = self._t["ACT_31_2018_S2"] + " " + own
        return own


def test_needles_name_the_citing_provision():
    assert auto.needles("CGSTA17_S2_C17_SCh#TEXT") == ["section 2", "(17)", "(h)"]
    assert auto.needles("CGSTR17_R36_SS4") == ["rule 36", "(4)"]


def test_python_finds_the_exact_amending_part():
    keys, how = auto.find_part(Idx(), {"key": "CGSTA17_S2_C17_SCh", "relation": "AMENDS", "mention": "x"}, 40)
    assert keys == ["ACT_31_2018_S2_Cb"] and how.startswith("python")


def test_model_is_asked_only_when_python_finds_nothing_and_only_known_keys_count():
    asked = []

    def ask(prompt):
        asked.append(json.loads(prompt.split("\nINPUT:\n", 1)[1]))
        return json.dumps({"keys": ["ACT_31_2018_S3", "ACT_31_2018_S99"], "reason": "changes 7(1)(d)"})
    keys, how = auto.find_part(Idx(), {"key": "CGSTA17_S9_SS9", "relation": "AMENDS", "mention": "x"}, 2, ask, "P")
    assert keys == ["ACT_31_2018_S3"] and how.startswith("model") and len(asked) == 1   # S99 invented: dropped


def test_small_document_cited_as_a_whole_is_run_whole():
    keys, how = auto.find_part(Idx(), {"key": "CGSTA17_S9_SS3", "relation": "REFERS", "mention": "notified"}, 40)
    assert keys == ["ACT_31_2018"] and how.startswith("whole document")


def test_self_targets_once_each_and_capped():
    rows = [{"scope": "SELF", "instrument": "CGSTA17", "target_key": t} for t in
            ("CGSTA17_SCHIII", "CGSTA17_SCHIII", "CGSTA17_S2_C119", "CGSTA17_S25")] + \
           [{"scope": "CROSS", "instrument": "CGSTA17", "target_key": ""}]
    assert auto.self_targets(rows, 2) == [("CGSTA17", "CGSTA17_SCHIII"), ("CGSTA17", "CGSTA17_S2_C119")]
    assert auto.self_targets(rows, 0) == []


def test_title_loses_the_footnote_verb():
    assert clean_title("Inserted by The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)") == \
        "The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)"
    assert clean_title("Substituted vide Notification No. 2/2019") == "Notification No. 2/2019"
    assert clean_title("The Finance Act, 2020") == "The Finance Act, 2020"


def test_principal_act_as_amended_is_refused_for_an_amending_act(monkeypatch, tmp_path):
    """Bug list C13: ACT_31_2018 was VERIFIED with the 200+ page CGST Act as amended (its footnotes name
    'Act 31 of 2018'). The amending Act names its number in its title, on page 1."""
    from ref_ss import fetch
    pages = {1: "THE CENTRAL GOODS AND SERVICES TAX ACT, 2017 ARRANGEMENT OF SECTIONS CHAPTER I PRELIMINARY",
             2: "THE CENTRAL GOODS AND SERVICES TAX ACT, 2017 ARRANGEMENT OF SECTIONS ... 1. Substituted by the "
                "Central Goods and Services Tax (Amendment) Act, 2018 (31 of 2018)"}
    monkeypatch.setattr(fetch, "first_pages_text", lambda path, n=2: " ".join(pages[i] for i in range(1, n + 1)))
    ok, why = fetch.verify_pdf(tmp_path / "x.pdf", 31, 2018, "AMENDING_ACT")
    assert not ok and "not in the title on page 1" in why
    pages[1] = "THE CENTRAL GOODS AND SERVICES TAX (AMENDMENT) ACT, 2018 NO. 31 OF 2018 [29th August, 2018]"
    assert fetch.verify_pdf(tmp_path / "x.pdf", 31, 2018, "AMENDING_ACT")[0]


# ---- reference design 2026-11-19 item 7: only the exact atom that names an outside document holds it

def _atom(el, statement):
    return {"MKP": "M_GANS", "TYPE": "A", "ELEMENT": el, "CONCEPT": "c",
            "RESULTS": f'statement: "{statement}"; facts: [F_X]; rule: "F_X = TRUE"'}


def test_the_atom_that_names_the_notification_number_holds_it():
    from ref_ss.auto import mark_outside
    rows = [_atom("A_GOVT", "The supply is made by the Central Government"),
            _atom("A_NOTIF", "The activity is notified in Notification No. 14/2017-Central Tax (Rate)")]
    notes = mark_outside(rows, [{"ref_id": "N_CTR_14_2017", "mention": "Notification No. 14 / 2017-Central Tax (Rate)"}])
    assert "outside_refs: [N_CTR_14_2017]" in rows[1]["RESULTS"] and "outside_refs" not in rows[0]["RESULTS"]
    assert any("A_NOTIF holds N_CTR_14_2017" in n for n in notes)


def test_no_number_one_reference_goes_to_the_notified_atom_and_unnamed_is_listed():
    from ref_ss.auto import mark_outside
    rows = [_atom("A_GOVT", "The supply is made by a local authority"), _atom("A_NOTIF", "The activity is notified")]
    mark_outside(rows, [{"ref_id": "N_X", "mention": "as may be notified"}])
    assert "outside_refs: [N_X]" in rows[1]["RESULTS"]
    rows = [_atom("A_GOVT", "The supply is made by a local authority")]
    notes = mark_outside(rows, [{"ref_id": "N_CTR_14_2017", "mention": "Notification 14/2017"}])
    assert "outside_refs" not in rows[0]["RESULTS"] and any("REFERENCE NOT ON ATOM" in n for n in notes)


def test_marking_twice_keeps_one_field():
    from ref_ss.auto import mark_outside
    rows = [_atom("A_NOTIF", "Notification 14/2017 and Notification 13/2017 apply")]
    refs = [{"ref_id": "N_A", "mention": "Notification 14/2017"}, {"ref_id": "N_B", "mention": "Notification 13/2017"}]
    mark_outside(rows, refs)
    mark_outside(rows, refs)
    assert rows[0]["RESULTS"].count("outside_refs:") == 1 and "outside_refs: [N_A, N_B]" in rows[0]["RESULTS"]
