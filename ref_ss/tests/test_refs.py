"""Cross references, permission access, error log, LLM runtime helpers (no database unless said).
Run: python -m pytest -q ref_ss/tests
"""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import kp_errors  # noqa: E402
import kp_settings  # noqa: E402
from ref_ss.detect import detect, labels  # noqa: E402
from ref_ss.ids import parse, ref_id_for_document  # noqa: E402


@pytest.mark.parametrize("text,want", [
    ("Notification No. 4/2018-Central Tax (Rate), dated 25.01.2018", "N_CTR_04_2018"),
    ("notfn 04/2018-CT(R)", "N_CTR_04_2018"),
    ("N.4/2018-Central Tax (Rate)", "N_CTR_04_2018"),
    ("Notification No. 4/2018 – Central Tax", "N_CT_04_2018"),
    ("vide notification No. 10/2017-Integrated Tax (Rate)", "N_ITR_10_2017"),
    ("Circular No. 47/21/2018-GST", "CIR_47_2018"),
    ("Inserted by The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)", "ACT_31_2018"),
    ("Ins. by Act 31 of 2018, s. 2", "ACT_31_2018"),
])
def test_every_spelling_gets_one_fixed_id(text, want):
    assert parse(text)[0] == want


def test_document_codes_meet_the_reference_ids():
    assert ref_id_for_document("Notification 4/2018-Central Tax (Rate)", "NOTIFICATION", "N04_2018") == "N_CTR_04_2018"
    assert ref_id_for_document("Circular 47/21/2018 - GST", "CIRCULAR", "C47_21_2018") == "CIR_47_2018"
    assert ref_id_for_document("CGST Act", "ACT", "CGSTA17") == "CGSTA17"


def test_self_cross_and_amending_references():
    xrefs = [{"cue": "Schedule II", "to": "CGSTA17_SCHII", "resolved": True},
             {"cue": "sub-section (9)", "to": None, "resolved": False}]
    refs = detect("CGSTA17", "as referred to in Schedule II and notified under notification No. 4/2018-Central Tax "
                             "(Rate)", ["Inserted w.e.f. 1-7-2017 by the CGST (Amendment) Act, 2018 (No. 31 of 2018)"],
                  xrefs)
    by = {(r["scope"], r["ref_id"]): r for r in refs}
    assert ("SELF", "CGSTA17") in by and by[("SELF", "CGSTA17")]["target_key"] == "CGSTA17_SCHII"
    assert by[("CROSS", "N_CTR_04_2018")]["amending"] is False
    assert by[("CROSS", "ACT_31_2018")]["amending"] is True
    assert labels(refs, "SELF") == ["CGSTA17_SCHII"]
    assert set(labels(refs, "CROSS")) == {"N_CTR_04_2018", "ACT_31_2018"}


def test_rules_refer_to_the_act_and_other_documents():
    refs = detect("CGSTR17", "the value shall be determined under section 15 of the Act and rule 27 of the Central "
                             "Goods and Services Tax Rules, 2017", act_code="CGSTA17")
    keys = {(r["ref_id"], r["target_key"]) for r in refs}
    assert ("CGSTA17", "CGSTA17_S15") in keys


def test_permission_access_and_new_blocks():
    cfg = kp_settings.load()
    assert cfg["workers"]["count"] >= 1 and set(cfg["workers"]["max_calls_at_once"]) == {"GEMINI", "CLAUDE"}
    assert cfg["features"]["web_scraping"] in (True, False)
    assert kp_settings.allowed(cfg, "READ", ROOT / "Law" / "Acts" / "x.pdf")
    assert kp_settings.allowed(cfg, "WRITE", ROOT / "Output" / "x.csv")
    assert not kp_settings.allowed(cfg, "WRITE", ROOT / "Prompt" / "x.txt")
    c2 = copy.deepcopy(cfg)
    c2["access"]["internet"] = False
    with pytest.raises(kp_settings.AccessDenied, match="access.internet"):
        kp_settings.require(c2, "INTERNET")


