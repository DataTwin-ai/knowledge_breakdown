"""Observer: the graph built from a tiny governance master yaml (+ report) - no database, no browser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from observer_ss import data as od  # noqa: E402

YAML = """# GOVERNANCE MASTER TSG01_03 Neither supply (Schedule III) (TG01 Supply & Taxability)
# LAW ARC - 2 laws - status APPROVED
name: x
facts:
  # ---- level 1: gate facts - if one is false, that law stops here
  F_PRR:  # 1
    name: Provider Recipient Relation
    type: CODE
  # ---- LAST: only heavy checks (free text) need these
  F_DOTA:  # 2
    name: Description Of The Activity
    type: TEXT
predicates:
  M_ESTE_A_SBETE: {statement: Service by employee to employer, order: LOW, rule: "F_PRR = 'EMPLOYEE_EMPLOYER'", handler: python}
  M_ANGNS_A_X: {statement: other, order: HIGH, grounding: [F_DOTA], handler: heavy}
groups:
  M_ANGNS_A_ASISIII: {name: Any of the referred steps (M_ANGNS_A_ASISIII), logic: ANY, members: [M_ESTE_PD_ESNS]}
outcomes:
  M_ESTE_LE_ESNS: {outcome: NEITHER_SUPPLY, expr: M_ESTE_A_SBETE, module: M_ESTE}
  M_ANGNS_LE_N: {outcome: NEITHER, expr: M_ANGNS_A_ASISIII AND M_ANGNS_A_X, module: M_ANGNS}
products:
  M_ESTE_PD_ESNS: {statement: Not a supply}
compounds:
- {id: C_ESTE, question: 'Schedule III, entry 1?', includes: [M_ESTE_LE_ESNS], produces: [{product: M_ESTE_PD_ESNS, from: [M_ESTE_LE_ESNS]}]}
"""


def _book(tmp):
    d = tmp / "TG01"
    (d / "report").mkdir(parents=True)
    (d / "TSG01_03_rulebook.yaml").write_text(YAML, encoding="utf-8")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Laws"
    ws.append(["MKP", "Why it is in"])
    ws.append(["CGSTA17_SCHIII_EN1#TEXT", "REFERENCE CGSTA17_SCHIII"])
    ws.append(["CGSTA17_S7_SS2_Ca#TEXT", "MEMBER"])
    m = wb.create_sheet("Molecules")
    m.append(["Molecule", "MKP id", "Element type", "Element id", "Governance master id", "Concept"])
    m.append(["CGSTA17_SCHIII_EN1#TEXT", "M_ESTE", "A", "M_ESTE_A_SBETE", "M_ESTE_A_SBETE", ""])
    m.append(["CGSTA17_S7_SS2_Ca#TEXT", "M_ANGNS", "A", "M_ANGNS_A_X", "M_ANGNS_A_X", ""])
    wb.save(d / "report" / "TSG01_03_report.xlsx")


def test_graph_from_rulebook(tmp_path):
    _book(tmp_path)
    origin = {"F_PRR": {"document": "USER", "method": "ASK", "how_to_get": "Is the buyer your employer?"}}
    refs = [{"mkp_element": "CGSTA17_S7_SS2_Ca#TEXT", "scope": "SELF", "ref_id": "CGSTA17", "target_key":
             "CGSTA17_SCHIII", "source_key": "", "target_status": "INSERTED"}]
    g = od.build(tmp_path, origin=origin, refs=refs)
    n, e = g["nodes"], {(a, b, k) for a, b, k, _t in g["edges"]}
    assert g["tsgs"][0]["id"] == "TSG01_03" and g["tsgs"][0]["status"] == "APPROVED"
    assert n["F_PRR"]["level"] == "L1" and n["F_DOTA"]["level"] == "LAST"
    assert ("F_PRR", "M_ESTE_A_SBETE", "fa") in e and ("F_DOTA", "M_ANGNS_A_X", "fa") in e
    assert n["M_ANGNS_A_ASISIII"]["type"] == "R" and ("M_ESTE_PD_ESNS", "M_ANGNS_A_ASISIII", "self") in e
    assert ("M_ESTE_LE_ESNS", "M_ESTE_PD_ESNS", "lp") in e and ("M_ANGNS_A_ASISIII", "M_ANGNS_LE_N", "al") in e
    assert n["F_PRR"]["origin"]["method"] == "ASK"
    assert g["modules"]["M_ESTE"]["cite"] == "CGST Act Sch. III para 1"
    assert g["modules"]["M_ANGNS"]["refs"][0]["target_status"] == "INSERTED"


def test_citation_and_module():
    assert od.citation("IGSTA17_S8_SS2_P1#TEXT") == "IGST Act s.8(2) proviso 1"
    assert od.citation("CGSTA17_S7_SS1_Ca#TEXT") == "CGST Act s.7(1)(a)"
    assert od.module_of("IH_ANGNS_SIAFOS") == "M_ANGNS" and od.module_of("M_SIAFOS_G_SFA") == "M_SIAFOS"


def test_cross_reference_kind(tmp_path):
    _book(tmp_path)
    p = tmp_path / "TG01" / "report" / "TSG01_03_report.xlsx"
    from openpyxl import load_workbook
    wb = load_workbook(p)
    for r in wb["Molecules"].iter_rows(min_row=2):
        if r[1].value == "M_ESTE":
            r[0].value = "IGSTA17_S8_SS1#TEXT"
    wb.save(p)
    g = od.build(tmp_path)
    assert g["nodes"]["M_ANGNS_A_ASISIII"]["ref_kind"] == "cross"
