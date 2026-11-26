"""2026-11-27: a money law in the governance master - the section 47(1) late fee example of the AKP prompt.
Every python atom carries its calculation (compute), the amount atom runs after its `when` atom and gives `result`,
and the outcome carries the rupee amount. No database needed. Run: python -m pytest -q governance_master_ss/tests
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from akp_ss.parse import parse_rows  # noqa: E402
from akp_ss.guard import engine_ready, structure  # noqa: E402
from governance_master_ss.build import build  # noqa: E402
from governance_master_ss.pack import check_pack  # noqa: E402
from governance_master_ss.source import Snapshot  # noqa: E402

CFG = json.loads((ROOT / "Config" / "governance_master_map.json").read_text(encoding="utf-8"))
PROMPT = (ROOT / "Prompt" / "law_arc_akp_prompt.txt").read_text(encoding="utf-8")
KEY = "CGSTA17_S47_SS1#TEXT"


def example_rows():
    """The full section 47(1) example of the prompt, exactly as the models are shown it."""
    lines = [l for l in PROMPT.splitlines() if l.startswith(("M_LFR|", "TXN|FACTS|F_REGISTRATION_STATUS", "TXN|FACTS|F_RETURN",
                                                              "TXN|FACTS|F_CHECK", "TXN|FACTS|F_LATE_FEE"))]
    head = ('MKP|TYPE|ELEMENT|CONCEPT|RESULTS\nM_LFR|MKP|M_LFR|NA|citation: "Section 47(1)"; citation_key: CGSTA17_S47_SS1; '
            'part: TEXT; knowledge_type: PENALTY; frame: PENALTY\n')
    tail = ('\nM_LFR|SP|SP1|NA|role: CONDITION; text: "fails to furnish ... by the due date"; elements: [A_REGISTERED, A_RETURN_LATE]'
            '\nM_LFR|SP|SP2|NA|role: CONSEQUENCE; text: "shall pay a late fee"; elements: [A_LATE_FEE_AMOUNT]')
    return parse_rows(head + "\n".join(lines) + tail, "M_LFR")


def test_prompt_example_passes_every_python_check():
    rows = example_rows()
    assert len([r for r in rows if r["TYPE"] == "FACTS"]) == 7          # raw inputs only
    assert engine_ready(rows, {}) == []
    assert structure(rows, [], True, set()) == []


def built():
    rows = example_rows()
    akp = [{"instrument": "CGSTA17", "version_id": "V_TEST", "source_mkp": KEY, "mkp_id": "M_LFR", "ordinal": i,
            "type": r["TYPE"], "element": r["ELEMENT"], "concept": r["CONCEPT"], "results": r["RESULTS"],
            "akp_id": f"{KEY}::{r['ELEMENT']}" if r["TYPE"] == "A" else None, "same_akp_id": None}
           for i, r in enumerate(rows, 1)]
    gkp = [{"source_mkp": KEY, "akp_element": r["ELEMENT"], "akp_type": "A", "tg_id": "TG11", "tsg_id": "TSG11_04",
            "status": "APPROVED"} for r in rows if r["TYPE"] == "A"]
    snap = Snapshot(akp=akp, facts={}, gkp=gkp, time_facts=[], questions={}, definitions=[])
    return build(snap, "TSG11_04", "Late fee", "TG11", "RETURNS", CFG, {}, "TSG11_04-v1", depth=3)


def test_yaml_atoms_carry_their_calculation_and_the_money():
    rb = built().rulebook
    preds, facts = rb["predicates"], rb["facts"]
    late = next(p for p in preds.values() if p.get("rule") == "days_late > 0")
    assert late["handler"] == "python" and "due-date table" in late["how"]
    assert late["facts"] == ["F_RETURN_DUE_DATE", "F_RETURN_FILED_DATE", "F_RETURN_FURNISHED", "F_CHECK_DATE"]
    assert late["compute"] == {"failure_end": "IF(F_RETURN_FURNISHED = TRUE, F_RETURN_FILED_DATE, F_CHECK_DATE)",
                               "days_late": "MAX(0, DAYS_BETWEEN(F_RETURN_DUE_DATE, failure_end))"}
    late_id = next(k for k, p in preds.items() if p is late)
    fee = next(p for p in preds.values() if p.get("result") == "late_fee")
    assert fee["handler"] == "python" and fee["when"] == late_id and fee["rule"] == "late_fee > 0"
    assert fee["compute"] == {"fee_daily": f"{late_id}.days_late * F_LATE_FEE_PER_DAY",
                              "late_fee": "MIN(fee_daily, F_LATE_FEE_CAP)"}
    fee_id = next(k for k, p in preds.items() if p is fee)
    out = next(iter(rb["outcomes"].values()))
    assert out["amount"] == f"{fee_id}.late_fee" and out["unit"] == "INR"
    # facts: raw inputs only - each one can be mapped to a field by fact origin; no step is a fact
    assert set(facts) >= {"F_REGISTRATION_STATUS", "F_RETURN_DUE_DATE", "F_RETURN_FILED_DATE", "F_RETURN_FURNISHED",
                          "F_CHECK_DATE", "F_LATE_FEE_PER_DAY", "F_LATE_FEE_CAP"}
    assert not {"failure_end", "days_late", "fee_daily", "late_fee"} & set(facts)
    assert {f["source_kind"] for k, f in facts.items() if k.startswith(("F_RETURN", "F_LATE", "F_CHECK", "F_REG"))} \
        <= {"DOCUMENT", "LOOKUP"}
    assert check_pack(rb) == []
    yaml.safe_dump(rb)