def test_missing_permission_key_stops_with_its_name():
    perm = __import__("json").loads((ROOT / "permission.json").read_text(encoding="utf-8"))
    del perm["features"]["web_scraping"]
    with pytest.raises(kp_settings.SettingsError, match="web_scraping"):
        kp_settings.load(permission=perm, settings=__import__("json").loads(
            (ROOT / "Config" / "settings.json").read_text(encoding="utf-8")))


def test_error_log_last_state_per_unit(tmp_path):
    kp_errors.record(tmp_path, "cgsta17", "CGSTA17_S7", "MKP", RuntimeError("x"), "W1")
    kp_errors.record(tmp_path, "CGSTA17", "CGSTA17_S9", "AKP", "comparison FALSE", "W2")
    kp_errors.done(tmp_path, "CGSTA17", "CGSTA17_S7", "W3")
    assert kp_errors.failed_units(tmp_path, "CGSTA17") == ["CGSTA17_S9"]
    assert (tmp_path / "CGSTA17" / "error_log.csv").read_text(encoding="utf-8-sig").startswith("time,document,unit")


def test_prompt_cache_marker_never_reaches_a_model():
    pytest.importorskip("anthropic")
    import llm_runtime as L
    head, tail = L.split_prompt("RULES" + L.CACHE_BREAK + "INPUT")
    assert (head, tail) == ("RULES", "INPUT") and L.CACHE_BREAK not in L.plain_prompt("A" + L.CACHE_BREAK + "B")


def test_too_many_requests_waits_then_raises_rate_limited(monkeypatch):
    pytest.importorskip("anthropic")
    import llm_runtime as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    monkeypatch.setenv("MAX_RATE_LIMIT_RETRIES", "2")
    calls = []

    def boom(client, model, prompt):
        calls.append(1)
        raise RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(L, "call_claude", boom)
    with pytest.raises(L.RateLimited):
        L.call_llm({"provider": "CLAUDE", "model": "m", "client": None}, "p")
    assert len(calls) == 3


# ---------------------------------------------------------------- database
@pytest.fixture()
def conn():
    import ikp_engine
    try:
        c = ikp_engine.get_connection(kp_settings.build_paths())
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    yield c
    c.close()


