"""main.py references step for Acts / Rules (2026-11-25) - fakes only: no database, no model, no PDF read.

Act s.16(2)(aa) "as may be prescribed" -> the Rules (a PDF already in Law/) -> exact part CGSTR17_R36_SR4 runs
MKP -> AKP -> GKP (own TSG); a notification is only stored; the parts made then get their own references (depth 2).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("psycopg2")

import main  # noqa: E402
from ref_ss import auto  # noqa: E402
from ref_ss.tests.test_ref_rules import Rules  # noqa: E402


class FakeConn:
    def close(self):
        pass

    def commit(self):
        pass


def _row(mkp, ref_id, kind, relation="REFERS", target="", source=""):
    return {"instrument": mkp.split("_")[0], "version_id": "v1", "mkp_element": mkp + "#TEXT", "scope": "CROSS",
            "ref_id": ref_id, "target_key": target, "relation": relation, "mention": "as may be prescribed",
            "target_status": "PENDING", "source_key": source, "kind": kind, "doc_status": "", "doc_code": "",
            "title": ""}


@pytest.fixture
def world(monkeypatch, tmp_path):
    law = tmp_path / "Law"
    (law / "Rules").mkdir(parents=True)
    (law / "Rules" / "r.pdf").write_bytes(b"%PDF")
    cfg = {"refs": {"auto_fetch": False, "auto_fetch_limit": 5, "self_depth": 1, "self_limit": 20,
                    "whole_document_max_parts": 40, "parts_view": "v", "ikp_kinds": ["ACT", "RULES", "OTHER_ACT"],
                    "depth": 2, "cross_limit": 20, "link_table": "mkp_ref", "document_table": "ref_document"},
           "documents": {"CGSTR17": {"code": "CGSTR17", "ref_id": "CGSTR17", "file": "Rules/r.pdf", "type": "RULES",
                                     "name": "CGST Rules"}},
           "akp": {"table": "akp"}, "features": {}, "ikp": {}, "paths": {"prompt_dir": str(tmp_path)},
           "files": {"ref_part_prompt": "p.txt"}, "workers": {"count": 1}}
    (tmp_path / "p.txt").write_text("P", encoding="utf-8")
    state = {"rows": [], "jobs": [], "status": {}, "source": {}, "made": [], "done": set()}
    paths = {"law_dir": law}
    import ikp_engine
    import kp_settings
    import mkp_engine
    import kp_pool
    from ref_ss import store as ref_store
    monkeypatch.setattr(ikp_engine, "get_connection", lambda p: FakeConn())
    monkeypatch.setattr(ikp_engine, "run_ikp_pipeline", lambda *a, **k: None)
    monkeypatch.setattr(ikp_engine, "validate_ikp_settings", lambda s: s)
    monkeypatch.setattr(kp_settings, "load", lambda fresh=False: cfg)
    monkeypatch.setattr(kp_settings, "resolve", lambda p: Path(p))
    monkeypatch.setattr(kp_settings, "allowed", lambda *a: False)
    def model(prompt):                    # step 1 (rule headings) -> the rule; step 2 (its parts) -> the sub-rule
        heads = '"CGSTR17_R36_SR1"' not in prompt
        return '{"keys": ["CGSTR17_R36' + ('"' if heads else '_SR4"') + '], "reason": "r"}'
    monkeypatch.setattr(main, "_ref_model", lambda c: model)
    monkeypatch.setattr(mkp_engine, "MkpRun", lambda *a, **k: SimpleNamespace(idx=Rules()))
    import db_insert
    monkeypatch.setattr(db_insert, "get_db_schema", lambda: "public")
    monkeypatch.setattr(auto, "pending_refs", lambda conn, s, l, d, mkps: [
        r for r in state["rows"] if mkps is None or auto.unit(r["mkp_element"]) in {auto.unit(m) for m in mkps}])
    monkeypatch.setattr(auto, "set_status", lambda c, s, l, rows, st: state["status"].update(
        {(r["mkp_element"], r["ref_id"]): st for r in rows}))
    monkeypatch.setattr(auto, "set_source_key", lambda c, s, l, r, keys: state["source"].update(
        {r["mkp_element"]: keys}))
    monkeypatch.setattr(auto, "part_done", lambda c, s, a, code, keys, idx=None: set(keys) <= state["done"])

    class Pool:
        def __init__(self, *a):
            self.mine = []

        def add(self, jobs):
            self.mine += jobs

        def start(self):
            state["jobs"].append(list(self.mine))
            made = state["made"].pop(0) if state["made"] else []
            return {"CGSTR17": SimpleNamespace(source_mkps=made)}
    monkeypatch.setattr(kp_pool, "Pool", Pool)
    monkeypatch.setattr(ref_store.RefStore, "update_target_status", lambda self, a, v="": 0)
    return cfg, paths, state


def test_rules_part_runs_with_its_own_gkp_and_the_notification_is_only_stored(world):
    cfg, paths, state = world
    state["rows"] = [_row("CGSTA17_S16_SS2_Caa", "CGSTR17", "RULES", "PRESCRIBED_BY"),
                     _row("CGSTA17_S9_SS3", "N_CTR_13_2017", "NOTIFICATION")]
    totals = {"CGSTA17": SimpleNamespace(source_mkps=["CGSTA17_S16_SS2_Caa#TEXT", "CGSTA17_S9_SS3#TEXT"])}
    main._references(cfg, paths, {}, {}, totals)
    jobs = state["jobs"][0]
    assert [j.batch.root for j in jobs] == ["CGSTR17_R36_SR4"]
    assert jobs[0].no_gkp is False and jobs[0].attach_to == "CGSTA17_S16_SS2_Caa#TEXT"
    assert state["source"]["CGSTA17_S16_SS2_Caa#TEXT"] == ["CGSTR17_R36_SR4"]
    assert ("CGSTA17_S9_SS3#TEXT", "N_CTR_13_2017") not in state["status"]           # stored, never queued


def test_a_part_already_inserted_is_only_linked_and_one_part_runs_once(world):
    cfg, paths, state = world
    state["rows"] = [_row("CGSTA17_S16_SS2_Caa", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R36_SR4"),
                     _row("CGSTA17_S16_SS2_Cb", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R36_SR4"),
                     _row("CGSTA17_S17_SS2", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R42")]
    state["done"] = {"CGSTR17_R42"}
    totals = {"CGSTA17": SimpleNamespace(source_mkps=["CGSTA17_S16_SS2_Caa", "CGSTA17_S16_SS2_Cb", "CGSTA17_S17_SS2"])}
    main._references(cfg, paths, {}, {}, totals)
    assert [j.batch.root for j in state["jobs"][0]] == ["CGSTR17_R36_SR4"]              # once for both laws
    assert state["status"][("CGSTA17_S17_SS2#TEXT", "CGSTR17")] == "INSERTED"


def test_the_saved_wider_part_gives_way_and_the_limit_waits(world):
    cfg, paths, state = world
    cfg["refs"]["cross_limit"] = 1
    state["rows"] = [_row("CGSTA17_S16_SS2_Caa", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R36_SR4",
                          source="CGSTR17_R36"),
                     _row("CGSTA17_S17_SS2", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R42")]
    totals = {"CGSTA17": SimpleNamespace(source_mkps=["CGSTA17_S16_SS2_Caa", "CGSTA17_S17_SS2"])}
    main._references(cfg, paths, {}, {}, totals)
    assert [j.batch.root for j in state["jobs"][0]] == ["CGSTR17_R36_SR4"]              # R42 waits (limit 1)


def test_depth_two_runs_the_references_of_the_parts_just_made_then_stops(world):
    cfg, paths, state = world
    state["rows"] = [_row("CGSTA17_S16_SS2_Caa", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R36_SR4"),
                     _row("CGSTR17_R36_SR4", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R42"),
                     _row("CGSTR17_R42", "CGSTR17", "RULES", "REFERS", target="CGSTR17_R80")]
    state["made"] = [["CGSTR17_R36_SR4#TEXT"], ["CGSTR17_R42#TEXT"]]
    totals = {"CGSTA17": SimpleNamespace(source_mkps=["CGSTA17_S16_SS2_Caa"])}
    main._references(cfg, paths, {}, {}, totals)
    assert [[j.batch.root for j in run] for run in state["jobs"]] == [["CGSTR17_R36_SR4"], ["CGSTR17_R42"]]
