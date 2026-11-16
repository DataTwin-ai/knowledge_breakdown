"""DARP LAW ARC - web scraping of cross-referenced documents (separate script; off by default).

    python ref_engine.py                 download + check every PENDING notification / circular
    python ref_engine.py --limit 10      at most 10 documents this time
    python ref_engine.py --run           then push the documents it got through IKP -> MKP -> AKP -> GKP

Needs permission.json features.web_scraping = true and access.internet = true. For each ref_document row with
status PENDING (made by MKP from the cross references of the law):
  1. link   the saved link, or Claude's web search (llm.ref_link, then llm.ref_link_retry) + Python's page /
            result checks find the official PDF link (ref_ss/link.py)
  2. fetch  HTTP 200, the file is a PDF, the host is an official one (Config/settings.json refs.allowed_hosts)
  3. check  a. Python: the number and year are in its first pages
            b. the LLM compares the first pages with the laws that cite it ("does this document really amend /
               explain this provision?") - a wrong file is refused (LINK_BAD) and the LLM is asked for another
               link (refs.max_link_tries)
  4. save   Law/Amendments (amending Acts), Law/Notifications or Law/Circulars + Config/documents.json
            (code = its fixed ID, e.g. ACT_31_2018, N_CTR_04_2018); status VERIFIED; with --run it goes through
            IKP -> MKP -> AKP -> GKP and becomes INSERTED. GKP gives its atoms the groups of the laws that cite it
            (no grouping LLM), and the governance master rebuilds only the TSGs those atoms are in.
Each document is fetched once (ref_document has one row per document, however many laws cite it). Amending
documents are not skipped any more (they are needed for the law before / after a change, notes section 13).
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
taxinformation.cbic.gov.in), the GST Council, the e-Gazette (egazette.gov.in) or, for an Act of Parliament,
India Code (indiacode.nic.in) / the Legislative Department (legislative.gov.in).
Document: {title}
Kind: {kind}   Number: {number}   Year: {year}   Series: {series}
{previous}
Answer with ONE JSON object only: {{"url": "<direct pdf link or empty>", "title": "<official title>"}}.
If you do not know the exact link, return an empty url. Never guess a link."""


def _ask_link(cfg: dict, ref: dict, previous: List[str]) -> dict:
    """The official link: Claude's web search + Python's page / result checks (ref_ss/link.py, cheapest model
    first); only when that cannot run (no Claude model), the model is asked from what it knows."""
    try:
        from ref_ss import link as finder
        from ref_ss.fetch import check_link
        got = finder.find(cfg, ref, finder.searchers(cfg), check_link, previous=previous)
        return {"url": got["link"], "title": ""}
    except ValueError:
        pass                                     # ref_link is not a Claude model: the old way below
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


CHECK_PROMPT = """You check a downloaded Indian GST document before it is used.
Document asked for: {title} ({kind} {number} of {year})
First pages of the downloaded PDF:
{text}
Laws that cite it (what they say about it):
{needs}
Is this PDF really this document, and does it really amend / explain / notify what these laws cite it for?
Answer with ONE JSON object only: {{"match": true or false, "reason": "<one sentence>"}}."""


def _check_content(cfg: dict, ref: dict, text: str, needs: List[dict]) -> tuple:
    """Step 3b: the LLM compares the PDF with the laws that need it (Python checked number + year already)."""
    if not needs:
        return True, "no citing law text to compare with"
    role = (cfg.get("refs") or {}).get("link_finder")
    from llm_runtime import call_llm, create_llm_runtime
    rt = dict(create_llm_runtime(role["llm"], kp_settings.build_paths(cfg)))
    rt["model"] = role["model"]
    needs_text = "\n".join(f"- {n['mkp']}: mentions \"{n['mention']}\" - {n['text'][:800]}" for n in needs)
    raw, _ = call_llm(rt, CHECK_PROMPT.format(title=ref["title"] or ref["ref_id"], kind=ref["kind"],
                                              number=ref.get("number") or "", year=ref.get("year") or "",
                                              text=" ".join(text.split())[:6000], needs=needs_text))
    t = raw.strip()
    obj = json.loads(t[t.find("{"): t.rfind("}") + 1] or "{}")
    return bool(obj.get("match")), str(obj.get("reason") or "")[:300]


def _folder_type(ref: dict) -> tuple:
    """(document type, kind folder key) of an outside document."""
    if ref["kind"] == "CIRCULAR":
        return "CIRCULAR", "CIRCULAR"
    if ref["kind"] == "AMENDING_ACT":
        return "ACT", "AMENDMENT"
    return "NOTIFICATION", "NOTIFICATION"


def _register(cfg: dict, ref: dict, file_name: str, title: str, url: str) -> str:
    """Add the downloaded PDF to Config/documents.json (code = its fixed ID)."""
    reg_path = kp_settings.resolve(cfg["files"]["documents_registry"])
    kp_settings.require(cfg, "WRITE", reg_path)
    reg = json.loads(reg_path.read_text(encoding="utf-8-sig")) if reg_path.is_file() else {}
    doc_type = _folder_type(ref)[0]
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
    folders["AMENDMENT"] = cfg["law_folders"]["amendments"]["folder"]     # Law/Amendments (amending Acts)
    mkp_table = (cfg.get("mkp") or {}).get("table", "mkp")
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
                kind_folder = folders[_folder_type(ref)[1]]
                dest = Path(paths["law_dir"]) / kind_folder / f"{ref['ref_id']}.pdf"
                kp_settings.require(cfg, "WRITE", dest)
                try:
                    data = fetch_pdf(link, int(rc.get("request_timeout_seconds", 60)), rc.get("allowed_hosts"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    ok, why = verify_pdf(dest, ref.get("number"), ref.get("year"), ref["kind"])
                    if ok:                     # 3b: is it really what the citing laws mean?
                        from ref_ss.fetch import first_pages_text
                        ok, why = _check_content(cfg, ref, first_pages_text(dest, 3), store.needed_by(ref["ref_id"], mkp_table))
                        why = ("content check: " + why) if why else "content check"
                except FetchError as exc:
                    ok, why = False, str(exc)
                except Exception as exc:       # the content check could not run: the file is not trusted
                    ok, why = False, f"content check failed - {type(exc).__name__}: {str(exc)[:120]}"
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