def test_reference_status_rules(conn):
    from ref_ss.store import RefStore
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.t_ref_doc; DROP TABLE IF EXISTS public.t_ref_link")
    conn.commit()
    st = RefStore(conn, "public", "t_ref_doc", "t_ref_link")
    st.ensure()
    amend = {"ref_id": "ACT_31_2018", "scope": "CROSS", "kind": "AMENDING_ACT", "mention": "Act 31 of 2018",
             "relation": "AMENDED_BY", "target_key": "", "where": "FOOTNOTE", "amending": True}
    notif = {"ref_id": "N_CTR_04_2018", "scope": "CROSS", "kind": "NOTIFICATION", "mention": "Notification 4/2018",
             "relation": "REFERS", "target_key": "", "where": "TEXT", "amending": False, "number": 4, "year": 2018}
    held_doc = {"ref_id": "CGSTR17", "scope": "CROSS", "kind": "RULES", "mention": "rule 27", "relation": "REFERS",
                "target_key": "CGSTR17_R27", "where": "TEXT", "amending": False}
    with conn.cursor() as cur:
        st.save(cur, "CGSTA17", "V1", "CGSTA17_S15", {"CGSTA17_S15_SS1#TEXT": [amend, notif, held_doc]},
                {"CGSTR17": "CGSTR17"}, push_amending=False)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT ref_id, status FROM t_ref_doc ORDER BY 1")      # amending Acts are not skipped any more
        assert dict(cur.fetchall()) == {"ACT_31_2018": "PENDING", "CGSTR17": "INSERTED", "N_CTR_04_2018": "PENDING"}
    assert sorted(p["ref_id"] for p in st.pending()) == ["ACT_31_2018", "N_CTR_04_2018"]
    st.mark_inserted("N_CTR_04_2018", "N04_2018")
    # the status of each law: CGSTR17 file is held, but rule 27 has no MKP yet -> PENDING; the notification has one
    with conn.cursor() as cur:
        cur.execute("""DROP TABLE IF EXISTS public.t_ref_mkp;
            CREATE TABLE public.t_ref_mkp (type text, element text, is_latest boolean, instrument_code text);
            INSERT INTO public.t_ref_mkp VALUES ('MKP', 'N04_2018_P1#TEXT', true, 'N04_2018'),
                                                ('MKP', 'CGSTR17_R2#TEXT', true, 'CGSTR17')""")
    conn.commit()
    assert st.update_target_status("t_ref_mkp") == 3
    got = {r["ref_id"]: (r["status"], r["doc_status"]) for r in st.for_mkp("V1", "CGSTA17_S15_SS1#TEXT")}
    assert got == {"N_CTR_04_2018": ("INSERTED", "INSERTED"), "CGSTR17": ("PENDING", "INSERTED"),
                   "ACT_31_2018": ("PENDING", "PENDING")}
    with conn.cursor() as cur:                                            # rule 27 is made -> INSERTED
        cur.execute("INSERT INTO public.t_ref_mkp VALUES ('MKP', 'CGSTR17_R27_SR1#TEXT', true, 'CGSTR17')")
    conn.commit()
    st.update_target_status("t_ref_mkp")
    assert {r["ref_id"]: r["status"] for r in st.for_mkp("V1", "CGSTA17_S15_SS1#TEXT")}["CGSTR17"] == "INSERTED"
    with conn.cursor() as cur:
        cur.execute("DROP TABLE public.t_ref_doc; DROP TABLE public.t_ref_link; DROP TABLE public.t_ref_mkp")
    conn.commit()


def test_delete_cascade_removes_everything_made_from_an_mkp(conn):
    import db_migrate as M
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS cas CASCADE; CREATE SCHEMA cas")
    cur.execute("""CREATE TABLE cas.m (cluster text, type text, element text, results text, version_id text, instrument_code text);
    CREATE TABLE cas.a (version_id text, source_mkp text);
    CREATE TABLE cas.g (version_id text, source_mkp text);
    CREATE TABLE cas.m_llm_cache (cache_key text, unit_key text);
    CREATE TABLE cas.r (version_id text, mkp_element text);
    CREATE TABLE cas.n_nodes (doc text, element text, is_latest boolean);
    INSERT INTO cas.m VALUES ('C','MKP','D_S7_SS1#TEXT','','V1','D'),('C','SP','D_S7_SS1#TEXT','','V1','D'),
                             ('C','MKP','D_S8#TEXT','','V1','D');
    INSERT INTO cas.a VALUES ('V1','D_S7_SS1#TEXT'),('V1','D_S8#TEXT');
    INSERT INTO cas.g VALUES ('V1','D_S7_SS1#TEXT');
    INSERT INTO cas.m_llm_cache VALUES ('k1','D_S7'),('k2','D_S8');
    INSERT INTO cas.r VALUES ('V1','D_S7_SS1#TEXT');
    INSERT INTO cas.n_nodes VALUES ('D','D_S8',true);""")
    conn.commit()
    names = {"mkp": "m", "akp": "a", "akp_cache": "a_cache", "akp_llm": "a_llm", "fact_edge": "fe", "time_fact": "tf",
             "gkp": "g", "gkp_cache": "g_cache", "gkp_llm": "g_llm", "gm": "gm", "ref_link": "r", "ikp_nodes": "n_nodes"}
    assert M.install_cascade(conn, "cas", names)
    cur.execute("DELETE FROM cas.m WHERE element='D_S7_SS1#TEXT' AND type='MKP'")
    conn.commit()
    count = lambda t: (cur.execute(f"SELECT count(*) FROM cas.{t}"), cur.fetchone()[0])[1]
    assert (count("m"), count("a"), count("g"), count("m_llm_cache"), count("r")) == (1, 1, 0, 1, 0)
    cur.execute("DELETE FROM cas.n_nodes WHERE element='D_S8'")                   # IKP node -> its MKPs -> AKP
    conn.commit()
    assert (count("m"), count("a"), count("m_llm_cache")) == (0, 0, 0)
    cur.execute("DROP SCHEMA cas CASCADE")
    conn.commit()


