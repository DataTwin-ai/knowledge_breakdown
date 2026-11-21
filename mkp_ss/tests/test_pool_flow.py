"""Worker pool end to end: CGST Act section 7 split into small jobs, 3 workers, fake MKP / AKP / GKP models,
local PostgreSQL. Checks: every MKP of the section is made once, groups and references saved, a rate-limited
unit goes back in the queue, a failing unit goes to the error log while the others finish.
Run: python -m pytest -q mkp_ss/tests/test_pool_flow.py
"""
import copy
import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "mkp_ss" / "tests", ROOT / "akp_ss" / "tests", ROOT / "gkp_ss" / "tests"):
    sys.path.insert(0, str(p))
import ikp_engine as p0  # noqa: E402
import kp_errors  # noqa: E402
import mkp_engine as p1  # noqa: E402
from kp_pool import Job, Pool  # noqa: E402
from mkp_ss.scope import jobs_for  # noqa: E402
from mkp_ss.source import unit_context  # noqa: E402

MKP_T, AKP_T, PFX = "mkp_pool", "pool_akp", "pool_gkp"
AKP_NAMES = {"llm_cache_table": AKP_T + "_llm_cache", "fact_table": "pool_fact", "atom_fact_table": "pool_atom_fact",
             "time_fact_table": "pool_time_fact", "gkp_table": PFX, "reuse_old_cache_table": ""}
GKP_NAMES = {"table": PFX, "cache_table": PFX + "_cache", "llm_cache_table": PFX + "_llm_cache",
             "coverage_table": PFX + "_coverage", "taxonomy_table": PFX + "_taxonomy", "atom_fact_table": "pool_atom_fact"}


class Limited(Exception):
    pass


class MkpFake:
    """The MKP test fake + groups on every MKP row; raises 'too many requests' once for one unit and always fails
    for another (to test the queue and the error log)."""

    def __init__(self, base, limited_unit, broken_unit):
        self.base, self.limited, self.broken, self.hits = base, limited_unit, broken_unit, 0

    @staticmethod
    def _expected(prompt):
        import json
        import re
        m = re.search(r"EXPECTED PROVISION LIST:?\s*(\[.*?\])", prompt)
        return set(json.loads(m.group(1))) if m else set()

    def __call__(self, provider, model, prompt):
        exp = self._expected(prompt)
        mine = lambda root: any(k == root or k.startswith(root + "_") for k in exp)
        if mine(self.broken):
            raise RuntimeError("model answered nonsense (test)")
        if mine(self.limited) and self.hits == 0:
            self.hits += 1
            raise Limited("429 Too Many Requests (test)")
        out = self.base(provider, model, prompt)
        lines = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 4 and parts[1] == "MKP" and "knowledge_type: OBLIGATION" in line:
                col = 5 if len(parts) == 7 else 3            # judge FINAL column / generator RESULTS
                parts[col] += "; groups: [TSG01_01, TSG99_99]"
                line = "|".join(parts)
            lines.append(line)
        return "\n".join(lines)


def emb(text):
    v = [0.0] * 8
    for w in str(text).lower().split():
        v[hash(w) % 8] += 1.0
    return v if any(v) else [1.0] + [0.0] * 7


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cfg = copy.deepcopy(__import__("kp_settings").load())
    paths = dict(p1.build_paths(cfg))
    paths["output_dir"] = tmp_path_factory.mktemp("pool_out")
    try:
        conn = p0.get_connection(paths)
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    with conn.cursor() as cur:
        for t in (MKP_T, MKP_T + "_cache", MKP_T + "_llm_cache", AKP_T, AKP_T + "_cache", AKP_T + "_llm_cache",
                  "pool_fact", "pool_atom_fact", "pool_time_fact", PFX, PFX + "_cache", PFX + "_llm_cache",
                  PFX + "_tg18_proposals", PFX + "_coverage", PFX + "_taxonomy"):
            cur.execute(f"DROP TABLE IF EXISTS public.{t}")
    conn.commit()
    s1 = p1.validate_mkp_settings({**cfg["mkp"], "enabled": True, "ask_user_input": False, "table": MKP_T,
                                   "export_excel": False, "judge_retries": 0})
    doc = next(dict(v, document_key=k) for k, v in cfg["documents"].items() if v["code"].upper() == "CGSTA17")
    run = p1.MkpRun(doc, cfg, paths, s1, conn, embed=emb)
    from test_mkp_flow import Fake as P1Fake
    from test_akp_flow import Fake as P2Fake
    from test_gkp_flow import Fake as P3Fake
    jobs = jobs_for(run.idx, "CGSTA17_S7", 600, run.drafter)        # tiny limit: section 7 is split
    tax_tmp = paths["output_dir"] / "taxonomy.json"
    tax_tmp.write_text((ROOT / "Config" / "gst_taxonomy.json").read_text(encoding="utf-8"), encoding="utf-8")
    from gkp_ss import taxonomy as tax
    _, subs = tax.load(tax_tmp)
    tax.sync(conn, "public", subs, emb, "TEST", PFX + "_taxonomy")
    limited, broken = jobs[1].root, jobs[-1].root
    fake = MkpFake(P1Fake(run.drafter), limited, broken)
    pool = Pool(cfg, paths, {"mkp": True, "akp": True, "gkp": True}, 3, s1, fake_llm=fake, embed=emb,
                akp_fake=P2Fake(), gkp_fake=P3Fake(),
                akp_override={"enabled": True, "mkp_table": MKP_T, "table": AKP_T, "cache_table": AKP_T + "_cache", **AKP_NAMES,
                              "ask_user_input": False},
                gkp_override={"enabled": True, "akp_table": AKP_T, "mkp_table": MKP_T, **GKP_NAMES,
                              "taxonomy_file": str(tax_tmp), "use_cache": True})
    pool.add([Job(doc, j) for j in jobs])
    totals = pool.start()
    yield {"conn": conn, "run": run, "jobs": jobs, "totals": totals, "paths": paths, "limited": limited,
           "broken": broken, "doc": doc, "fake": fake}
    conn.close()


