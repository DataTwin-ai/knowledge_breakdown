"""2026-11-19: atoms from the old AKP cache get a fresh fact + SAME check (no database, no model)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from akp_engine import _old_standard  # noqa: E402
from transport import get_field  # noqa: E402


def _row(t, el, res):
    return {"MKP": "M_X", "TYPE": t, "ELEMENT": el, "CONCEPT": "c", "RESULTS": res}


def test_old_same_atom_becomes_a_plain_atom_again():
    data = {"status": "APPROVED", "standard": [
        _row("A_REF", "M_OTHER_A_SIVBS", 'statement: "The supply is a supply of vouchers"; facts: [F_VSS]'),
        _row("LE", "M_X_LE_MAIN", 'expr: "M_OTHER_A_SIVBS"')]}
    data["standard"][0].update({"SAME_AKP_ID": "M_OTHER_A_SIVBS", "SAME_BY": "model", "_facts": ["F_VSS"]})
    rows = _old_standard(data)
    assert rows[0]["TYPE"] == "A" and "SAME_AKP_ID" not in rows[0] and "_facts" not in rows[0]
    assert data["standard"][0]["TYPE"] == "A_REF"                 # the old answer itself is not changed


def test_not_reused_when_not_approved_no_atoms_or_no_wording():
    assert _old_standard(None) is None
    assert _old_standard({"status": "TECHNICAL_ERROR", "standard": [_row("A", "M_X_A_A", 'statement: "x"')]}) is None
    assert _old_standard({"status": "APPROVED", "python": True, "standard": [_row("A", "M_X_A_A", 'statement: "x"')]}) is None
    assert _old_standard({"status": "APPROVED", "standard": [_row("LE", "M_X_LE_A", 'expr: "x"')]}) is None
    assert _old_standard({"status": "APPROVED", "standard": [_row("A_REF", "M_O_A_A", "")]}) is None


# ---- 2026-11-19: old fact ids belong to the OLD fact table - matched again by name, never by the old id

from akp_engine import _detach_old_facts  # noqa: E402


def test_old_fact_ids_are_set_aside_and_undeclared_ones_come_from_the_old_table():
    rows = [
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_PRS", "CONCEPT": "Person Relation",
         "RESULTS": 'name: "Person Relation Status"; entity: PERSON; type: CODE; options: [RELATED, NOT_RELATED]'},
        _row("A", "M_X_A_REL", 'statement: "The supply is made between related persons"; facts: [F_PRS]; '
                               'rule: "F_PRS = \'RELATED\'"'),
        _row("A", "M_X_A_BUS", 'statement: "The supply is in the course of business"; facts: [F_AOTD]; '
                               'rule: "F_AOTD = TRUE"'),
        _row("LE", "M_X_LE_S", 'expr: "M_X_A_REL AND M_X_A_BUS"'),
    ]
    old = {"F_AOTD": {"fact_id": "F_AOTD", "name": "Activity Or Transaction Description", "entity": "ACTIVITY",
                      "type": "TEXT", "unit": None, "options": [], "evidence": [], "derived_from": [], "nl": "x",
                      "concept": "Transaction Description"}}
    notes = _detach_old_facts(rows, old)
    facts = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "FACTS"}
    assert "F_PRS" not in facts and "F_AOTD" not in facts                  # no old id is kept
    assert len(facts) == 2 and all(k.startswith("F_REUSED_") for k in facts)
    rel = next(r for r in rows if r["ELEMENT"] == "M_X_A_REL")
    assert "F_PRS" not in rel["RESULTS"] and any(k in rel["RESULTS"] for k in facts)
    names = sorted(get_field(r, "name") for r in facts.values())
    assert names == ["Activity Or Transaction Description", "Person Relation Status"]   # matched by name later
    assert any("OLD FACTS" in n for n in notes)


def test_a_fact_missing_from_the_old_table_is_logged():
    rows = [_row("A", "M_X_A_Y", 'statement: "x y z w"; facts: [F_GONE]; rule: "F_GONE = TRUE"')]
    notes = _detach_old_facts(rows, {})
    assert any("OLD FACT MISSING" in n and "F_GONE" in n for n in notes)
