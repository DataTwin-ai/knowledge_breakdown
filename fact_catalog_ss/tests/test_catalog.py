"""Fact catalog: router (no database) and the catalog built from fact tables (local PostgreSQL).
Run: python -m pytest -q fact_catalog_ss/tests
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fact_catalog_ss.router import expand, route  # noqa: E402

CAT = [
    {"fact_id": "F_INVOICE_DATE", "kind": "DOCUMENT", "derived_from": [], "tsgs": {"TSG05_01": 2, "TSG02_02": 1}},
    {"fact_id": "F_SUPPLIER_STATE", "kind": "DOCUMENT", "derived_from": [], "tsgs": {"TSG03_01": 1}},
    {"fact_id": "F_POS_STATE", "kind": "DOCUMENT", "derived_from": [], "tsgs": {"TSG03_01": 1}},
    {"fact_id": "F_INTER_STATE", "kind": "DERIVED", "derived_from": ["F_SUPPLIER_STATE", "F_POS_STATE"],
     "tsgs": {"TSG03_01": 1}},
    {"fact_id": "F_HSN_RATE", "kind": "LOOKUP", "derived_from": [], "tsgs": {"TSG02_02": 1}},
    {"fact_id": "F_CONSIDERATION", "kind": "ASK", "derived_from": [], "tsgs": {"TSG01_01": 1}},
]


def test_derived_facts_come_free():
    assert "F_INTER_STATE" in expand(["F_SUPPLIER_STATE", "F_POS_STATE"], CAT)
    assert "F_INTER_STATE" not in expand(["F_SUPPLIER_STATE"], CAT)


def test_router_says_which_laws_a_document_can_check():
    r = {x["tsg"]: x for x in route(["F_INVOICE_DATE", "F_SUPPLIER_STATE", "F_POS_STATE"], CAT)}
    assert r["TSG03_01"]["status"] == "READY"                     # inter-State is derived from the two states
    assert r["TSG05_01"]["status"] == "READY"
    assert r["TSG02_02"]["status"] == "PARTIAL" and r["TSG02_02"]["missing_by_kind"] == {"LOOKUP": ["F_HSN_RATE"]}
    assert r["TSG01_01"]["status"] == "NOT_RUN"


def test_catalog_from_the_fact_tables(tmp_path):
    import ikp_engine
    import kp_settings
    from fact_catalog_engine import build_catalog
    try:
        conn = ikp_engine.get_connection(kp_settings.build_paths())
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    cur = conn.cursor()
    cur.execute("""DROP TABLE IF EXISTS fc_fact, fc_atom_fact, fc_akp, fc_gkp, fc_catalog, fc_index;
    CREATE TABLE fc_fact (fact_id text PRIMARY KEY, name text, entity text, data_type text, unit text, options jsonb,
                          derived_from jsonb, nl text);
    CREATE TABLE fc_atom_fact (instrument text, version_id text, source_mkp text, akp_version int, atom text,
                               fact_id text, rule text, is_latest boolean);
    CREATE TABLE fc_akp (version_id text, source_mkp text, element text, results text, is_latest boolean);
    CREATE TABLE fc_gkp (source_mkp text, akp_element text, tsg_id text, status text, is_latest boolean);
    INSERT INTO fc_fact VALUES ('F_SUPPLY_CONSIDERATION','Consideration','SUPPLY','CODE',NULL,'[]','[]','x');
    INSERT INTO fc_atom_fact VALUES ('CGSTA17','V1','CGSTA17_S7_SS1_Ca#TEXT',1,'A_SALE','F_SUPPLY_CONSIDERATION','r',true),
                                    ('CGSTA17','V1','CGSTA17_S7_SS1_Ca#TEXT',1,'A_COB','F_SUPPLY_CONSIDERATION','r',true);
    INSERT INTO fc_akp VALUES ('V1','CGSTA17_S7_SS1_Ca#TEXT','A_SALE','order: LOW',true),
                              ('V1','CGSTA17_S7_SS1_Ca#TEXT','A_COB','order: HIGH; resolver: HEAVY_MODEL',true);
    INSERT INTO fc_gkp VALUES ('CGSTA17_S7_SS1_Ca#TEXT','A_SALE','TSG01_01','APPROVED',true),
                              ('CGSTA17_S7_SS1_Ca#TEXT','A_COB','TSG01_06','APPROVED',true);""")
    conn.commit()
    cfg = kp_settings.load(fresh=True)
    cfg["governance_master"]["tables"].update({"fact": "fc_fact", "akp": "fc_akp", "gkp": "fc_gkp"})
    cfg["governance_master"]["output_dir"] = str(ROOT / "Output" / "fc_test")
    cfg["fact_catalog"].update({"table": "fc_catalog", "index_table": "fc_index"})
    r = build_catalog(cfg, conn)
    f = r["catalog"][0]
    assert r["facts"] == 1 and f["tsgs"] == {"TSG01_01": 1, "TSG01_06": 1} and f["checks"] == {"python": 1, "judge": 1}
    assert Path(r["file"]).is_file() and "F_SUPPLY_CONSIDERATION" in Path(r["file"]).read_text(encoding="utf-8")
    cur.execute("SELECT count(*) FROM fc_index")
    assert cur.fetchone()[0] == 2
    cur.execute("DROP TABLE fc_fact, fc_atom_fact, fc_akp, fc_gkp, fc_catalog, fc_index")
    conn.commit()
    conn.close()
