"""DARP LAW ARC - web scraping of cross-referenced documents (separate script; off by default).

    python ref_engine.py                 download + check every PENDING notification / circular
    python ref_engine.py --limit 10      at most 10 documents this time
    python ref_engine.py --run           then push the documents it got through IKP -> MKP -> AKP -> GKP

Needs permission.json features.web_scraping = true and access.internet = true. For each ref_document row with
status PENDING (made by MKP from the cross references of the law):
  1. link   the saved link, or the LLM (permission.json llm.ref_link) is asked for the official PDF link
  2. fetch  HTTP 200, the file is a PDF, the host is an official one (Config/settings.json refs.allowed_hosts)
  3. check  the notification / circular number and year are in its first pages (Python); a wrong file is
            refused (LINK_BAD) and the LLM is asked once more (refs.max_link_tries)
  4. save   Law/Notifications or Law/Circulars + Config/documents.json (code = its fixed ID, e.g. N_CTR_04_2018)
            status VERIFIED; with --run it goes through the pipeline and becomes INSERTED.
Amending notifications (their text is already inside the consolidated Act / Rules) stay SKIPPED unless
features.push_amending_notifications is true.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import List, Optional

import kp_settings
from kp_console import flow, log

LINK_PROMPT = """You are helping download an official Indian GST document.
Give the direct link to the official PDF of this document, published by CBIC (cbic-gst.gov.in / cbic.gov.in /
taxinformation.cbic.gov.in), the GST Council or the e-Gazette (egazette.gov.in).
Document: {title}
Kind: {kind}   Number: {number}   Year: {year}   Series: {series}
{previous}
Answer with ONE JSON object only: {{"url": "<direct pdf link or empty>", "title": "<official title>"}}.
If you do not know the exact link, return an empty url. Never guess a link."""


def _ask_link(cfg: dict, ref: dict, previous: List[str]) -> dict:
    role = (cfg.get("refs") or {}).get("link_finder")
    if not role:
        raise RuntimeError("permission.json llm.ref_link is missing (the model that finds official links)")
    from llm_runtime import call_llm, create_llm_runtime
    rt = dict(create_llm_runtime(role["llm"], kp_settings.build_paths(cfg)))
    rt["model"] = role["model"]
    prev = ("These links were wrong (not the document, or not a PDF): " + "; ".join(previous)) if previous else ""
    raw, _ = call_llm(rt, LINK_PROMPT.format(title=ref["title"] or ref["ref_id"], kind=ref["kind"],
                                             number=ref.get("number") or "", year=ref.get("year") or "",
                                             series=ref.get("series") or "", previous=prev))
    text = raw.strip()
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1] or "{}")
    return {"url": str(obj.get("url") or "").strip(), "title": str(obj.get("title") or "").strip()}


def _register(cfg: dict, ref: dict, file_name: str, title: str, url: str) -> str:
    """Add the downloaded PDF to Config/documents.json (code = its fixed ID)."""
    reg_path = kp_settings.resolve(cfg["files"]["documents_registry"])
    kp_settings.require(cfg, "WRITE", reg_path)
    reg = json.loads(reg_path.read_text(encoding="utf-8-sig")) if reg_path.is_file() else {}
    doc_type = "CIRCULAR" if ref["kind"] == "CIRCULAR" else "NOTIFICATION"
    reg[file_name] = {"name": title or ref["title"] or ref["ref_id"], "code": ref["ref_id"], "type": doc_type,
                      "url": url, "ref_id": ref["ref_id"]}
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ref["ref_id"]


def run(limit: int = 50, then_run: bool = False) -> List[str]:
    cfg = kp_settings.load()
    if not cfg["features"]["web_scraping"]:
        flow("WEB SCRAPING : switched off - set features.web_scraping = true in permission.json to run it")
        return []
    kp_settings.require(cfg, "INTERNET")
    from db_insert import get_connection, get_db_schema
    from ref_ss.fetch import FetchError, fetch_pdf, verify_pdf
    from ref_ss.store import RefStore
    rc = cfg.get("refs") or {}
    conn = get_connection()
    store = RefStore(conn, get_db_schema(), rc.get("document_table", "ref_document"), rc.get("link_table", "mkp_ref"))
    store.ensure()
    paths = kp_settings.build_paths(cfg)
    folders = {spec["type"]: spec["folder"] for spec in cfg["law_folders"].values()}
    got: List[str] = []
    try:
        todo = store.pending(limit=limit)
        flow(f"WEB SCRAPING : {len(todo)} document(s) waiting")
        for ref in todo:
            tries, bad = int(rc.get("max_link_tries", 2)), []
            link = ref["link"]
            for attempt in range(tries + 1):
                title = ""
                if not link:
                    try:
                        ans = _ask_link(cfg, ref, bad)
                    except Exception as exc:
                        flow(f"  {ref['ref_id']:<16} link not found - {str(exc)[:120]}")
                        break
                    link, title = ans["url"], ans["title"]
                    if not link:
                        break
                kind_folder = folders["CIRCULAR" if ref["kind"] == "CIRCULAR" else "NOTIFICATION"]
                dest = Path(paths["law_dir"]) / kind_folder / f"{ref['ref_id']}.pdf"
                kp_settings.require(cfg, "WRITE", dest)
                try:
                    data = fetch_pdf(link, int(rc.get("request_timeout_seconds", 60)), rc.get("allowed_hosts"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    ok, why = verify_pdf(dest, ref.get("number"), ref.get("year"), ref["kind"])
                except FetchError as exc:
                    ok, why = False, str(exc)
                if ok:
                    code = _register(cfg, ref, dest.name, title, link)
                    store.set(ref["ref_id"], link=link, link_status="OK", status="VERIFIED",
                              local_file=str(dest.relative_to(kp_settings.BASE_DIR)), doc_code=code)
                    flow(f"  {ref['ref_id']:<16} VERIFIED -> Law/{kind_folder}/{dest.name}")
                    got.append(code)
                    break
                dest.unlink(missing_ok=True)
                bad.append(link)
                store.set(ref["ref_id"], link=link, link_status="BAD", status="LINK_BAD", note=why[:300])
                log(f"WEB SCRAPING {ref['ref_id']}: {link} refused - {why}")
                link = ""
            if ref["ref_id"] not in got:
                store.set(ref["ref_id"], status="NOT_FOUND" if not bad else "LINK_BAD",
                          note="no official link found" if not bad else f"{len(bad)} wrong link(s)")
                flow(f"  {ref['ref_id']:<16} {'NOT FOUND' if not bad else 'LINK BAD'} - add the PDF to Law/ by hand")
    finally:
        conn.close()
    if got and then_run:
        flow(f"WEB SCRAPING : {len(got)} new document(s) go through IKP -> GKP now")
        import main as law_arc
        law_arc.main(["--documents", ",".join(got)])
    elif got:
        flow("WEB SCRAPING : run  python main.py --documents " + ",".join(got) + "  to push them")
    return got


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LAW ARC web scraping of cross-referenced notifications / circulars")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--run", action="store_true", help="push the documents it got through the pipeline")
    a = ap.parse_args()
    run(a.limit, a.run)
