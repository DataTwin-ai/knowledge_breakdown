"""DARP LAW ARC: IKP -> MKP -> AKP -> GKP -> GOVERNANCE MASTER -> FACT CATALOG.

    python main.py                 the documents switched on in permission.json
    python main.py --retry-failed  only the units whose last row in Output/<DOC>/error_log.csv is FAILED
    python main.py --documents N_CTR_04_2018,CIR_47_2018   only these documents (e.g. after ref_engine.py)

1. PDFs: a document in Config/documents.json with a url and no PDF is downloaded first
   (permission.json features.download_missing_pdfs + access.internet).
2. IKP reads each whole PDF and builds its index (Python; AI only for a chapter that fails the check).
3. Scope: with scope.ask_user_input you choose per document (full document, chapter, section, sub-section,
   clause); all questions come first, then the run goes on by itself.
4. Worker pool (kp_pool.py): the scope becomes jobs (one per section / rule / paragraph; big units split);
   workers.count workers run MKP -> AKP -> GKP per job, a free worker takes the next job.
5. When every job is done: Excel files, the governance master of every TSG that got laws, the fact catalog,
   and a summary per document. Errors are in Output/<DOC>/error_log.csv; the full detail in Output/logs/.
"""
from __future__ import annotations

import argparse
from typing import Dict, List

import kp_settings
from kp_console import flow, log, log_path, say, set_show


def main_ikp(cfg: dict) -> List[dict]:
    from ikp_engine import run_ikp_pipeline, validate_ikp_settings
    settings = validate_ikp_settings(cfg["ikp"])
    paths = kp_settings.build_paths(cfg)
    results = []
    for document in kp_settings.selected_documents(cfg):
        path = paths["law_dir"] / document["file"]
        if not path.is_file():
            flow(f"IKP       : {document['code']} PDF missing ({path}) - skipped")
            continue
        try:
            result = run_ikp_pipeline(path, settings, document, paths)
        except Exception as exc:                 # one bad PDF must not stop the other documents
            import kp_errors
            kp_errors.record(paths["output_dir"], document["code"], document["code"], "IKP", exc)
            flow(f"IKP       : {document['code']} FAILED - skipped, the other documents go on ({str(exc)[:200]})")
            log(f"IKP {document['code']} FAILED: {exc}")
            continue
        results.append(result)
        flow(f"IKP       : {document['name']} ({document['code']}) - {result['ikp_rows']} rows, "
             f"LLM calls {result['llm_calls']}")
    return results


def plan_jobs(cfg: dict, paths: dict, mkp_settings: dict, retry_failed: bool = False) -> list:
    """Every selected document's scope as pool jobs (scope questions first, then no more input)."""
    import ikp_engine
    import kp_errors
    from kp_pool import Job
    from mkp_engine import MkpRun
    from mkp_ss.scope import Batch, ask_scope, jobs_for, label_of, resolve_token
    max_chars = int((cfg.get("pool") or {}).get("max_job_chars", 24000))
    jobs: List[Job] = []
    conn = ikp_engine.get_connection(paths)
    try:
        for doc in kp_settings.selected_documents(cfg):
            try:
                run = MkpRun(doc, cfg, paths, mkp_settings, conn)
            except Exception as exc:
                kp_errors.record(paths["output_dir"], doc["code"], doc["code"], "MKP_LOAD", exc)
                flow(f"PLAN      : {doc['code']} skipped - {str(exc)[:160]}")
                continue
            if retry_failed:
                units = [u for u in kp_errors.failed_units(paths["output_dir"], doc["code"]) if u in run.idx.nodes]
                mine = [Job(doc, Batch(u, label_of(run.idx, u), u, True, False)) for u in units]
                flow(f"PLAN      : {doc['name']} ({doc['code']}) - {len(mine)} failed unit(s) again")
                jobs += mine
                continue
            if mkp_settings["ask_user_input"]:
                roots = [ask_scope(run.idx)]
            else:
                tokens = mkp_settings["scope"] if isinstance(mkp_settings["scope"], list) else [mkp_settings["scope"]]
                roots = [resolve_token(run.idx, t) for t in tokens]
            mine, seen = [], set()
            for r in roots:                       # overlapping choices (section 7 + Chapter III) run once
                for b in jobs_for(run.idx, r, max_chars, run.drafter, cfg["workers"]["count"]):
                    key = (b.root, b.only_keys)
                    if key not in seen and not any(b.root.startswith(x[0] + "_") and x[1] is None for x in seen):
                        seen.add(key)
                        mine.append(Job(doc, b))
            split = sum(1 for j in mine if j.batch.only_keys)
            flow(f"PLAN      : {doc['name']} ({doc['code']}) - scope {', '.join(label_of(run.idx, r) for r in roots)} - "
                 f"{len(mine)} job(s)" + (f" ({split} big unit(s) split into parts)" if split else ""))
            jobs += mine
    finally:
        conn.close()
    return jobs


