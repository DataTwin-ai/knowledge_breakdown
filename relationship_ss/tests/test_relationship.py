"""relationship_builder: latest AKP / fact link / GKP / governance master rows -> kp_relation, only new or changed
MKPs on a second run, retire on delete, and the page drawn from kp_relation."""
import pytest

from relationship_ss.build import links

M1, M2 = "CGSTA17_S7_SS1_Ca#TEXT", "CGSTA17_S33#TEXT"
AKP_M1 = [
    {"type": "MKP", "element": "M_SIAFOS", "concept": "NA", "results": "citation_key: CGSTA17_S7_SS1_Ca; frame: INCLUSIVE"},
    {"type": "LE", "element": "LE_SIAFOS", "concept": "NA", "results": 'outcome: SUPPLY_INCLUDED; expr: "G_FORMS AND A_CONSIDERATION"'},
    {"type": "G", "element": "G_FORMS", "concept": "NA", "results": "logic: ANY; members: [A_SALE, A_BARTER]"},
    {"type": "A", "element": "A_SALE", "concept": "Sale", "results": 'statement: "The supply is a sale"; facts: [F_TFOS]'},
    {"type": "A", "element": "A_BARTER", "concept": "Barter", "results": 'statement: "The supply is a barter"; facts: [F_TFOS]'},
    {"type": "A", "element": "A_CONSIDERATION", "concept": "Consideration",
     "results": 'statement: "The supply is for a consideration"; facts: [F_CONSIDERATION]'},
    {"type": "CT", "element": "CT_1", "concept": "NA", "results": 'question: "Is it a gift?"'},
]
AKP_M2 = [
    {"type": "MKP", "element": "M_TAIID", "concept": "NA", "results": "citation_key: CGSTA17_S33; frame: OPERATIVE"},
    {"type": "LE", "element": "LE_1", "concept": "NA", "results": 'outcome: SHOW_TAX; expr: "A_CONSIDERATION"'},
    {"type": "A", "element": "A_CONSIDERATION", "concept": "Consideration",
     "results": 'statement: "The supply is for a consideration"; facts: [F_CONSIDERATION]'},
]
NAMES = {"F_TFOS": "Form of supply", "F_CONSIDERATION": "Consideration exists"}


def rel(rows, relation):
    return {(r["from_id"], r["to_id"]) for r in rows if r["relation"] == relation}


def test_links_of_one_mkp():
    rows = links(M1, AKP_M1, [{"atom": "A_SALE", "fact_id": "F_TFOS"}], [
        {"akp_element": "A_SALE", "tsg_id": "TSG01_01", "tsg_name": "Supply with consideration", "tg_id": "TG01",
         "tg_name": "Supply"}], [{"tsg_id": "TSG01_01", "tsg_name": "Supply with consideration"}], NAMES)
    p = lambda e: f"{M1}::{e}"
    # A_SALE has a fact link; the others fall back to their own facts field
    assert rel(rows, "READS") == {(p("A_SALE"), "F_TFOS"), (p("A_BARTER"), "F_TFOS"), (p("A_CONSIDERATION"), "F_CONSIDERATION")}
    assert rel(rows, "MEMBER_OF") == {(p("A_SALE"), p("G_FORMS")), (p("A_BARTER"), p("G_FORMS"))}
    assert rel(rows, "USED_BY") == {(p("G_FORMS"), p("LE_SIAFOS")), (p("A_CONSIDERATION"), p("LE_SIAFOS"))}
    assert rel(rows, "PART_OF") == {(p("LE_SIAFOS"), M1), (p("CT_1"), M1)}
    assert rel(rows, "USED_IN") == {(M1, "TSG01_01")}
    assert rel(rows, "GROUPED_IN") == {(p("A_SALE"), "TSG01_01")}
    assert rel(rows, "BELONGS_TO") == {("TSG01_01", "TG01")}
    fact = next(r for r in rows if r["to_id"] == "F_TFOS" and r["from_id"] == p("A_SALE"))
    assert fact["to_name"] == "Form of supply" and fact["from_name"] == "The supply is a sale" and fact["link"] == "DIRECT"
    part = next(r for r in rows if r["relation"] == "PART_OF")
    assert part["to_type"] == "MKP" and part["to_name"] == "CGSTA17_S7_SS1_Ca"
    assert {r["link"] for r in rows if r["relation"] == "NEEDS"} == {"DERIVED"}
    assert {r["link"] for r in rows if r["relation"] != "NEEDS"} == {"DIRECT"}


