"""Atoms must be usable by the engine as written (less hallucination); no database needed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from akp_ss.guard import engine_ready  # noqa: E402

FACT = {"TYPE": "FACTS", "ELEMENT": "F_SUPPLY_FORM", "RESULTS": 'name: "Form"; type: CODE; options: [BARTER, SALE]; source_kind: DOCUMENT'}


def atom(el, results):
    return {"TYPE": "A", "ELEMENT": el, "RESULTS": results}


def test_good_atoms_pass():
    rows = [FACT, atom("A_BARTER", "statement: \"x\"; order: LOW; handler: python; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM = 'BARTER'\""),
            atom("A_OTHER", "statement: \"x\"; order: HIGH; handler: heavy; facts: [F_SUPPLY_FORM]; interpretation: \"Is it another form?\"")]
    assert engine_ready(rows) == []


def test_step_number_as_order_is_refused():
    assert engine_ready([FACT, atom("A_X", 'statement: "x"; order: 1')])[0].startswith("ORDER: A_X")


def test_value_not_in_options_and_bad_grammar_are_refused():
    errs = engine_ready([FACT, atom("A_B", "statement: \"x\"; order: LOW; handler: python; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM = 'SWAP'\""),
                         atom("A_C", "statement: \"x\"; order: LOW; handler: python; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM IN (SALE)\"")])
    assert any(e.startswith("VALUE: A_B") for e in errs) and any(e.startswith("RULE: A_C") for e in errs)


def test_high_atom_needs_question_and_fact():
    errs = engine_ready([atom("A_H", 'statement: "x"; order: HIGH')])
    assert any(e.startswith("QUESTION: A_H") for e in errs) and any(e.startswith("FACTS: A_H") for e in errs)


def test_handler_must_fit_the_order():
    bad = [FACT, atom("A_BARTER", "statement: \"x\"; order: LOW; handler: heavy; facts: [F_SUPPLY_FORM]; "
                                  "rule: \"F_SUPPLY_FORM = 'BARTER'\""),
           atom("A_OTHER", "statement: \"x\"; order: HIGH; facts: [F_SUPPLY_FORM]; interpretation: \"Is it another form?\"")]
    errs = engine_ready(bad)
    assert any("A_BARTER handler heavy does not fit order LOW" in e for e in errs)
    assert any("A_OTHER handler must be python, light or heavy" in e for e in errs)
    light = [FACT, atom("A_OTHER", "statement: \"x\"; order: HIGH; handler: light; facts: [F_SUPPLY_FORM]; "
                                   "interpretation: \"Is it another form?\"")]
    assert engine_ready(light) == []


# ---- 2026-11-26: python means python - no READ / ASK fact behind a python atom, every WORKED_OUT fact has its formula

def fact(el, results):
    return {"TYPE": "FACTS", "ELEMENT": el, "RESULTS": results}


def test_python_atom_on_a_read_fact_is_refused():
    rows = [fact("F_FOS", "type: CODE; options: [SALE, RENTAL]; source_kind: READ"),
            atom("A_S", "statement: \"x\"; order: LOW; handler: python; facts: [F_FOS]; rule: \"F_FOS = 'SALE'\"")]
    assert any(e.startswith("PYTHON: A_S") and "READ" in e for e in engine_ready(rows))


def test_python_atom_without_source_kind_or_facts_row_is_refused():
    rows = [fact("F_FOS", "type: CODE; options: [SALE]"),
            atom("A_S", "statement: \"x\"; order: LOW; handler: python; facts: [F_FOS]; rule: \"F_FOS = 'SALE'\"")]
    assert any(e.startswith("PYTHON: A_S") for e in engine_ready(rows))
    rows = [atom("A_R", "statement: \"x\"; order: LOW; handler: python; facts: [F_OLD]; rule: \"F_OLD = 'X'\"")]
    assert any(e.startswith("PYTHON: A_R") and "source_kind 'none'" in e for e in engine_ready(rows, {"F_OLD"}))
    assert any("A_R" in e for e in engine_ready(rows, set()))          # unknown fact: refused by the rule check


def test_calculated_value_as_a_new_fact_is_refused_but_a_registry_one_is_kept():
    rows = [fact("F_A", "type: AMOUNT; source_kind: DOCUMENT"),
            fact("F_X", 'type: AMOUNT; source_kind: WORKED_OUT; derivation: "F_A * 2"')]
    assert any(e.startswith("FACT: F_X") and "step" in e for e in engine_ready(rows, {}))
    reg = {"F_X": {"type": "AMOUNT", "source_kind": "WORKED_OUT", "derivation": "F_A * 2"}}
    assert not [e for e in engine_ready(rows, reg) if "F_X" in e]


# ---- 2026-11-27: Python repairs - a python atom that Python cannot decide goes to the LLM; registry ids kept

from akp_ss.guard import repair_handlers, structure  # noqa: E402
from akp_ss.parse import concept_words  # noqa: E402
from transport import get_field  # noqa: E402


def test_python_atom_on_read_or_unknown_fact_is_moved_to_the_llm():
    rows = [fact("F_SCT", "type: CODE; options: [PAID, NONE]; source_kind: READ"),
            atom("A_FC", "statement: \"The import of services is for a consideration\"; order: LOW; handler: python; "
                         "facts: [F_SCT]; rule: \"F_SCT = 'PAID'\""),
            atom("A_OLD", "statement: \"The old fact holds the value X\"; order: LOW; handler: python; facts: [F_OLD]; "
                          "rule: \"F_OLD = 'X'\""),
            fact("F_UTA", "type: AMOUNT; source_kind: DOCUMENT"),
            atom("A_AMT", "statement: \"The unpaid tax amount is above zero\"; order: LOW; handler: python; "
                          "facts: [F_UTA]; rule: \"F_UTA > 0\"; how: \"unpaid tax from the return\"")]
    notes = repair_handlers(rows)
    assert len(notes) == 2
    for r in rows[1:3]:
        assert get_field(r, "order") == "HIGH" and get_field(r, "handler") == "light" and not get_field(r, "rule")
        assert get_field(r, "interpretation").startswith("Decide whether:")
    assert get_field(rows[4], "handler") == "python" and get_field(rows[4], "rule") == "F_UTA > 0"
    assert engine_ready(rows, {"F_OLD"}) == []


def test_registry_fact_keeps_its_short_id():
    rows = [{"MKP": "M", "TYPE": "MKP", "ELEMENT": "M", "CONCEPT": "NA", "RESULTS": "x"},
            {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_SKOT", "CONCEPT": "Supply Kind", "RESULTS": "type: CODE"}]
    assert not [e for e in structure(rows, [], False, {"F_SKOT"}) if "F_SKOT: fact id" in e]
    assert [e for e in structure(rows, [], False, set()) if "F_SKOT: fact id" in e]


def test_id_like_concept_becomes_words():
    assert concept_words("A_RETURN_S44_NOT_FURNISHED") == "Return Not Furnished"
    assert concept_words("Return", 'statement: "Any registered person fails to furnish"') == "Return Registered Person"
    assert concept_words("", "") == ""


# ---- 2026-11-27: money laws - raw facts, the calculation as steps inside the atom

from akp_ss.guard import fill_source_kind, atom_steps  # noqa: E402
from akp_ss.formula import check as fcheck  # noqa: E402

LATE = ("statement: \"The return is furnished after the due date\"; order: LOW; handler: python; "
        "facts: [F_ARDD, F_ARFD, F_ARIF, F_DOTC]; step1: \"failure_end = IF(F_ARIF = TRUE, F_ARFD, F_DOTC)\"; "
        "step2: \"days_late = MAX(0, DAYS_BETWEEN(F_ARDD, failure_end))\"; rule: \"days_late > 0\"; "
        "how: \"due date table; GSTR-9 ARN date and status; run date\"")
FEE = ("statement: \"A late fee of one hundred rupees for every day, at most five thousand rupees\"; role: CONSEQUENCE; "
       "order: LOW; handler: python; when: A_LATE; facts: [F_LFPD, F_LFC]; "
       "step1: \"fee_daily = A_LATE.days_late * F_LFPD\"; step2: \"late_fee = MIN(fee_daily, F_LFC)\"; "
       "rule: \"late_fee > 0\"; result: late_fee; how: \"days late times the fee per day, capped\"")
RAW = [fact("F_ARDD", "type: DATE; source_kind: LOOKUP"), fact("F_ARFD", "type: DATE; source_kind: DOCUMENT"),
       fact("F_ARIF", "type: BOOL; source_kind: DOCUMENT"), fact("F_DOTC", "type: DATE; source_kind: LOOKUP"),
       fact("F_LFPD", "type: AMOUNT; source_kind: LOOKUP"), fact("F_LFC", "type: AMOUNT; source_kind: LOOKUP")]


def test_formula_grammar_accepts_if_dates_and_refuses_the_rest():
    assert fcheck("IF(F_A > F_B, 'LATE', 'IN_TIME')")[1] == []
    assert fcheck("MAX(0, DAYS_BETWEEN(F_DUE, F_FILED))")[0] == {"F_DUE", "F_FILED"}
    assert fcheck('"F_DAYS * @F_RATE"') == ({"F_DAYS", "F_RATE"}, [])          # quotes and @ are tidied
    assert fcheck("SQRT(F_A)")[1] and fcheck("MIN(F_A)")[1] and fcheck("IF F_A THEN F_B")[1]
    assert fcheck("days_late * 2")[1] and fcheck("days_late * 2", {"days_late"})[1] == []


def test_late_fee_atoms_with_steps_pass():
    rows = RAW + [atom("A_LATE", LATE), atom("A_FEE", FEE)]
    assert atom_steps(rows[-1]) == {"fee_daily": "A_LATE.days_late * F_LFPD", "late_fee": "MIN(fee_daily, F_LFC)"}
    assert engine_ready(rows, {}) == []
    assert repair_handlers(rows, {}) == []                                       # nothing moved to the LLM


def test_bad_steps_are_refused_with_the_reason():
    bad_fee = FEE.replace("A_LATE.days_late", "A_LATE.days_over").replace("facts: [F_LFPD, F_LFC]", "facts: [F_LFPD]")
    rows = RAW + [atom("A_LATE", LATE), atom("A_FEE", bad_fee.replace("result: late_fee", "result: fee"))]
    errs = engine_ready(rows, {})
    assert any(e.startswith("STEP: A_FEE") and "days_over" in e for e in errs)
    assert any(e.startswith("STEP: A_FEE") and "F_LFC" in e and "list it" in e for e in errs)
    assert any("A_FEE" in e and "result fee" in e for e in errs)
    read = [fact("F_ARIF", "type: BOOL; source_kind: READ")] + [r for r in RAW if r["ELEMENT"] != "F_ARIF"]
    assert any(e.startswith("PYTHON: A_LATE") and "F_ARIF" in e for e in engine_ready(read + [atom("A_LATE", LATE)], {}))


def test_judge_dropped_source_kind_is_copied_from_the_generators_or_the_registry():
    std = [fact("F_A", "type: DATE"), fact("F_B", "type: CODE")]
    gen = [fact("F_A", "type: DATE; source_kind: DOCUMENT")]
    notes = fill_source_kind(std, gen, {"F_B": {"source_kind": "READ"}})
    assert len(notes) == 2
    assert get_field(std[0], "source_kind") == "DOCUMENT" and get_field(std[1], "source_kind") == "READ"


def test_python_atom_on_registry_fact_uses_the_registry_source_kind():
    rows = [atom("A_R", "statement: \"The person is registered now\"; order: LOW; handler: python; facts: [F_PRS]; "
                        "rule: \"F_PRS = 'REGISTERED'\"; how: \"GSTIN on the filed return\"")]
    reg = {"F_PRS": {"type": "CODE", "options": ["REGISTERED", "CANCELLED"], "source_kind": "LOOKUP"}}
    assert repair_handlers(rows, reg) == [] and engine_ready(rows, reg) == []


# ---- 2026-11-27 (3): Python RUNS every python atom on test inputs; what it cannot do alone goes to the LLM

def _verdict(rows):
    notes = repair_handlers(rows, {})
    return ("LLM" if get_field(rows[-1], "order") == "HIGH" else "PYTHON"), " ".join(notes)


def test_python_test_keeps_real_python_and_moves_the_rest_to_the_llm():
    prs = fact("F_PRS", "type: CODE; options: [REGISTERED, CANCELLED]; source_kind: LOOKUP")
    py = "order: LOW; handler: python; "
    cases = {
        "registered": ([prs, atom("A1", py + "facts: [F_PRS]; rule: \"F_PRS = 'REGISTERED'\"; how: \"GSTIN on the return\"")],
                       "PYTHON", ""),
        "threshold": ([fact("F_AT", "type: AMOUNT; source_kind: DOCUMENT"),
                       atom("A1", py + "facts: [F_AT]; rule: \"F_AT <= 15000000\"; how: \"turnover of the return\"")],
                      "PYTHON", ""),
        "days late": ([fact("F_DD", "type: DATE; source_kind: LOOKUP"), fact("F_FD", "type: DATE; source_kind: DOCUMENT"),
                       atom("A1", py + "facts: [F_DD, F_FD]; step1: \"days_late = MAX(0, DAYS_BETWEEN(F_DD, F_FD))\"; "
                                       "rule: \"days_late > 0\"; how: \"due date table; ARN date\"")], "PYTHON", ""),
        "no how": ([prs, atom("A1", py + "facts: [F_PRS]; rule: \"F_PRS = 'REGISTERED'\"")], "LLM", "no how"),
        "hsn rate": ([fact("F_RT", 'name: "Rate Of Tax For HSN"; type: PERCENT; source_kind: LOOKUP'),
                      atom("A1", py + "facts: [F_RT]; rule: \"F_RT > 5\"; how: \"HSN rate table\"")], "LLM", "READ"),
        "text": ([fact("F_D", "type: TEXT; source_kind: DOCUMENT"),
                  atom("A1", py + "facts: [F_D]; rule: \"F_D IS_SET\"; how: \"description\"")], "LLM", "TEXT"),
        "broken step": ([fact("F_D", "type: DATE; source_kind: DOCUMENT"),
                         atom("A1", py + "facts: [F_D]; step1: \"x = F_D + 30\"; rule: \"x > 0\"; how: \"date\"")],
                        "LLM", "needs numbers"),
        "always true": ([fact("F_A", "type: AMOUNT; source_kind: DOCUMENT"),
                         atom("A1", py + "facts: [F_A]; rule: \"F_A >= 0\"; how: \"value\"")], "LLM", "always True"),
        "read fact": ([fact("F_FOS", "type: CODE; options: [SALE, RENTAL]; source_kind: READ"),
                       atom("A1", py + "facts: [F_FOS]; rule: \"F_FOS = 'SALE'\"; how: \"invoice\"")], "LLM", "READ"),
    }
    for name, (rows, want, why) in cases.items():
        got, notes = _verdict(rows)
        assert got == want, (name, notes)
        assert why in notes, (name, notes)


def test_late_fee_is_worked_out_by_python_with_real_numbers():
    import datetime as dt
    from akp_ss.formula import run
    rows = RAW + [atom("A_LATE", LATE), atom("A_FEE", FEE)]
    env = {"F_ARDD": dt.date(2024, 12, 31), "F_ARFD": dt.date(2025, 1, 20), "F_ARIF": True,
           "F_DOTC": dt.date(2025, 3, 1), "F_LFPD": 100.0, "F_LFC": 5000.0}
    for a in rows[-2:]:
        for n, f in atom_steps(a).items():
            env[n] = env[f"{a['ELEMENT']}.{n}"] = run(f, env)
    assert env["days_late"] == 20 and env["late_fee"] == 2000
    assert run(get_field(rows[-1], "rule"), env) is True


def test_amount_atom_without_how_or_that_cannot_run_is_refused_not_sent_to_the_llm():
    rows = RAW + [atom("A_LATE", LATE), atom("A_FEE", FEE.replace("; how: \"days late times the fee per day, capped\"", ""))]
    assert any(e.startswith("HOW: A_FEE") for e in engine_ready(rows, {}))
    assert repair_handlers(rows, {}) == [] and get_field(rows[-1], "handler") == "python"


def test_return_not_filed_yet_has_no_filing_date_and_still_works():
    """IF only works out the branch it takes: no filing date is needed when the return is not filed."""
    import datetime as dt
    from akp_ss.formula import run
    rows = RAW + [atom("A_LATE", LATE), atom("A_FEE", FEE)]
    env = {"F_ARDD": dt.date(2024, 12, 31), "F_ARIF": False, "F_DOTC": dt.date(2025, 12, 31),
           "F_LFPD": 100.0, "F_LFC": 5000.0}
    for a in rows[-2:]:
        for n, f in atom_steps(a).items():
            env[n] = env[f"{a['ELEMENT']}.{n}"] = run(f, env)
    assert env["days_late"] == 365 and env["late_fee"] == 5000