def test_too_many_requests_lowers_the_calls_at_once_then_raises_it_back(monkeypatch):
    pytest.importorskip("anthropic")
    import llm_runtime as L
    g = L.AdaptiveGate("GEMINI", 8)
    g.too_many()
    assert g.cap == 6                       # 8 - 8//4
    g.too_many()
    assert g.cap == 6                       # not again within 10 s
    g.last_down -= 11
    g.too_many()
    assert g.cap == 5
    g.fine()
    assert g.cap == 5                       # not quiet for a minute yet
    g.last_429 -= 61
    g.fine()
    assert g.cap == 6
    for _ in range(10):
        g.last_down -= 11
        g.too_many()
    assert g.cap == 2                       # never below 2


def test_call_llm_slows_every_worker_on_429(monkeypatch):
    pytest.importorskip("anthropic")
    import llm_runtime as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    L.configure({"GEMINI": 8})
    hits = []

    def flaky(client, model, prompt):
        if len(hits) < 2:
            hits.append(1)
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"
    monkeypatch.setattr(L, "call_gemini", flaky)
    assert L.call_llm({"provider": "GEMINI", "model": "m", "client": None}, "p")[0] == "ok"
    assert L.calls_at_once()["GEMINI"] == 6


def test_too_many_requests_lowers_calls_at_once_then_it_comes_back():
    pytest.importorskip("anthropic")
    import llm_runtime as L
    g = L.AdaptiveGate("GEMINI", 8)
    g.too_many()
    assert g.cap == 6
    g.too_many()
    assert g.cap == 6                       # at most once every 10 s
    for _ in range(10):
        g.last_down -= 11
        g.too_many()
    assert g.cap == 2                       # never below 2
    g.last_429 -= 61
    g.last_up -= 31
    g.fine()
    assert g.cap == 3                       # a quiet minute: one more call at once


def test_jobs_sheet_has_the_reason_of_a_failed_law(tmp_path):
    import openpyxl
    import kp_usage
    z = {s: kp_usage._zero() for s in kp_usage.STEPS}
    rows = [{"document": "CGSTA17", "unit": "CGSTA17_S7_SS1_Ca", "worker": "W1", "status": "DONE", "seconds": 1,
             "usage": z, "label": "Clause (a)", "stage": "", "error": ""},
            {"document": "CGSTA17", "unit": "CGSTA17_S7_SS3", "worker": "W2", "status": "FAILED", "seconds": 1,
             "usage": z, "label": "Sub Section (3)", "stage": "AKP", "error": "comparison FALSE"}]
    run = {"run_id": "r", "started": "r", "documents": "CGSTA17", "scope": "s", "workers": 8, "jobs": 2, "done": 1,
           "failed": 1, "seconds": 1}
    p = kp_usage.save_excel(tmp_path / "usage.xlsx", run, rows)
    ws = openpyxl.load_workbook(p)["Jobs"]
    head = [c.value for c in ws[1]]
    r2, r3 = [c.value for c in ws[2]], [c.value for c in ws[3]]
    i = head.index("Reason")
    assert r2[head.index("Status")] == "DONE" and r2[i] is None
    assert r3[head.index("Status")] == "FAILED" and r3[i] == "comparison FALSE" and r3[head.index("Failed at")] == "AKP"


