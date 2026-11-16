"""Fact flow layer 3: a new fact is checked against the registered facts (no database, no model)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from akp_ss.factcheck import check, gate_same  # noqa: E402

DOB = {"fact_id": "F_DOB", "name": "Date Of Birth", "entity": "PERSON", "type": "DATE", "unit": None, "options": [],
       "nl": "", "aliases": [], "similarity": 0.95}
FORM = {"fact_id": "F_FOS", "name": "Form Of Supply", "entity": "SUPPLY", "type": "CODE", "unit": None,
        "options": ["SALE", "BARTER", "OTHER"], "nl": "", "aliases": [], "similarity": 0.91}
KNOWN = {"F_DOB": {"name": "Date Of Birth", "type": "DATE"}, "F_FOS": {"name": "Form Of Supply", "type": "CODE"}}


def table():
    return [
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_BIRTH_DATE", "CONCEPT": "NA",
         "RESULTS": 'name: "Birth date"; entity: PERSON; type: DATE'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_BIRTH_YEAR", "CONCEPT": "NA",
         "RESULTS": 'name: "Year of birth"; entity: PERSON; type: NUMBER'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_SUPPLY_KIND", "CONCEPT": "NA",
         "RESULTS": 'name: "Kind of supply"; entity: SUPPLY; type: CODE; options: [SALE, GIFT]'},
        {"MKP": "M_X", "TYPE": "A", "ELEMENT": "A_OLD", "CONCEPT": "",
         "RESULTS": 'statement: "x"; facts: [F_BIRTH_DATE, F_BIRTH_YEAR]; rule: "F_BIRTH_YEAR < 1960"'},
        {"MKP": "M_X", "TYPE": "A", "ELEMENT": "A_SALE", "CONCEPT": "",
         "RESULTS": "statement: \"y\"; facts: [F_SUPPLY_KIND]; rule: \"F_SUPPLY_KIND = 'SALE'\""},
    ]


def run(answer):
    rows = table()
    nearest = lambda v, cv=None: [DOB] if v == "birth" else [FORM] if v == "supply" else []
    embed = lambda text: "birth" if "birth" in text.lower() else "supply" if "supply" in text.lower() else None
    asked = []
    decisions, n = check(rows, KNOWN, embed, nearest, lambda req: asked.append(req) or json.dumps(answer), {"source_mkp": "K"})
    return rows, decisions, n, asked


def test_same_derived_and_refused_same():
    answer = {"facts": [
        {"fact": "F_BIRTH_DATE", "decision": "SAME", "existing": "F_DOB", "reason": "same input"},
        {"fact": "F_BIRTH_YEAR", "decision": "DERIVED", "derived_from": ["F_DOB"], "derivation": "year(F_DOB)"},
        {"fact": "F_SUPPLY_KIND", "decision": "SAME", "existing": "F_FOS", "reason": "same"}]}
    rows, decisions, n, asked = run(answer)
    assert n == 3 and len(asked) == 1                                    # one call per MKP
    by = {d["fact"]: d for d in decisions}
    assert by["F_BIRTH_DATE"]["decision"] == "SAME"
    assert by["F_BIRTH_YEAR"]["decision"] == "DERIVED"
    assert by["F_SUPPLY_KIND"]["decision"] == "NEW" and "GIFT" in by["F_SUPPLY_KIND"]["reason"]   # options do not fit
    atom = next(r for r in rows if r["ELEMENT"] == "A_OLD")
    assert "facts: [F_DOB, F_BIRTH_YEAR]" in atom["RESULTS"]           # the atom now reads the registered fact
    year = next(r for r in rows if r["ELEMENT"] == "F_BIRTH_YEAR")
    assert 'derived_from: [F_DOB]; derivation: "year(F_DOB)"' in year["RESULTS"]
    assert [r["ELEMENT"] for r in rows if r["TYPE"] == "FACTS"] == ["F_DOB", "F_BIRTH_YEAR", "F_SUPPLY_KIND"]


def test_same_to_a_fact_already_in_the_table_keeps_one_row():
    rows = table()
    rows.insert(0, {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_DOB", "CONCEPT": "NA", "RESULTS": 'name: "Date Of Birth"'})
    answer = {"facts": [{"fact": "F_BIRTH_DATE", "decision": "SAME", "existing": "F_DOB"}]}
    check(rows, KNOWN, lambda t: "birth" if "birth" in t.lower() else None,
          lambda v, cv=None: [DOB] if v == "birth" else [], lambda req: json.dumps(answer), {})
    assert [r["ELEMENT"] for r in rows if r["TYPE"] == "FACTS"].count("F_DOB") == 1


def test_no_candidates_no_model_call():
    rows = table()
    called = []
    decisions, n = check(rows, KNOWN, lambda t: None, lambda v, cv=None: [], lambda req: called.append(1), {})
    assert n == 0 and not called and all(d["decision"] == "NEW" for d in decisions)


def test_gate_same():
    new = {"RESULTS": 'name: "Supplier GSTIN"; entity: SUPPLIER; type: ID'}
    assert gate_same(new, {"type": "ID", "entity": "RECIPIENT"}) == "entity SUPPLIER is not RECIPIENT"
    assert gate_same(new, {"type": "TEXT", "entity": "SUPPLIER"}).startswith("type")
    assert gate_same(new, {"type": "ID", "entity": "SUPPLIER"}) == ""


def test_same_band_keeps_close_atoms_with_the_same_test():
    from akp_engine import _same_band
    rows = [{"MKP": "M_X", "TYPE": "A", "ELEMENT": "A_REG", "CONCEPT": "",
             "RESULTS": 'facts: [F_SIR]; rule: "F_SIR = TRUE"'}]
    cands = [{"element": "A_REG", "candidates": [
        {"akp_id": "M_Y_A_RP", "similarity": 0.85, "results": 'facts: [F_SIR]; rule: "F_SIR = TRUE"'},     # same test
        {"akp_id": "M_Y_A_OT", "similarity": 0.86, "results": 'facts: [F_SIR]; rule: "F_SIR = FALSE"'},    # other rule
        {"akp_id": "M_Z_A_HI", "similarity": 0.95, "results": 'facts: [F_ABC]'}]}]                          # above 0.90
    kept = [c["akp_id"] for c in _same_band(rows, cands, 0.90)[0]["candidates"]]
    assert kept == ["M_Y_A_RP", "M_Z_A_HI"]


def test_second_check_at_save_sees_facts_of_other_workers():
    """Clauses of one section run at the same time: a fact another worker registered meanwhile is found at save."""
    rows = table()
    n = len(rows)
    answer = {"facts": [{"fact": "F_BIRTH_DATE", "decision": "SAME", "existing": "F_DOB"}]}
    asked = []
    decisions, k = check(rows, KNOWN, lambda t: "birth" if "birth" in t.lower() else None,
                         lambda v, cv=None: [DOB] if v == "birth" else [],
                         lambda req: asked.append(req) or json.dumps(answer), {}, since={"F_FOS"})
    assert k >= 1 and any(d["decision"] == "SAME" for d in decisions)
    assert len(rows) == n                                     # no row removed: positions carry the embeddings
    # the same facts were already known when the split began -> no second model call
    called = []
    decisions, k = check(table(), KNOWN, lambda t: "birth", lambda v, cv=None: [DOB], lambda req: called.append(1), {},
                         since={"F_DOB", "F_FOS"})
    assert k == 0 and not called and decisions == []
