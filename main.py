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
   the relationships + the fact flow observer page (relationship_builder.py), and a summary per document. Errors are in Output/<DOC>/error_log.csv; the full detail in Output/logs/.
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
                failed = kp_errors.failed_units(paths["output_dir"], doc["code"])
                # a row for the whole document (IKP / MKP_LOAD could not reach the database) is not a law: running it
                # again would run the WHOLE document (bug list D16). Run IKP again instead: python main.py
                whole = [u for u in failed if u in (doc["code"], run.idx.doc)]
                units = [u for u in failed if u in run.idx.nodes and u not in whole]
                mine = [Job(doc, Batch(u, label_of(run.idx, u), u, True, False)) for u in units]
                flow(f"PLAN      : {doc['name']} ({doc['code']}) - {len(mine)} failed unit(s) again"
                     + (f" (a failed row for the whole document is skipped - it is not a law)" if whole else ""))
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
    if run.get("akp"):                    # bug list C: references of the run's laws, automatically, with the workers
        _references_waiting(cfg, paths, run, mkp_settings, totals)
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


def _references_waiting(cfg: dict, paths: dict, run: dict, mkp_settings: dict, totals: dict) -> None:
    """The references step, with the same wait as the workers (2026-11-20): when the database cannot be reached it
    waits (database.unreachable_wait_seconds, trying every unreachable_retry_seconds) and runs the step once more.
    Any other error, or still unreachable: the laws are saved and the references stay PENDING / QUEUED for the next
    run (a QUEUED row is picked again)."""
    import db_insert
    for attempt in (1, 2):
        try:
            _references(cfg, paths, run, mkp_settings, totals)
            return
        except Exception as exc:
            down = db_insert.is_db_unreachable(exc) or db_insert.is_connection_lost(exc)
            if attempt == 1 and down:
                total, every = db_insert.unreachable_wait()
                flow(f"REFERENCES : database not reachable ({str(exc)[:100]}) - waiting, then once more")
                if db_insert.wait_for_db(total, every, flow):
                    continue
            flow(f"REFERENCES : not done ({type(exc).__name__}: {str(exc)[:160]}) - they stay PENDING / QUEUED "
                 "for the next run")
            return