def test_same_atom_and_amendment_links():
    akp = [{"type": "A_REF", "element": "A_X", "concept": "X", "results": "", "same_akp_id": "AKP9"},
           {"type": "LK", "element": "LK_1", "concept": "NA",
            "results": "relation: AMENDS; target: CGSTA17_S7_SS3; kind: SUBSTITUTED"}]
    rows = links(M2, akp, [], [], [], {}, {"AKP9": {"source_mkp": M1, "element": "A_CONSIDERATION", "label": "for consideration"}})
    assert rel(rows, "SAME_AS") == {(f"{M2}::A_X", f"{M1}::A_CONSIDERATION")}
    assert rel(rows, "AMENDS") == {(M2, "CGSTA17_S7_SS3")}


def test_every_type_gets_a_direct_link_to_the_fact():
    akp = [
        {"type": "MKP", "element": "M_SIAFOS", "concept": "NA", "results": "citation_key: CGSTA17_S7_SS1_Ca"},
        {"type": "LE", "element": "LE_SIAFOS", "concept": "NA",
         "results": 'outcome: SUPPLY_INCLUDED; expr: "G_TIMING AND A_CONSIDERATION"; feeds: [PD_SIAFOS]'},
        {"type": "G", "element": "G_TIMING", "concept": "NA", "results": "logic: ANY; members: [A_MADE, A_AGREED]"},
        {"type": "A", "element": "A_MADE", "concept": "Made", "results": 'statement: "made"; facts: [F_SUPPLY_STATUS]'},
        {"type": "A", "element": "A_AGREED", "concept": "Agreed", "results": 'statement: "agreed"; facts: [F_SUPPLY_STATUS]'},
        {"type": "A", "element": "A_CONSIDERATION", "concept": "C", "results": 'statement: "paid"; facts: [F_CONSIDERATION]'},
        {"type": "PD", "element": "PD_SIAFOS", "concept": "NA", "results": 'statement: "included"'},
        {"type": "OR", "element": "OR_SIAFOS", "concept": "NA", "results": "outcome: NOT_COVERED"},
        {"type": "CT", "element": "CT_GIFT", "concept": "NA", "results": 'question: "Gift?"; trigger: A_AGREED'},
        {"type": "TC", "element": "TC1", "concept": "NA", "results": 'case: "made and paid"'},
    ]
    gkp = [{"akp_element": "A_MADE", "tsg_id": "TSG01_01", "tsg_name": "Supply", "tg_id": "TG01", "tg_name": "S"}]
    rows = links(M1, akp, [], gkp, [{"tsg_id": "TSG01_01", "tsg_name": "Supply"}], NAMES)
    short = lambda i: i.split("::", 1)[1] if "::" in i else i
    on_fact = {(r["from_type"], short(r["from_id"])): r for r in rows
               if r["to_id"] == "F_SUPPLY_STATUS" and r["relation"] in ("READS", "NEEDS")}
    assert set(on_fact) == {("ATOM", "A_MADE"), ("ATOM", "A_AGREED"), ("GROUP", "G_TIMING"), ("LE", "LE_SIAFOS"),
                            ("PD", "PD_SIAFOS"), ("OR", "OR_SIAFOS"), ("CT", "CT_GIFT"), ("TC", "TC1"),
                            ("MKP", M1), ("TSG", "TSG01_01"), ("GOVERNANCE_MASTER", "TSG01_01")}
    assert on_fact[("LE", "LE_SIAFOS")]["via"] == "A_AGREED > G_TIMING > LE_SIAFOS"
    assert on_fact[("GROUP", "G_TIMING")]["link"] == "DERIVED"
    assert ("A_CONSIDERATION" not in on_fact[("CT", "CT_GIFT")]["via"])           # CT only through its trigger
    p = lambda e: f"{M1}::{e}"
    assert rel(rows, "OTHERWISE_OF") == {(p("OR_SIAFOS"), p("LE_SIAFOS"))}
    assert rel(rows, "TRIGGERED_BY") == {(p("CT_GIFT"), p("A_AGREED"))}
    assert rel(rows, "TESTS") == {(p("TC1"), p("LE_SIAFOS"))}
    assert rel(rows, "PRODUCES") == {(p("LE_SIAFOS"), p("PD_SIAFOS"))}
    consid = {r["from_type"] for r in rows if r["to_id"] == "F_CONSIDERATION"}
    assert "CT" not in consid and "GROUP" not in consid and "LE" in consid


