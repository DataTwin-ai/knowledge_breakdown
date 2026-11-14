"""A new PDF of a document: unchanged laws are copied to the new version, changed / removed ones are not, and the old
version's rows are no longer is_latest (mkp_ss/carry.py)."""
import pytest

from mkp_ss import carry as carry_mod

S = "carry_test"
NAMES = {"akp": "akp", "fact_edge": "akp_atom_fact", "time_fact": "akp_time_fact", "gkp": "gkp", "ref_link": "mkp_ref"}
OLD, NEW = "V_AAAAAAAAAAAAAAAA", "V_BBBBBBBBBBBBBBBB"


@pytest.fixture()
def conn():
    import ikp_engine as p0
    import kp_settings
    try:
        c = p0.get_connection(kp_settings.build_paths(kp_settings.load()))
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}")
        cur.execute(f"""
        CREATE TABLE {S}.ikp_versions (doc text, version_no int, pdf_sha256 text, is_latest boolean);
        CREATE TABLE {S}.ikp_nodes (doc text, version_no int, element text, status text, heading text, own_text text,
            amendments jsonb, PRIMARY KEY (doc, version_no, element));
        CREATE TABLE {S}.mkp (cluster text, type text, element text, results text, version_id text, run_id text,
            inserted_at timestamptz DEFAULT now(), source_version text, mkp_version int DEFAULT 1,
            is_latest boolean DEFAULT true);
        CREATE TABLE {S}.akp (instrument text, version_id text, source_mkp text, akp_version int, ordinal int,
            type text, results text, is_latest boolean DEFAULT true, created_at timestamptz DEFAULT now(),
            PRIMARY KEY (instrument, version_id, source_mkp, akp_version, ordinal));
        CREATE TABLE {S}.akp_atom_fact (instrument text, version_id text, source_mkp text, atom text, fact_id text,
            is_latest boolean DEFAULT true);
        CREATE TABLE {S}.akp_time_fact (instrument text, version_id text, amendment_mkp text, kind text);
        CREATE TABLE {S}.gkp (instrument text, version_id text, source_mkp text, tsg_id text, is_latest boolean DEFAULT true);
        CREATE TABLE {S}.mkp_ref (version_id text, mkp_element text, ref_id text);
        INSERT INTO {S}.ikp_versions VALUES ('CGSTA17', 1, 'aaaaaaaaaaaaaaaa11', false), ('CGSTA17', 2, 'bbbbbbbbbbbbbbbb22', true);
        """)
        for v, rows in ((1, [("CGSTA17_S7", "same"), ("CGSTA17_S9", "old words"), ("CGSTA17_S10", "gone later")]),
                        (2, [("CGSTA17_S7", "same"), ("CGSTA17_S9", "new words"), ("CGSTA17_S11", "new law")])):
            for el, txt in rows:
                cur.execute(f"INSERT INTO {S}.ikp_nodes VALUES ('CGSTA17', %s, %s, 'ACTIVE', 'h', %s, '[]')", (v, el, txt))
        for el, cl in (("CGSTA17_S7#TEXT", "C_CGSTA17_S7"), ("CGSTA17_S7#AMD1", "C_CGSTA17_S7"),
                       ("CGSTA17_S9#TEXT", "C_CGSTA17_S9"), ("CGSTA17_S10#TEXT", "C_CGSTA17_S10")):
            cur.execute(f"INSERT INTO {S}.mkp (cluster, type, element, results, version_id, source_version) "
                        "VALUES (%s, 'MKP', %s, 'text: x', %s, '1'), (%s, 'SP', %s, 'text: x', %s, '1')",
                        (cl, el, OLD, cl, el, OLD))
            cur.execute(f"INSERT INTO {S}.akp VALUES ('CGSTA17', %s, %s, 1, 1, 'A', 'a')", (OLD, el))
            cur.execute(f"INSERT INTO {S}.akp_atom_fact VALUES ('CGSTA17', %s, %s, 'A_X', 'F_X')", (OLD, el))
            cur.execute(f"INSERT INTO {S}.gkp VALUES ('CGSTA17', %s, %s, 'TSG01_01')", (OLD, el))
            cur.execute(f"INSERT INTO {S}.mkp_ref VALUES (%s, %s, 'ACT_31_2018')", (OLD, el))
        for cl in ("C_CGSTA17_S7", "C_CGSTA17_S9"):
            cur.execute(f"INSERT INTO {S}.mkp (cluster, type, element, results, version_id) VALUES (%s, 'BATCH', 'DOC', 'q', %s)",
                        (cl, OLD))
        cur.execute(f"INSERT INTO {S}.akp_time_fact VALUES ('CGSTA17', %s, 'CGSTA17_S7#AMD1', 'INSERTED')", (OLD,))
    c.commit()
    yield c
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
    c.commit()
    c.close()


def _q(conn, text, *a):
    with conn.cursor() as cur:
        cur.execute(text, a)
        return cur.fetchall()


def test_new_pdf_copies_only_unchanged_laws_and_retires_the_old_version(conn):
    carry_mod._done.clear()
    lines = []
    r = carry_mod.carry_forward(conn, "CGSTA17", NEW, 2, "RUN1", S, "mkp", "ikp", NAMES, say=lines.append)
    assert r == {"old": OLD, "carried": 2, "removed": 1, "redo": 1}
    new_mkps = {e for (e,) in _q(conn, f"SELECT element FROM {S}.mkp WHERE version_id=%s AND is_latest AND type='MKP'", NEW)}
    assert new_mkps == {"CGSTA17_S7#TEXT", "CGSTA17_S7#AMD1"}                 # S9 changed, S10 removed: not copied
    assert _q(conn, f"SELECT count(*) FROM {S}.mkp WHERE version_id=%s AND type='SP'", NEW) == [(2,)]
    assert _q(conn, f"SELECT cluster FROM {S}.mkp WHERE version_id=%s AND type='BATCH'", NEW) == [("C_CGSTA17_S7",)]
    assert _q(conn, f"SELECT DISTINCT run_id, source_version FROM {S}.mkp WHERE version_id=%s", NEW) == [("RUN1", "2")]
    for t, col in (("akp", "source_mkp"), ("akp_atom_fact", "source_mkp"), ("gkp", "source_mkp"), ("mkp_ref", "mkp_element")):
        assert {e for (e,) in _q(conn, f"SELECT {col} FROM {S}.{t} WHERE version_id=%s", NEW)} == new_mkps, t
    assert _q(conn, f"SELECT amendment_mkp FROM {S}.akp_time_fact WHERE version_id=%s", NEW) == [("CGSTA17_S7#AMD1",)]
    for t in ("mkp", "akp", "akp_atom_fact", "gkp"):                          # old rows kept, but not latest
        assert _q(conn, f"SELECT count(*) FROM {S}.{t} WHERE version_id=%s AND is_latest", OLD) == [(0,)], t
        assert _q(conn, f"SELECT count(*) FROM {S}.{t} WHERE version_id=%s", OLD)[0][0] > 0, t
    assert any("2 law(s) unchanged" in x for x in lines)
    # a second start of the same run does nothing
    assert carry_mod.carry_forward(conn, "CGSTA17", NEW, 2, "RUN1", S, "mkp", "ikp", NAMES, say=lines.append) is None


def test_same_pdf_again_does_nothing(conn):
    carry_mod._done.clear()
    assert carry_mod.carry_forward(conn, "CGSTA17", OLD, 1, "RUN1", S, "mkp", "ikp", NAMES, say=print) is None
    assert _q(conn, f"SELECT count(*) FROM {S}.mkp WHERE version_id=%s AND is_latest", OLD) == [(10,)]