def show_summary(cfg: dict, totals: dict, gm_results: List[dict], paths: dict) -> None:
    docs = {d["code"]: d for d in kp_settings.selected_documents(cfg)}
    flow("\n" + "=" * 72)
    for code, t in totals.items():
        name = docs.get(code, {}).get("name", code)
        flow(f"SUMMARY - {name} ({code})")
        flow(f"  JOBS : {t.jobs} - {t.done} done, {t.failed} failed"
             + (f" - see Output/{code}/error_log.csv" if t.failed else ""))
        flow(f"  MKP  : {t.mkps} MKP(s) - {t.mkps_from_db} from database, {t.mkps - t.mkps_from_db} by LLM "
             f"({t.mkps_new} new, {t.mkps_changed} new version)")
        flow(f"  AKP  : {t.atoms} atoms")
        if t.tsg:
            flow("  GKP  : atoms per TSG")
            for tsg, v in sorted(t.tsg.items()):
                flow(f"         {tsg}  {v['name'][:44]:<44} : {v['atoms']}")
        for c in t.created:
            flow(f"         NEW GROUP (created by GKP) : {c}")
        if t.unclassified:
            flow(f"         UNCLASSIFIED : {t.unclassified} atom(s) - see Output/gkp.xlsx")
        flow(f"  LLM calls : MKP {t.llm_mkp}, AKP {t.llm_akp}, GKP {t.llm_gkp}")
    if gm_results:
        flow("GOVERNANCE MASTER")
        for r in gm_results:
            c = r.get("counts") or {}
            flow(f"         {r['tsg']} : {r['status'].lower()}"
                 + (f" - {c.get('compounds', 0)} steps, {c.get('predicates', 0)} checks" if c else "")
                 + (f" - {r.get('pack_status')}" if r.get("pack_status") else ""))
    flow(f"Full detail : {log_path()}")
    flow("=" * 72)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="DARP LAW ARC")
    ap.add_argument("--retry-failed", action="store_true",
                    help="run again only the units whose last row in Output/<DOC>/error_log.csv is FAILED")
    ap.add_argument("--documents", default="", help="comma-separated document codes: run only these")
    args = ap.parse_args(argv)
    cfg = kp_settings.load()
    only = {c.strip().upper() for c in args.documents.split(",") if c.strip()}
    if only:
        unknown = only - set(cfg["documents"])
        if unknown:
            raise SystemExit(f"--documents: not in Config/documents.json or the Law folders: {', '.join(sorted(unknown))}")
        for code, d in cfg["documents"].items():
            d["read"] = code in only
    set_show(cfg["console"]["show"])
    from config_check import check_permission
    check_permission(cfg)                 # stops here, naming every missing key, before any step runs
    import llm_runtime                    # LLM calls at once per provider (all workers together), prompt caching
    llm_runtime.configure(cfg["workers"]["max_calls_at_once"], cfg["features"]["prompt_caching"],
                          cfg["llm_fallback"])            # Claude no credit / key expired -> Gemini
    run = {k: cfg[k]["enabled"] for k in kp_settings.STEPS}
    paths = kp_settings.build_paths(cfg)
    flow("\nDARP LAW ARC")
    flow("=" * 60)
    flow("STEPS     : " + " -> ".join(k.upper().replace("_", " ") for k in kp_settings.STEPS if run[k])
         + (" -> FACT CATALOG" if cfg["fact_catalog"]["enabled"] else ""))
    from ref_ss.fetch import download_missing
    if download_missing(cfg):                  # the downloaded PDFs are now on disk: read the folders again
        keep = {c: d["read"] for c, d in cfg["documents"].items()}
        cfg = kp_settings.load(fresh=True)
        for c, d in cfg["documents"].items():
            d["read"] = keep.get(c, d["read"])
    docs = kp_settings.selected_documents(cfg)
    flow("DOCUMENTS : " + (", ".join(f"{d['name']} ({d['code']})" for d in docs) or
                           "none - check \"read\" / \"documents\" in permission.json"))
    flow(f"WORKERS   : {cfg['workers']['count']}   LOG : {log_path()}")
    if run["ikp"] and not args.retry_failed:
        main_ikp(cfg)
    if not run["mkp"]:
        flow("MKP       : switched off - AKP / GKP / governance master use the MKPs already saved")
        _without_mkp(cfg, run)
        return
    from mkp_engine import validate_mkp_settings
    mkp_settings = validate_mkp_settings(cfg["mkp"])
    jobs = plan_jobs(cfg, paths, mkp_settings, args.retry_failed)
    if not jobs:
        flow("Nothing to do.")
        return
    if run["gkp"]:
        from gkp_engine import sync_taxonomy
        sync_taxonomy()
    from kp_pool import Pool
    pool = Pool(cfg, paths, run, cfg["workers"]["count"], mkp_settings)
    pool.add(jobs)
    import time as _time
    import kp_usage
    started, run_id = _time.time(), kp_usage.run_id()
    totals = pool.start()
    gm_started = _time.time()
    after_pool(cfg, run, totals, paths)
    _usage(cfg, paths, pool, totals, jobs, run_id, started, gm_started)


