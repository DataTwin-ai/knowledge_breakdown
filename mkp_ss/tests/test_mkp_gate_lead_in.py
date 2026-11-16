"""MKP gate: a clause job's parent lead-in (no database, no model).

Real case 2026-11-16: job CGSTA17_S7_SS3_Cb (clause (b) of s.7(3)) got the lead-in of CGSTA17_S7_SS3 to read; the
model also wrote "UNL CGSTA17_S7_SS3" and the gate stopped the job. The lead-in is inside the clause MKP, so that
UNL row is dropped; a UNL row for any other provision still stops the job.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mkp_ss.check import GateError, standard_gate  # noqa: E402

C = "C_CGSTA17_S7_SS3_Cb"
LEAD = "Subject to the provisions of sub-sections (1), (1A) and (2), the Government may, on the recommendations of " \
       "the Council, specify, by notification, the transactions that are to be treated as—"
TEXT = LEAD + " (b) a supply of services and not as a supply of goods."


def ctx():
    prov = lambda key, role, level, parent: {"key": key, "role": role, "level": level, "parent": parent,
                                             "status": "", "source_text": ""}
    return {"cluster": {"id": C, "shared_amendments": [{"owner": "CGSTA17_S7_SS3"}]},
            "expected": ["CGSTA17_S7_SS3_Cb"],
            "provisions": [prov("CGSTA17_S7_SS3", "LEAD_IN", "SUB_SECTION", "CGSTA17_S7"),
                           prov("CGSTA17_S7_SS3_Cb", "PROVISION", "CLAUSE", "CGSTA17_S7_SS3"),
                           prov("CGSTA17_S7_SS2", "CONTEXT", "SUB_SECTION", "CGSTA17_S7")],
            "legal_parents": {}, "references": {}}


def rows(unl):
    r = lambda t, el, res: {"CLUSTER": C, "TYPE": t, "ELEMENT": el, "RESULTS": res}
    return [r("BATCH", C, "scope: CGSTA17_S7_SS3_Cb"),
            r("MKP", "CGSTA17_S7_SS3_Cb#TEXT", f'citation_key: CGSTA17_S7_SS3_Cb; part: TEXT; text: "{TEXT}"; '
              'concept: "Notified Transactions As Services"; knowledge_type: POWER; parent: CGSTA17_S7_SS3'),
            r("SP", "CGSTA17_S7_SS3_Cb#TEXT", f'sp: SP1; text: "{TEXT}"')] + \
        [r("UNL", el, "reason: text not in the expected list") for el in unl] + [r("SUM", C, "mkps: 1")]


def test_lead_in_unl_row_is_dropped_and_the_clause_passes():
    got, warnings = standard_gate(rows(["CGSTA17_S7_SS3"]), ctx(), fidelity_mode="WARN")
    assert "UNL" not in {r["TYPE"] for r in got}
    assert [r["ELEMENT"] for r in got if r["TYPE"] == "MKP"] == ["CGSTA17_S7_SS3_Cb#TEXT"]
    assert any("CGSTA17_S7_SS3 dropped - it is the lead-in" in w for w in warnings)


def test_unl_row_for_another_provision_still_stops_the_job():
    with pytest.raises(GateError, match="source boundary includes unselected CGSTA17_S7_SS2"):
        standard_gate(rows(["CGSTA17_S7_SS2"]), ctx(), fidelity_mode="WARN")


def test_clause_without_extra_rows_passes_as_before():
    got, _ = standard_gate(rows([]), ctx(), fidelity_mode="WARN")
    assert [r["TYPE"] for r in got] == ["BATCH", "MKP", "SP", "SUM"]