# ---------------------------------------------------------------- with PostgreSQL
S = "rel_test"


@pytest.fixture()
def db(monkeypatch, tmp_path):
    import ikp_engine as p0
    import kp_settings
    try:
        c = p0.get_connection(kp_settings.build_paths(kp_settings.load()))
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}")
        cur.execute(f"""
        CREATE TABLE {S}.akp (instrument text, version_id text, source_mkp text, ordinal int, type text, element text,
            concept text, results text, akp_version int, is_latest boolean, akp_id text, same_akp_id text);
        CREATE TABLE {S}.akp_atom_fact (source_mkp text, atom text, fact_id text, is_latest boolean);
        CREATE TABLE {S}.akp_fact (fact_id text, name text);
        CREATE TABLE {S}.gkp (source_mkp text, akp_element text, tsg_id text, tsg_name text, tg_id text, tg_name text,
            status text, is_latest boolean);
        CREATE TABLE {S}.governance_master (tsg_id text, tsg_name text, source_mkp text);
        INSERT INTO {S}.akp_fact VALUES ('F_TFOS', 'Form of supply'), ('F_CONSIDERATION', 'Consideration exists');
        INSERT INTO {S}.governance_master VALUES ('TSG01_01', 'Supply with consideration', '{M1}'),
                                                 ('TSG01_01', 'Supply with consideration', '{M2}');
        """)
        for m, rows in ((M1, AKP_M1), (M2, AKP_M2)):
            for i, r in enumerate(rows):
                cur.execute(f"INSERT INTO {S}.akp VALUES ('CGSTA17', 'V_1', %s, %s, %s, %s, %s, %s, 1, true, NULL, NULL)",
                            (m, i, r["type"], r["element"], r["concept"], r["results"]))
        cur.execute(f"INSERT INTO {S}.gkp VALUES (%s, 'A_SALE', 'TSG01_01', 'Supply with consideration', 'TG01', "
                    "'Supply', 'APPROVED', true)", (M1,))
    c.commit()
    import db_insert
    monkeypatch.setattr(db_insert, "get_db_schema", lambda: S)
    monkeypatch.setattr(kp_settings, "require", lambda *a, **k: None)
    cfg = kp_settings.load()
    cfg["relationship"] = {"table": "kp_relation", "page": str(tmp_path / "fact_network.html")}
    cfg["governance_master"] = {**cfg["governance_master"], "tables": {**cfg["governance_master"].get("tables", {}),
                                                                         "fact": "akp_fact"}}
    for k, t in (("akp", "akp"), ("gkp", "gkp")):
        cfg[k] = {**cfg[k], ("table" if k == "akp" else "table_prefix"): t}
    cfg["governance_master"]["table"] = "governance_master"
    yield c, cfg, tmp_path
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
    c.commit()
    c.close()


def q(c, text, *a):
    with c.cursor() as cur:
        cur.execute(text, a)
        return cur.fetchall()