def _usage(cfg, paths, pool, totals, jobs, run_id, started, gm_started) -> None:
    """Tokens and time: on screen, and appended to Output/usage.xlsx (Runs, Jobs, Models)."""
    import time as _time
    import kp_usage
    took = _time.time() - started
    for line in kp_usage.summary_lines(took):
        flow(line)
    flow(f"  governance master + fact catalog + Excel : {(_time.time() - gm_started) / 60:.1f} min")
    docs = kp_settings.selected_documents(cfg)
    scope = sorted({j.batch.label.split(" - ")[0][:40] for j in jobs})
    run = {"run_id": run_id, "started": run_id, "documents": ", ".join(d["code"] for d in docs),
           "scope": ("; ".join(scope[:6]) + (" ..." if len(scope) > 6 else "")), "workers": cfg["workers"]["count"],
           "jobs": sum(t.jobs for t in totals.values()), "done": sum(t.done for t in totals.values()),
           "failed": sum(t.failed for t in totals.values()), "seconds": took}
    passed = sum(1 for r in pool.usage_rows if r["status"] == "DONE")
    failed = sum(1 for r in pool.usage_rows if r["status"] == "FAILED")
    flow(f"LAWS      : {passed} passed, {failed} failed - each with its reason in Output/usage.xlsx, sheet Jobs")
    try:
        path = kp_usage.save_excel(paths["output_dir"] / "usage.xlsx", run, pool.usage_rows)
        flow(f"USAGE     : saved to {path}")
    except PermissionError:
        flow("USAGE     : Output/usage.xlsx is open in Excel - close it; this run's usage was not saved")
    except Exception as exc:
        flow(f"USAGE     : not saved ({str(exc)[:120]})")


def after_pool(cfg: dict, run: dict, totals: dict, paths: dict) -> List[dict]:
    """Once, when every job is done: Excel, governance masters, fact catalog, reference status, summary."""
    if run.get("akp"):
        try:
            from akp_engine import export_excel
            export_excel()
        except Exception as exc:
            log(f"AKP EXCEL : not written ({exc})")
    if run.get("gkp"):
        try:
            from gkp_engine import export_excel
            export_excel(list(totals))
        except Exception as exc:
            log(f"GKP EXCEL : not written ({exc})")
    mkps = [m for t in totals.values() for m in t.source_mkps]
    gm: List[dict] = []
    if run.get("governance_master") and mkps:
        flow("GOVERNANCE MASTER : building the TSGs that got laws in this run")
        from governance_master_engine import run_for_mkps
        gm = run_for_mkps(mkps, cfg)
    if cfg["fact_catalog"]["enabled"]:
        try:
            from fact_catalog_engine import build_catalog
            r = build_catalog(cfg)
            flow(f"FACT CATALOG : {r['facts']} fact(s), {r['links']} law link(s) - {r['file']}")
        except Exception as exc:
            flow(f"FACT CATALOG : not built ({str(exc)[:160]})")
    _mark_documents_inserted(cfg, totals)
    show_summary(cfg, totals, gm, paths)
    return gm


def _mark_documents_inserted(cfg: dict, totals: dict) -> None:
    """A notification / circular / Act we just processed: its cross-reference row becomes INSERTED."""
    try:
        from db_insert import get_connection, get_db_schema
        from ref_ss.store import RefStore
        rc = cfg.get("refs") or {}
        conn = get_connection()
        try:
            store = RefStore(conn, get_db_schema(), rc.get("document_table", "ref_document"),
                             rc.get("link_table", "mkp_ref"))
            store.ensure()
            for code in totals:
                d = cfg["documents"].get(code) or {}
                if totals[code].done:
                    store.mark_inserted(d.get("ref_id") or code, code, d.get("name", ""), d.get("type", ""))
        finally:
            conn.close()
    except Exception as exc:
        log(f"REFERENCES : document status not updated ({exc})")


def _without_mkp(cfg: dict, run: dict) -> None:
    """MKP off: AKP / GKP / governance master on the saved MKPs of the default scope (one after another)."""
    scope = cfg["mkp"].get("scope") or "ALL"
    scope = "ALL" if str(scope).upper() in ("FULL", "ALL") else scope
    paths = kp_settings.build_paths(cfg)
    mkps: List[str] = []
    for d in kp_settings.selected_documents(cfg):
        if run["akp"]:
            from akp_engine import run_akp_scope
            run_akp_scope(paths["law_dir"] / d["file"], d["code"], scope_key=scope)
        if run["gkp"]:
            from gkp_engine import run_gkp_scope
            mkps += (run_gkp_scope(d["code"], scope_key=scope) or {}).get("source_mkps", [])
    if run["governance_master"]:
        from governance_master_engine import main_governance_master, run_for_mkps
        if mkps:
            run_for_mkps(mkps, cfg)
        elif not run["gkp"]:
            main_governance_master()


if __name__ == "__main__":
    main()