def q(conn, sql, *a):
    with conn.cursor() as cur:
        cur.execute(sql, a)
        out = cur.fetchall() if cur.description else []
    conn.commit()
    return out


def test_section_split_into_jobs_covers_every_mkp_once(env):
    run, jobs = env["run"], env["jobs"]
    assert len(jobs) > 3 and any(j.only_keys for j in jobs)
    doc = env["doc"]
    whole = [d.mkp_key for b in __import__("mkp_ss.scope", fromlist=["x"]).batches_for(run.idx, "CGSTA17_S7")
             for d in unit_context(run.drafter, b, doc)["drafts"]]
    parts = [d.mkp_key for j in jobs for d in unit_context(run.drafter, j, doc, only_keys=j.only_keys)["drafts"]]
    assert sorted(parts) == sorted(whole)


def test_all_jobs_finish_and_mkps_saved(env):
    t = env["totals"]["CGSTA17"]
    assert t.done + t.failed == t.jobs and t.failed == 1
    conn, run = env["conn"], env["run"]
    saved = {r[0] for r in q(conn, f"SELECT DISTINCT element FROM {MKP_T} WHERE type='MKP' AND is_latest")}
    broken = env["broken"]
    want = {d.mkp_key for j in env["jobs"] if j.root != broken
            for d in unit_context(run.drafter, j, env["doc"], only_keys=j.only_keys)["drafts"]}
    norm = lambda k: k.replace("#HEAD", "#TEXT")                   # a heading-only node is saved as #TEXT
    assert want and {norm(k) for k in want} <= {norm(k) for k in saved}


def test_rate_limited_unit_went_back_and_finished(env):
    conn = env["conn"]
    assert env["fake"].hits == 1
    assert q(conn, f"SELECT count(*) FROM {MKP_T} WHERE type='MKP' AND element LIKE %s", env["limited"] + "%")[0][0] > 0


def test_failed_unit_in_error_log_others_go_on(env):
    out = env["paths"]["output_dir"]
    assert kp_errors.failed_units(out, "CGSTA17") == [env["broken"]]
    rows = list(csv.DictReader((out / "CGSTA17" / "error_log.csv").open(encoding="utf-8-sig")))
    assert rows[-1]["stage"] in ("MKP", "MKP_GATE") and rows[-1]["worker"].startswith("W")


def test_groups_and_references_saved_on_mkps(env):
    conn = env["conn"]
    g = q(conn, f"SELECT group_names FROM {MKP_T} WHERE type='MKP' AND is_latest AND group_names::text LIKE '%%TSG01_01%%'")
    assert g and all("TSG99_99" not in str(x[0]) for x in g)          # unknown id dropped by the Python gate
    refs = q(conn, f"SELECT self_refs FROM {MKP_T} WHERE element='CGSTA17_S7_SS1A#TEXT' AND is_latest AND type='MKP'")
    assert refs and "CGSTA17_SCHII" in str(refs[0][0])


def test_akp_and_gkp_ran_for_the_units(env):
    conn = env["conn"]
    assert q(conn, f"SELECT count(*) FROM {AKP_T} WHERE type='A' AND is_latest")[0][0] > 0
    assert q(conn, f"SELECT count(*) FROM {PFX} WHERE is_latest AND status='APPROVED'")[0][0] > 0
    assert env["totals"]["CGSTA17"].source_mkps


def test_no_credit_stops_the_run_instead_of_failing_every_unit(env):
    """A 'credit balance too low' answer cannot be fixed by retrying: the pool stops, waiting jobs never start."""
    run = env["run"]
    jobs = jobs_for(run.idx, "CGSTA17_S9", 300, run.drafter)
    assert len(jobs) > 4

    def broke(provider, model, prompt):
        raise RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    import db_migrate
    q(env["conn"], f"DELETE FROM {MKP_T}_llm_cache RETURNING 1")
    pool = Pool(__import__("kp_settings").load(), env["paths"], {"mkp": True, "akp": False, "gkp": False}, 2,
                run.s, fake_llm=broke, embed=emb)
    pool.add([Job(env["doc"], j) for j in jobs])
    t = pool.start()["CGSTA17"]
    assert "credit" in pool.stopped and t.failed <= 2 and t.done == 0
    assert db_migrate.POOL_READY is False