def test_builder_reads_only_new_or_changed_mkps(db):
    import relationship_builder as rb
    c, cfg, tmp = db
    r = rb.run(conn=c, cfg=cfg)
    assert (r["new"], r["changed"], r["same"]) == (2, 0, 0)
    shared = q(c, f"SELECT DISTINCT mkp FROM {S}.kp_relation WHERE relation='READS' AND to_id='F_CONSIDERATION' "
                  "AND is_latest")
    assert set(shared) == {(M1,), (M2,)}                                             # one fact joins two laws
    page = (tmp / "fact_network.html").read_text(encoding="utf-8")
    assert "F_CONSIDERATION" in page and "__DATA__" not in page

    r = rb.run(conn=c, cfg=cfg)                                                       # nothing changed: nothing read
    assert (r["new"], r["changed"], r["same"], r["rows"]) == (0, 0, 2, 0)

    with c.cursor() as cur:                                                           # AKP of M2 made again
        cur.execute(f"UPDATE {S}.akp SET is_latest=false WHERE source_mkp=%s", (M2,))
        cur.execute(f"INSERT INTO {S}.akp VALUES ('CGSTA17','V_1',%s,0,'LE','LE_1','NA',"
                    "'outcome: X; expr: \"A_NEW\"',2,true,NULL,NULL), ('CGSTA17','V_1',%s,1,'A','A_NEW','New',"
                    "'statement: \"new\"; facts: [F_TFOS]',2,true,NULL,NULL)", (M2, M2))
    c.commit()
    r = rb.run(conn=c, cfg=cfg)
    assert (r["new"], r["changed"], r["same"]) == (0, 1, 1)
    assert q(c, f"SELECT max(version), bool_and(akp_version = 2) FROM {S}.kp_relation WHERE mkp=%s AND is_latest", M2) == [(2, True)]
    assert q(c, f"SELECT count(*) FROM {S}.kp_relation WHERE mkp=%s AND NOT is_latest", M2)[0][0] > 0   # kept

    with c.cursor() as cur:                                                           # M2 deleted from the database
        cur.execute(f"DELETE FROM {S}.akp WHERE source_mkp=%s", (M2,))
    c.commit()
    r = rb.run(conn=c, cfg=cfg)
    assert r["removed"] == 1
    assert q(c, f"SELECT count(*) FROM {S}.kp_relation WHERE mkp=%s AND is_latest", M2) == [(0,)]


def test_old_kp_relation_is_upgraded_and_rebuilt(db):
    """A kp_relation made by the first builder (molecule, from_label, to_label, '/' ids) is upgraded in place."""
    import relationship_builder as rb
    c, cfg, tmp = db
    with c.cursor() as cur:
        cur.execute(f"""CREATE TABLE {S}.kp_relation (molecule text NOT NULL, version_id text, instrument text,
            version integer NOT NULL, akp_version integer, fingerprint text NOT NULL, from_type text NOT NULL,
            from_id text NOT NULL, from_label text, relation text NOT NULL, to_type text NOT NULL, to_id text NOT NULL,
            to_label text, is_latest boolean NOT NULL DEFAULT true, inserted_at timestamptz NOT NULL DEFAULT now())""")
        cur.execute(f"INSERT INTO {S}.kp_relation (molecule, version, fingerprint, from_type, from_id, relation, to_type, to_id) "
                    "VALUES (%s, 1, 'old', 'ATOM', %s, 'READS', 'FACT', 'F_TFOS')", (M1, M1 + "/A_SALE"))
    c.commit()
    r = rb.run(conn=c, cfg=cfg)
    assert (r["new"], r["changed"]) == (1, 1)                    # M1 had old rows: rebuilt; M2 new
    assert q(c, f"SELECT count(*) FROM {S}.kp_relation WHERE is_latest AND from_id LIKE '%%/%%'") == [(0,)]
    assert q(c, f"SELECT DISTINCT link FROM {S}.kp_relation WHERE relation = 'NEEDS' AND is_latest") == [("DERIVED",)]
    cols = {x[0] for x in q(c, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                               "AND table_name='kp_relation'", S)}
    assert {"mkp", "from_name", "to_name", "link", "via", "rel_id"} <= cols and "molecule" not in cols