def _references(cfg: dict, paths: dict, run: dict, mkp_settings: dict, totals: dict) -> None:
    """Bug list C (agreed 2026-11-18), after the workers, before the SAME checks and the governance master:
    SELF  - the direct targets of the run's laws with no MKP yet (7(1)(a) -> Schedule III) run MKP -> AKP -> GKP;
    CROSS - outside documents (refs.auto_fetch, features.web_scraping, access.internet): download + check, IKP, the
            EXACT part each citing law needs runs MKP -> AKP with no GKP; a part already inserted (approved AKP) only
            gets the citing law's link (mkp_ref.source_key). The governance master writes the part in full in the
            citing law's yaml (reference design 2026-11-19 - no TSG copy).
    Status: PENDING -> QUEUED -> INSERTED / FAILED. Never uses permission.json scope.default."""
    import ikp_engine
    from db_insert import get_db_schema
    from kp_pool import Job, Pool
    from mkp_engine import MkpRun
    from mkp_ss.scope import Batch, label_of
    from ref_ss import auto
    rc = cfg.get("refs") or {}
    for k in ("auto_fetch", "auto_fetch_limit", "self_depth", "self_limit", "whole_document_max_parts", "parts_view"):
        if k not in rc:
            raise KeyError(f"Config/settings.json refs.{k} is missing")
    schema = get_db_schema()
    link_t, doc_t = rc.get("link_table", "mkp_ref"), rc.get("document_table", "ref_document")
    mkps = [m for t in totals.values() for m in t.source_mkps]
    conn = ikp_engine.get_connection(paths)
    jobs: List = []
    queued: List[dict] = []
    akp_t = cfg["akp"]["table"]
    try:
        rows = auto.pending_refs(conn, schema, link_t, doc_t, mkps)
        # ---- SELF: the target is a law of its own (own MKP, own TSG); depth 1 = only what these laws name
        targets = auto.self_targets(rows, rc["self_limit"] if int(rc["self_depth"]) >= 1 else 0)
        idx_of: Dict[str, object] = {}
        for code, key in targets:
            doc = cfg["documents"].get(code)
            if not doc:
                continue
            if code not in idx_of:
                idx_of[code] = MkpRun(dict(doc, document_key=code), cfg, paths, mkp_settings, conn).idx
            idx = idx_of[code]
            if key in idx.nodes:
                jobs.append(Job(dict(doc, document_key=code), Batch(key, label_of(idx, key), key, True, False)))
                queued += [r for r in rows if r["scope"] == "SELF" and r["target_key"] == key]
        if targets:
            flow(f"REFERENCES : {len(jobs)} self reference target(s) of this run's laws - "
                 + ", ".join(k for _c, k in targets[:8]) + (" ..." if len(targets) > 8 else ""))
        # ---- CROSS: outside documents, only the exact part each law needs
        cross = [r for r in rows if r["scope"] == "CROSS"]
        if cross and rc["auto_fetch"] and cfg["features"].get("web_scraping") and kp_settings.allowed(cfg, "INTERNET"):
            ids = list(dict.fromkeys(r["ref_id"] for r in cross))[:int(rc["auto_fetch_limit"])]
            need = [i for i in ids if not any(r["ref_id"] == i and r["doc_code"] and r["doc_status"] in
                                              ("VERIFIED", "INSERTED") for r in cross)]
            if need:
                import ref_engine
                ref_engine.run(limit=len(need), then_run=False, ref_ids=need)     # download + check (manual tool too)
            fresh = kp_settings.load(fresh=True)
            got = auto.pending_refs(conn, schema, link_t, doc_t, mkps)
            from ikp_engine import run_ikp_pipeline, validate_ikp_settings
            ikp_settings = validate_ikp_settings(fresh["ikp"])
            prompt = (kp_settings.resolve(fresh["paths"]["prompt_dir"]) / fresh["files"]["ref_part_prompt"]).read_text(
                encoding="utf-8-sig")
            ask = _ref_model(fresh)
            for ref_id in ids:
                mine = [r for r in got if r["scope"] == "CROSS" and r["ref_id"] == ref_id]
                code = next((r["doc_code"] for r in mine if r["doc_code"]), "")
                doc = fresh["documents"].get(code) if code else None
                if not doc:
                    flow(f"REFERENCES : {ref_id} - no verified PDF yet (see ref_document) - stays PENDING")
                    continue
                doc = dict(doc, document_key=code)
                pdf = paths["law_dir"] / doc["file"]
                run_ikp_pipeline(pdf, ikp_settings, doc, paths)        # reused when this PDF was IKP'd before
                idx = MkpRun(doc, fresh, paths, mkp_settings, conn).idx
                for r in mine:
                    citing_key = auto.unit(r["mkp_element"])
                    saved = [k for k in str(r.get("source_key") or "").split(",") if k in idx.nodes]
                    if saved:                                    # found before (a run that stopped / failed)
                        keys, how = saved, "found before (mkp_ref.source_key)"
                    else:
                        keys, how = auto.find_part(idx, {"key": citing_key, "text": "", "mention": r["mention"],
                                                         "relation": r["relation"]},
                                                   int(rc["whole_document_max_parts"]), ask, prompt)
                    if not keys:
                        flow(f"REFERENCES : {ref_id} for {citing_key} - {how} - stays PENDING")
                        continue
                    auto.set_source_key(conn, schema, link_t, r, keys)
                    if auto.part_done(conn, schema, akp_t, code, keys):   # inserted before: link only, no run
                        auto.set_status(conn, schema, link_t, [r], "INSERTED")
                        flow(f"REFERENCES : {ref_id} for {citing_key} -> {', '.join(keys)} already inserted - linked")
                        continue
                    flow(f"REFERENCES : {ref_id} for {citing_key} -> {', '.join(keys)} ({how})")
                    for k in keys:
                        if not any(j.doc["code"] == code and j.batch.root == k for j in jobs):
                            jobs.append(Job(doc, Batch(k, label_of(idx, k), k, True, False), no_gkp=True,
                                            attach_to=r["mkp_element"]))
                    queued.append(r)
        elif cross:
            flow(f"REFERENCES : {len({r['ref_id'] for r in cross})} outside document(s) waiting - automatic download "
                 "is off (refs.auto_fetch / features.web_scraping / access.internet); python ref_engine.py --run")
        if not jobs:
            return
        auto.set_status(conn, schema, link_t, queued, "QUEUED")
    finally:
        conn.close()
    flow(f"REFERENCES : {len(jobs)} reference job(s) go to the workers")
    pool = Pool(cfg, paths, run, cfg["workers"]["count"], mkp_settings)
    pool.add(jobs)
    more = pool.start()
    for code, t in more.items():          # the references count in this run (SAME checks, governance master)
        mine = totals.setdefault(code, t)
        if mine is not t:
            for m in t.source_mkps:
                if m not in mine.source_mkps:
                    mine.source_mkps.append(m)
    conn = ikp_engine.get_connection(paths)
    try:
        from ref_ss.store import RefStore
        rs = RefStore(conn, schema, doc_t, link_t)
        rs.update_target_status(akp_t, rc["parts_view"])          # INSERTED per clause (approved AKP)
        still = [r for r in auto.pending_refs(conn, schema, link_t, doc_t, mkps)
                 if any(q["mkp_element"] == r["mkp_element"] and q["ref_id"] == r["ref_id"]
                        and q["target_key"] == r["target_key"] for q in queued)]
        auto.set_status(conn, schema, link_t, still, "FAILED")
        flow(f"REFERENCES : {len(queued) - len(still)} reference(s) INSERTED, {len(still)} FAILED "
             "(FAILED ones: python main.py --retry-failed)")
    finally:
        conn.close()


