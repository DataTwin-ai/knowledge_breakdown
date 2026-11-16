"""AKP abbreviated ids: every element and fact gets its id from Python before saving (no database needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from akp_ss.ids import abbreviate, check, norm_name  # noqa: E402

M = "M_SIAFOS"


def rows():
    return [
        {"MKP": M, "TYPE": "MKP", "ELEMENT": M, "CONCEPT": "NA", "RESULTS": 'citation: "Section 7(1)(a)"'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_SUPPLY_FORM", "CONCEPT": "NA",
         "RESULTS": 'name: "Form of supply"; entity: SUPPLY; type: CODE; options: [SALE, BARTER]; used_by: [A_SALE, A_BARTER]'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_SUPPLIER_IS_REGISTERED", "CONCEPT": "NA",
         "RESULTS": 'name: "Supplier is registered"; entity: SUPPLIER; type: BOOL'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_SALE", "CONCEPT": "Sale",
         "RESULTS": 'statement: "Supply is a sale"; facts: [F_SUPPLY_FORM]; rule: "F_SUPPLY_FORM = \'SALE\'"'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_BARTER", "CONCEPT": "Barter",
         "RESULTS": 'statement: "Supply is a barter"; name: "Supply Is A Barter"; facts: [F_SUPPLY_FORM]; '
                    'rule: "F_SUPPLY_FORM = \'BARTER\'"'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_SALE_2", "CONCEPT": "Sale",
         "RESULTS": 'statement: "Supply is a sale again"; name: "Supply Is A Sale"; facts: [F_SUPPLIER_IS_REGISTERED]'},
        {"MKP": M, "TYPE": "G", "ELEMENT": "G_FORMS", "CONCEPT": "NA", "RESULTS": "logic: ANY; members: [A_SALE, A_BARTER]"},
        {"MKP": M, "TYPE": "LE", "ELEMENT": "LE_SIAFOS", "CONCEPT": "NA",
         "RESULTS": 'outcome: SUPPLY; expr: "G_FORMS AND A_SALE_2"; feeds: [PD_SIAFOS]'},
        {"MKP": M, "TYPE": "PD", "ELEMENT": "PD_SIAFOS", "CONCEPT": "NA", "RESULTS": "from: LE_SIAFOS"},
        {"MKP": M, "TYPE": "SP", "ELEMENT": "SP1", "CONCEPT": "NA", "RESULTS": 'text: "x"; elements: [A_SALE, A_BARTER]'},
    ]


def test_every_id_is_abbreviated_and_rewritten_everywhere():
    t = rows()
    mapping, notes = abbreviate(t, M, {})
    assert mapping["A_SALE"] == "M_SIAFOS_A_S"                 # no name: the words of the working id
    assert mapping["A_BARTER"] == "M_SIAFOS_A_SIAB"            # from its name
    assert mapping["A_SALE_2"] == "M_SIAFOS_A_SIAS"
    assert mapping["G_FORMS"] == "M_SIAFOS_G_F"
    assert mapping["LE_SIAFOS"] == "M_SIAFOS_LE_S" and mapping["PD_SIAFOS"] == "M_SIAFOS_PD_S"
    assert mapping["F_SUPPLY_FORM"] == "F_FOS" and mapping["F_SUPPLIER_IS_REGISTERED"] == "F_SIR"
    by = {r["ELEMENT"]: r for r in t}
    assert by["M_SIAFOS_A_S"]["RESULTS"] == 'statement: "Supply is a sale"; facts: [F_FOS]; rule: "F_FOS = \'SALE\'"'
    assert by["M_SIAFOS_G_F"]["RESULTS"] == "logic: ANY; members: [M_SIAFOS_A_S, M_SIAFOS_A_SIAB]"
    assert 'expr: "M_SIAFOS_G_F AND M_SIAFOS_A_SIAS"; feeds: [M_SIAFOS_PD_S]' in by["M_SIAFOS_LE_S"]["RESULTS"]
    assert by["SP1"]["RESULTS"] == 'text: "x"; elements: [M_SIAFOS_A_S, M_SIAFOS_A_SIAB]'
    assert "used_by: [M_SIAFOS_A_S, M_SIAFOS_A_SIAB]" in by["F_FOS"]["RESULTS"]
    assert check(t, M) == [] and notes == []


def test_registered_fact_keeps_its_id_and_same_name_reuses_it():
    known = {"F_FOS": {"name": "Form Of The Supply", "type": "CODE"},       # same meaning (small words ignored)
             "F_SIR": {"name": "Supply Is Reverse", "type": "BOOL"}}       # same acronym, different fact
    t = rows()
    mapping, _ = abbreviate(t, M, known)
    assert mapping["F_SUPPLY_FORM"] == "F_FOS"                  # one fact, one id in every law
    assert mapping["F_SUPPLIER_IS_REGISTERED"] == "F_SIR2"     # a different fact never takes a used id
    t = rows()
    t[1]["ELEMENT"] = "F_FOS"                                   # the model reused the registered id
    t[3]["RESULTS"] = t[3]["RESULTS"].replace("F_SUPPLY_FORM", "F_FOS")
    mapping, _ = abbreviate(t, M, known)
    assert mapping["F_FOS"] == "F_FOS"


def test_same_acronym_in_one_law_gets_a_number():
    t = [{"MKP": M, "TYPE": "A", "ELEMENT": "A_SALE", "CONCEPT": "", "RESULTS": 'name: "Supply Is A Sale"'},
         {"MKP": M, "TYPE": "A", "ELEMENT": "A_SALE_OTHER", "CONCEPT": "", "RESULTS": 'name: "Seller Is A Supplier"'}]
    mapping, _ = abbreviate(t, M, {})
    assert list(mapping.values()) == ["M_SIAFOS_A_SIAS", "M_SIAFOS_A_SIAS2"]


def test_long_name_is_cut_and_noted():
    t = [{"MKP": M, "TYPE": "A", "ELEMENT": "A_X", "CONCEPT": "",
          "RESULTS": 'name: "one two three four five six seven eight nine ten eleven"'}]
    mapping, notes = abbreviate(t, M, {})
    assert mapping["A_X"] == "M_SIAFOS_A_OTTFFSSENT" and notes and "cut" in notes[0]


def test_resave_is_unchanged():
    t = rows()
    abbreviate(t, M, {})
    before = [dict(r) for r in t]
    known = {"F_FOS": {"name": "Form of supply"}, "F_SIR": {"name": "Supplier is registered"}}
    abbreviate(t, M, known)
    assert t == before


def test_gate_finds_wrong_ids():
    t = [{"MKP": M, "TYPE": "A", "ELEMENT": "A_SALE", "CONCEPT": "", "RESULTS": ""},
         {"MKP": M, "TYPE": "A", "ELEMENT": "M_OTHER_A_X", "CONCEPT": "", "RESULTS": ""}]
    errs = check(t, M)
    assert any("A_SALE" in e for e in errs) and any("does not start with" in e for e in errs)


def test_norm_name():
    assert norm_name("Date Of Birth") == norm_name("dates of birth") == "date birth"


def test_saved_alias_reuses_the_fact():
    known = {"F_DOB": {"name": "Date Of Birth", "type": "DATE", "aliases": ["Birth date of the person"]}}
    t = [{"MKP": M, "TYPE": "FACTS", "ELEMENT": "F_PERSON_BIRTH_DATE", "CONCEPT": "NA",
          "RESULTS": 'name: "Birth date of the person"; type: DATE'},
         {"MKP": M, "TYPE": "A", "ELEMENT": "A_AGE", "CONCEPT": "", "RESULTS": "facts: [F_PERSON_BIRTH_DATE]"}]
    mapping, _ = abbreviate(t, M, known)
    assert mapping["F_PERSON_BIRTH_DATE"] == "F_DOB" and t[1]["RESULTS"] == "facts: [F_DOB]"


def test_same_test_in_one_law_is_merged():
    from akp_ss.ids import merge_same_tests
    t = [{"MKP": M, "TYPE": "A", "ELEMENT": "A_REG", "CONCEPT": "", "RESULTS": 'order: LOW; facts: [F_SIR]; rule: "F_SIR = TRUE"'},
         {"MKP": M, "TYPE": "A", "ELEMENT": "A_REG2", "CONCEPT": "", "RESULTS": 'order: LOW; facts: [F_SIR]; rule: "F_SIR = TRUE"'},
         {"MKP": M, "TYPE": "A", "ELEMENT": "A_X", "CONCEPT": "", "RESULTS": 'order: LOW; facts: [F_X]; rule: "F_X = TRUE"'},
         {"MKP": M, "TYPE": "LE", "ELEMENT": "LE_1", "CONCEPT": "", "RESULTS": 'expr: "A_REG AND A_X AND A_REG2"'},
         {"MKP": M, "TYPE": "SP", "ELEMENT": "SP1", "CONCEPT": "", "RESULTS": "elements: [A_REG2]"}]
    notes = merge_same_tests(t)
    assert [r["ELEMENT"] for r in t] == ["A_REG", "A_X", "LE_1", "SP1"] and "merged" in notes[0]
    assert t[2]["RESULTS"] == 'expr: "A_REG AND A_X AND A_REG"' and t[3]["RESULTS"] == "elements: [A_REG]"
    t = [dict(t0) for t0 in [
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_1", "CONCEPT": "", "RESULTS": 'order: LOW; facts: [F_SIR]; rule: "F_SIR = TRUE"'},
        {"MKP": M, "TYPE": "A", "ELEMENT": "A_2", "CONCEPT": "", "RESULTS": 'order: LOW; facts: [F_SIR]; rule: "F_SIR = TRUE"'},
        {"MKP": M, "TYPE": "G", "ELEMENT": "G_1", "CONCEPT": "", "RESULTS": "logic: ANY; members: [A_1, A_2]"}]]
    assert "kept" in merge_same_tests(t)[0] and len(t) == 3


def test_two_merges_in_one_law():
    from akp_ss.ids import merge_same_tests
    a = lambda el, f: {"MKP": M, "TYPE": "A", "ELEMENT": el, "CONCEPT": "", "RESULTS": f'order: LOW; facts: [{f}]; rule: "{f} = TRUE"'}
    t = [a("A_1", "F_A"), a("A_2", "F_A"), a("A_3", "F_B"), a("A_4", "F_B"),
         {"MKP": M, "TYPE": "LE", "ELEMENT": "LE_1", "CONCEPT": "", "RESULTS": 'expr: "A_1 AND A_2 AND A_3 AND A_4"'}]
    merge_same_tests(t)
    assert [r["ELEMENT"] for r in t] == ["A_1", "A_3", "LE_1"] and t[2]["RESULTS"] == 'expr: "A_1 AND A_1 AND A_3 AND A_3"'
