"""2026-11-21 (no database): fact hierarchy - which fact to get first, read from a rulebook only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from governance_master_ss.hierarchy import hierarchy, parse  # noqa: E402


def P(rule, facts=None, order="LOW", handler="python"):
    p = {"statement": "x", "order": order, "handler": handler}
    if order == "LOW":
        p["rule"] = rule
    else:
        p["grounding"] = facts or []
    return p


def rb_indian():
    """Indian if Aadhaar holder AND adult: Aadhaar is the gate, DOB only after it."""
    return {
        "facts": {"F_AAD": {"name": "Aadhaar Holder"}, "F_DOB": {"name": "Date Of Birth"}, "F_DESC": {"name": "Desc"}},
        "predicates": {"M_I_A_AAD": P("F_AAD = TRUE"), "M_I_A_ADULT": P("F_DOB <= '2008-01-01'"),
                       "M_I_A_STORY": P("", ["F_DESC"], order="HIGH", handler="heavy")},
        "groups": {},
        "outcomes": {"M_I_LE_IN": {"outcome": "INDIAN", "expr": "M_I_A_AAD AND (M_I_A_ADULT OR M_I_A_STORY)",
                                   "module": "M_I"}},
        "compounds": [{"id": "C_I", "question": "Indian?", "includes": ["M_I_LE_IN"]}],
    }


def test_parse_keeps_brackets_and_not():
    assert parse("A AND (B OR NOT C)") == ("AND", [("ID", "A"), ("OR", [("ID", "B"), ("NOT", ("ID", "C"))])])


def test_gate_first_then_branch_then_heavy():
    tsg, law = hierarchy(rb_indian())
    assert [(r["fact"], r["level"]) for r in tsg] == [("F_AAD", "1 gate"), ("F_DOB", "2"),
                                                      ("F_DESC", "LAST (heavy check only)")]
    dob = next(r for r in law if r["fact"] == "F_DOB")
    assert dob["asked_only_if"] == "F_AAD" and dob["law"] == "M_I" and dob["outcome"] == "M_I_LE_IN"


def test_cheaper_source_first_inside_a_level():
    rb = rb_indian()
    rb["predicates"]["M_I_A_RES"] = P("F_RES = TRUE")
    rb["facts"]["F_RES"] = {"name": "Resident"}
    rb["outcomes"]["M_I_LE_IN"]["expr"] = "M_I_A_RES AND M_I_A_AAD"
    tsg, _ = hierarchy(rb, kinds={"F_AAD": {"kind": "ASK"}})
    assert [r["fact"] for r in tsg] == ["F_RES", "F_AAD"]            # both gates: DOCUMENT before ASK


def test_any_group_of_one_fact_is_a_gate_and_referred_law_is_level_3():
    rb = rb_indian()
    rb["predicates"].update({"M_I_A_PAID": P("F_CON = 'PAID'"), "M_I_A_AGREED": P("F_CON = 'AGREED'"),
                             "M_S_A_EMP": P("F_REL = 'EMPLOYEE'")})
    rb["facts"].update({"F_CON": {"name": "Consideration"}, "F_REL": {"name": "Relation"}})
    rb["groups"] = {"M_I_G_C": {"logic": "ANY", "members": ["M_I_A_PAID", "M_I_A_AGREED"]},
                    "M_I_A_SCH": {"logic": "ANY", "members": ["M_S_PD_E"]}}
    rb["outcomes"]["M_S_LE_E"] = {"outcome": "NS", "expr": "M_S_A_EMP", "module": "M_S"}
    rb["compounds"].append({"id": "C_S", "question": "S?", "includes": ["M_S_LE_E"],
                            "produces": [{"product": "M_S_PD_E", "from": ["M_S_LE_E"]}]})
    rb["outcomes"]["M_I_LE_IN"]["expr"] = "M_I_G_C AND M_I_A_SCH"
    _, law = hierarchy(rb)
    mine = {r["fact"]: r["level"] for r in law if r["law"] == "M_I"}
    assert mine == {"F_CON": "1 gate", "F_REL": "3"}


def test_two_clauses_of_one_compound_are_separate_ways():
    """7(2): clause a OR clause b - clause b's gate does not stop clause a."""
    rb = rb_indian()
    rb["predicates"]["M_B_A_GOVT"] = P("F_GOVT = TRUE")
    rb["facts"]["F_GOVT"] = {"name": "Govt"}
    rb["outcomes"]["M_B_LE_X"] = {"outcome": "NS", "expr": "M_B_A_GOVT", "module": "M_B"}
    rb["compounds"][0]["includes"].append("M_B_LE_X")
    _, law = hierarchy(rb)
    dob = next(r for r in law if r["fact"] == "F_DOB")
    assert "F_GOVT" not in dob["asked_only_if"]


def test_yaml_facts_in_ask_order_with_comments_and_same_content():
    """The facts section is written in ask order with the level as a comment - the engine reads the same content."""
    import yaml
    from governance_master_engine import _comment_facts, _facts_in_order
    rb = rb_indian()
    rb["facts"] = {"F_DESC": rb["facts"]["F_DESC"], "F_DOB": rb["facts"]["F_DOB"], "F_AAD": rb["facts"]["F_AAD"],
                   "F_DOI": {"name": "Law date"}}
    tsg, _ = hierarchy(rb)
    rb["facts"] = _facts_in_order(rb["facts"], tsg)
    assert list(rb["facts"]) == ["F_AAD", "F_DOB", "F_DESC", "F_DOI"]       # a fact no law asks for stays at the end
    text = _comment_facts(yaml.safe_dump(rb, sort_keys=False), tsg)
    assert "  # ---- level 1: gate facts" in text and "  F_DOB:  # 2. 2 | 1 law(s) | asked only if M_I: F_AAD" in text
    assert yaml.safe_load(text) == rb