def _ref_model(cfg: dict):
    """The model that picks a part of an outside document (refs.link_finder, used only when Python finds none)."""
    role = (cfg.get("refs") or {}).get("link_finder")
    if not role:
        return None
    from llm_runtime import call_llm, create_llm_runtime
    rt = dict(create_llm_runtime(role["llm"], kp_settings.build_paths(cfg)))
    rt["model"] = role["model"]
    return lambda prompt: call_llm(rt, prompt)[0]


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
    if run.get("akp") and mkps:           # bug list A4: SAME atoms of laws that ran at the same time, before the yaml
        try:
            from akp_engine import fact_sweep, same_sweep
            flow("SAME CHECK : facts and atoms of this run against every saved one")
            fact_sweep(mkps)              # bug list B8: facts first, so the atom check sees the merged fact ids
            same_sweep(mkps)
        except Exception as exc:          # the laws are saved; only the mapping is missing (python akp_engine.py --same-review)
            flow(f"SAME CHECK : not done ({type(exc).__name__}: {str(exc)[:160]}) - run  python akp_engine.py --same-review")
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
    _relationships(cfg)
    show_summary(cfg, totals, gm, paths)
    return gm


def _relationships(cfg: dict) -> None:
    """kp_relation + the fact flow observer page, at the end of every run (Config/settings.json
    relationship.run_after_pipeline), so the page always shows the latest graph (notes section 14)."""
    if not (cfg.get("relationship") or {}).get("run_after_pipeline"):
        return
    try:
        import relationship_builder
        relationship_builder.run(cfg=cfg)
    except Exception as exc:                  # the run itself is done; only the page is not refreshed
        flow(f"RELATION   : not built ({str(exc)[:160]}) - run  python relationship_builder.py")


def _mark_documents_inserted(cfg: dict, totals: dict) -> None:
    """A notification / circular / Act we just processed: its document row (ref_document) becomes INSERTED - we
    have its file. Then every reference row (mkp_ref) gets the status of the exact law it points to."""
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
            # each reference: is the exact law it points to made yet? (mkp_ref.target_status)
            n = store.update_target_status(cfg["akp"]["table"], rc["parts_view"])
            log(f"REFERENCES : target status of {n} reference(s) updated")
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