FALLBACK = {"enabled": True, "CLAUDE": {"claude-opus-5-5": "gemini-2.5-pro", "claude-sonnet-5-5": "gemini-2.5-pro"},
            "second_opinion": {"gemini-2.5-pro": "gemini-2.5-flash", "gemini-2.5-flash": "gemini-2.5-pro"}}


def test_route_moves_claude_roles_to_gemini_only_when_claude_is_down():
    import llm_fallback as F
    tier = {"generator_1": {"llm": "GEMINI", "model": "gemini-2.5-pro"},
            "generator_2": {"llm": "CLAUDE", "model": "claude-sonnet-5-5"},
            "judge": {"llm": "CLAUDE", "model": "claude-opus-5-5"}}
    F.configure(FALLBACK)
    assert F.route(tier) is tier                                    # Claude works: nothing changes
    F.mark_down("credit balance is too low")
    r = F.route(tier)
    assert r["judge"] == {"llm": "GEMINI", "model": "gemini-2.5-pro"}
    assert r["generator_1"]["model"] == "gemini-2.5-pro"
    assert r["generator_2"] == {"llm": "GEMINI", "model": "gemini-2.5-flash"}   # still two different opinions
    simple = {**tier, "generator_1": {"llm": "GEMINI", "model": "gemini-2.5-flash"}}
    assert F.route(simple)["generator_2"]["model"] == "gemini-2.5-pro"
    F.configure(None)


def test_claude_credit_error_is_answered_by_gemini(monkeypatch):
    pytest.importorskip("anthropic")
    import llm_fallback as F
    import llm_runtime as L
    calls = []

    def claude(client, model, prompt):
        calls.append(("CLAUDE", model))
        raise RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API")

    def gemini(client, model, prompt):
        calls.append(("GEMINI", model))
        return "answer"
    monkeypatch.setattr(L, "call_claude", claude)
    monkeypatch.setattr(L, "call_gemini", gemini)
    monkeypatch.setattr(L, "create_gemini_client", lambda paths: object())
    rt = {"provider": "CLAUDE", "model": "claude-sonnet-5-5", "client": object(), "paths": None}
    L.configure(None, True, FALLBACK)
    assert L.call_llm(rt, "q")[0] == "answer" and F.claude_down()
    assert L.call_llm(dict(rt, model="claude-opus-5-5"), "q")[0] == "answer"      # no Claude try any more
    assert calls == [("CLAUDE", "claude-sonnet-5-5"), ("GEMINI", "gemini-2.5-pro"), ("GEMINI", "gemini-2.5-pro")]
    L.configure(None, True, {**FALLBACK, "enabled": False})                     # fallback off: the run stops as before
    with pytest.raises(RuntimeError, match="CLAUDE call failed"):
        L.call_llm(rt, "q")
    L.configure(None, True, None)


def test_amending_act_check_and_folder(monkeypatch, tmp_path):
    """Amending Acts are fetched too (Law/Amendments) and checked by 'No. 31 of 2018' (notes section 13)."""
    from ref_ss import fetch
    import ref_engine
    monkeypatch.setattr(fetch, "first_pages_text", lambda path, pages=2: "THE CGST (AMENDMENT) ACT, 2018 NO. 31 OF 2018")
    assert fetch.verify_pdf(tmp_path / "x.pdf", 31, 2018, "AMENDING_ACT")[0]
    assert not fetch.verify_pdf(tmp_path / "x.pdf", 32, 2018, "AMENDING_ACT")[0]
    assert ref_engine._folder_type({"kind": "AMENDING_ACT"}) == ("ACT", "AMENDMENT")
    assert ref_engine._folder_type({"kind": "CIRCULAR"}) == ("CIRCULAR", "CIRCULAR")
    assert ref_engine._check_content({}, {"ref_id": "ACT_31_2018"}, "text", []) == (True, "no citing law text to compare with")
