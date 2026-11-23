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

2026-11-23 - the link register Config/ref_links.xlsx (ref_ss/register.py) comes first:
  * a link written in the Excel is the ONLY link tried for that document (no web search); the AI checks the PDF
    against the "document name" column; the status column says INSERTED / VERIFIED / WRONG URL - <reason> /
    NOT INSERTED - <reason>. A wrong link is never replaced by the script: fix it in the Excel and run again.
  * an empty link: refs.search_empty_links = true -> searched once (as before) and the link found is written into
    the Excel; false -> status NO LINK, nothing is searched.
  * SKIP rows are never fetched; INSERTED rows are never checked again.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import List, Optional

import kp_settings
from kp_console import flow, log

LINK_PROMPT = """You are helping download an official Indian law document (GST or another Act of Parliament).
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


CHECK_PROMPT = """You check a downloaded Indian law document before it is used.
Document asked for: {title} ({kind} {number} of {year})
First pages of the downloaded PDF:
{text}
Laws that cite it (key, where it is in its Act, what it says about the document):
{needs}
Two separate questions:
1. Is this PDF the document asked for itself - not another Act / notification / circular? {doc_rule}
2. For which of the citing laws do these pages show the change / explanation they cite it for? An amending Act
   names the place it changes by its number only (e.g. "in clause (35) ..." for section 2, clause (35)), not by
   the words of the definition - match by that number.
Answer with ONE JSON object only:
{{"same_document": true or false, "confirmed": ["<law key>", ...], "reason": "<one sentence>"}}."""

# 2026-11-22: what "the document itself" means differs by kind
DOC_RULE = {
    "AMENDING_ACT": "It must be the amending Act itself - not a consolidated or updated copy of the principal Act "
                    "that only mentions it in footnotes.",
    "ACT": "It must be this Act / Rules itself; an updated (consolidated, as amended) copy of it is right and "
           "preferred - not an amending Act, a commentary or another Act.",
    "NOTIFICATION": "It must be this notification / circular itself (its number and year), not another one or a list.",
}
DOC_RULE.update({"RULES": DOC_RULE["ACT"], "OTHER_ACT": DOC_RULE["ACT"], "CIRCULAR": DOC_RULE["NOTIFICATION"]})

_PART = [(r"SCH([IVXL]+)", "Schedule {}"), (r"SS(\d+[A-Z]?)", "sub-section ({})"), (r"S(\d+[A-Z]*)", "section {}"),
         (r"SC([ivxl]+)", "sub-clause ({})"), (r"C([0-9]+[A-Z]*|[a-z]+)", "clause ({})"), (r"EN(\d+)", "paragraph {}"),
         (r"P(\d+)", "proviso {}"), (r"E(\d+)", "Explanation {}")]


def place_words(key: str) -> str:
    """CGSTA17_S2_C35#TEXT -> 'section 2, clause (35)' (2026-11-22: the content check matches an amending Act's
    "in clause (35) ..." by this number, not by the definition's words)."""
    words = []
    for part in key.split("#")[0].split("_")[1:]:
        for rx, fmt in _PART:
            m = re.fullmatch(rx, part)
            if m:
                words.append(fmt.format(m.group(1)))
                break
    return ", ".join(words)


def _check_content(cfg: dict, ref: dict, text: str, needs: List[dict]) -> tuple:
    """Step 3b: the LLM compares the PDF with the laws that need it (Python checked number + year already).
    2026-11-22: refused only when the PDF is NOT the document asked for. A citing law whose change the first pages do
    not show is a warning (log), not a refusal - one weak citation (a footnote) used to throw the right Act away."""
    # 2026-11-23: no citing law text -> the AI still checks the PDF against the document name (question 1)
    role = (cfg.get("refs") or {}).get("link_finder")
    if not role:
        if not needs:
            return True, "no citing law text and no check model (permission.json llm.ref_link)"
        raise RuntimeError("permission.json llm.ref_link is missing (the model that checks the PDF)")
    from llm_runtime import call_llm, create_llm_runtime
    rt = dict(create_llm_runtime(role["llm"], kp_settings.build_paths(cfg)))
    rt["model"] = role["model"]
    needs_text = "\n".join(f"- {n['mkp']} ({place_words(n['mkp']) or 'place unknown'}): mentions \"{n['mention']}\""
                           f" - {n['text'][:800]}" for n in needs) or "(none - answer question 1 only)"
    raw, _ = call_llm(rt, CHECK_PROMPT.format(title=ref["title"] or ref["ref_id"], kind=ref["kind"],
                                              number=ref.get("number") or "", year=ref.get("year") or "",
                                              text=" ".join(text.split())[:6000], needs=needs_text,
                                              doc_rule=DOC_RULE.get(ref["kind"], DOC_RULE["NOTIFICATION"])))
    return _content_verdict(ref, raw, needs)


def _content_verdict(ref: dict, raw: str, needs: List[dict]) -> tuple:
    t = (raw or "").strip()
    obj = json.loads(t[t.find("{"): t.rfind("}") + 1] or "{}")
    same = obj.get("same_document", obj.get("match"))
    reason = str(obj.get("reason") or "")[:300]
    if not same:
        return False, reason
    confirmed = {str(x).strip() for x in obj.get("confirmed") or []}
    for n in needs:
        if n["mkp"] not in confirmed:
            log(f"WEB SCRAPING {ref['ref_id']}: {n['mkp']} ({place_words(n['mkp'])}) - its change is not confirmed in "
                f"the first pages (kept: the PDF is the document asked for)")
    return True, reason


def _folder_type(ref: dict) -> tuple:
    """(document type, kind folder key) of an outside document (2026-11-22: GST Acts / Rules and other Acts too)."""
    kind = ref["kind"]
    if kind == "CIRCULAR":
        return "CIRCULAR", "CIRCULAR"
    if kind == "AMENDING_ACT":
        return "ACT", "AMENDMENT"
    if kind in ("ACT", "OTHER_ACT"):
        return "ACT", "ACT"
    if kind == "RULES":
        return "RULES", "RULES"
    return "NOTIFICATION", "NOTIFICATION"


def _folders(cfg: dict) -> dict:
    """kind folder key -> folder under Law/ (by the law_folders key: Law/Acts and Law/Amendments are both type ACT)."""
    lf = cfg["law_folders"]
    return {"ACT": lf["acts"]["folder"], "RULES": lf["rules"]["folder"], "NOTIFICATION": lf["notifications"]["folder"],
            "CIRCULAR": lf["circulars"]["folder"], "AMENDMENT": lf["amendments"]["folder"]}


def _file_name(cfg: dict, ref: dict) -> str:
    """The PDF name: the one Config/documents.json already gives this code (IGST-Act.pdf for IGSTA17), else <id>.pdf."""
    reg_path = kp_settings.resolve(cfg["files"]["documents_registry"])
    reg = json.loads(reg_path.read_text(encoding="utf-8-sig")) if reg_path.is_file() else {}
    for name, info in reg.items():
        if isinstance(info, dict) and ref["ref_id"] in (str(info.get("code") or "").upper(), info.get("ref_id")):
            return name
    return f"{ref['ref_id']}.pdf"


def _register(cfg: dict, ref: dict, file_name: str, title: str, url: str) -> str:
    """Add the downloaded PDF to Config/documents.json (code = its fixed ID)."""
    reg_path = kp_settings.resolve(cfg["files"]["documents_registry"])
    kp_settings.require(cfg, "WRITE", reg_path)
    reg = json.loads(reg_path.read_text(encoding="utf-8-sig")) if reg_path.is_file() else {}
    doc_type = _folder_type(ref)[0]
    old = reg.get(file_name) if isinstance(reg.get(file_name), dict) else {}
    # reference_only: fetched because a law cites it - only the part it needs is run, never the whole document with
    # permission.json scope (set permission.json documents.<code> = true to read it as a main document)
    reg[file_name] = {"name": old.get("name") or title or ref["title"] or ref["ref_id"], "code": ref["ref_id"],
                      "type": doc_type, "url": url, "ref_id": ref["ref_id"], "reference_only": True}
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ref["ref_id"]


def _sync_inserted(store, cfg: dict, reg: dict) -> None:
    """Register rows whose document is used (ref_document.status / mkp_ref.target_status INSERTED) become INSERTED."""
    from psycopg2 import sql
    from ref_ss import register
    ids = [i for i, row in reg.items() if not register.done(row)]
    if not ids:
        return
    with store.conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT d.ref_id, d.link FROM {d} d WHERE d.ref_id = ANY(%s) AND (d.status = 'INSERTED'
            OR EXISTS (SELECT 1 FROM {l} l WHERE l.ref_id = d.ref_id AND l.target_status = 'INSERTED'))""").format(
            d=store.t(store.doc_t), l=store.t(store.link_t)), (ids,))
        rows = cur.fetchall()
    store.conn.commit()
    register.write(cfg, {r: {"status": register.INSERTED, "link": link or None} for r, link in rows})


def run(limit: int = 50, then_run: bool = False, ref_ids: Optional[List[str]] = None) -> List[str]:
    """ref_ids: only these documents (main.py's automatic step after the workers, bug list C1)."""
    cfg = kp_settings.load()
    if not cfg["features"]["web_scraping"]:
        flow("WEB SCRAPING : switched off - set features.web_scraping = true in permission.json to run it")
        return []
    kp_settings.require(cfg, "INTERNET")
    from db_insert import get_connection, get_db_schema
    from ref_ss import register
    from ref_ss.fetch import FetchError, fetch_pdf, verify_pdf
    from ref_ss.store import RefStore
    rc = cfg.get("refs") or {}
    search_on = bool(rc.get("search_empty_links", True))
    conn = get_connection()
    store = RefStore(conn, get_db_schema(), rc.get("document_table", "ref_document"), rc.get("link_table", "mkp_ref"))
    store.ensure()
    paths = kp_settings.build_paths(cfg)
    folders = _folders(cfg)
    mkp_table = (cfg.get("mkp") or {}).get("table", "mkp")
    got: List[str] = []
    updates: dict = {}
    try:
        reg = register.load(cfg)
        _sync_inserted(store, cfg, reg)
        todo = store.pending(limit=limit if ref_ids is None else 10000)
        # a link written in the Excel for a document ref_document gave up on (NOT_FOUND / LINK_BAD ...): tried again
        have = {r["ref_id"] for r in todo}
        again = [i for i, row in reg.items() if row.get("link") and not register.done(row) and i not in have]
        if again:
            todo += [dict(r, mentions=0) for r in store.link_state(again)
                     if r["status"] not in ("VERIFIED", "QUEUED", "INSERTED")]
        if ref_ids is not None:
            todo = [r for r in todo if r["ref_id"] in set(ref_ids)][:limit]
        register.add_new(cfg, todo, reg)
        reg = register.load(cfg)
        flow(f"WEB SCRAPING : {len(todo)} document(s) waiting - links from {register.path(cfg).name} first")
        from ref_ss.link import local_document
        for ref in todo:
            row = reg.get(ref["ref_id"]) or {}
            if register.skipped(row):
                flow(f"  {ref['ref_id']:<16} {row['status']} (link register)")
                continue
            local = local_document(cfg, ref["ref_id"])
            if local:                  # 2026-11-22: already in Law/ - used as it is, never searched / downloaded
                store.set(ref["ref_id"], status="VERIFIED", link_status="LOCAL", doc_code=local["code"],
                          local_file=str(Path(cfg["paths"]["law_dir"]) / local["file"]), note="PDF already in Law/")
                flow(f"  {ref['ref_id']:<16} VERIFIED -> Law/{local['file']} (already there)")
                updates[ref["ref_id"]] = {"status": register.LOCAL}
                got.append(local["code"])
                continue
            if row.get("document name"):     # the AI checks the PDF against the name in the Excel
                ref = dict(ref, title=row["document name"])
            mine = row.get("link") or ""
            if mine and not row.get("status", "").upper().startswith("FOUND BY SEARCH"):
                link, source, tries = mine, "register", 0      # the Excel's link: the only one tried, never searched
            elif mine:                       # a link the search wrote: checked, and searched again when wrong
                link, source, tries = mine, "search", int(rc.get("max_link_tries", 2))
            elif ref.get("status") == "LINK_FOUND" and ref.get("link"):
                link, source, tries = ref["link"], "search", int(rc.get("max_link_tries", 2))
            elif search_on:
                link, source, tries = "", "search", int(rc.get("max_link_tries", 2))
            else:
                flow(f"  {ref['ref_id']:<16} NO LINK - write its link in {register.path(cfg).name}")
                updates[ref["ref_id"]] = {"status": register.NO_LINK}
                continue
            bad, skipped, last = [], False, ""
            for attempt in range(tries + 1):
                title = ""
                if not link:
                    try:
                        ans = _ask_link(cfg, ref, bad)
                    except Exception as exc:
                        flow(f"  {ref['ref_id']:<16} search could not run - {str(exc)[:120]} - tried again next run")
                        skipped = True
                        break
                    link, title = ans["url"], ans["title"]
                    if not link:
                        break
                kind_folder = folders[_folder_type(ref)[1]]
                dest = Path(paths["law_dir"]) / kind_folder / _file_name(cfg, ref)
                kp_settings.require(cfg, "WRITE", dest)
                wrong = True                   # False: the link itself does not work (NOT INSERTED)
                try:
                    data = fetch_pdf(link, int(rc.get("request_timeout_seconds", 60)), rc.get("allowed_hosts"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    ok, why = verify_pdf(dest, ref.get("number"), ref.get("year"), ref["kind"], ref.get("title") or "")
                    if ok:                     # 3b: is it really what the citing laws mean?
                        from ref_ss.fetch import first_pages_text
                        ok, why = _check_content(cfg, ref, first_pages_text(dest, 3), store.needed_by(ref["ref_id"], mkp_table))
                        why = ("content check: " + why) if why else "content check"
                except FetchError as exc:
                    ok, why, wrong = False, str(exc), False
                except Exception as exc:       # the content check could not run: the file is not trusted
                    ok, why, wrong = False, f"content check failed - {type(exc).__name__}: {str(exc)[:120]}", False
                if ok:
                    code = _register(cfg, ref, dest.name, title, link)
                    store.set(ref["ref_id"], link=link, link_status="OK", status="VERIFIED", note="",
                              local_file=str(dest.relative_to(kp_settings.BASE_DIR)), doc_code=code)
                    flow(f"  {ref['ref_id']:<16} VERIFIED -> Law/{kind_folder}/{dest.name}")
                    updates[ref["ref_id"]] = {"status": register.VERIFIED, "link": link,
                                              "note": "link found by the web search - check it" if source == "search" else None}
                    got.append(code)
                    break
                dest.unlink(missing_ok=True)
                bad.append(link)
                last = ("WRONG URL - " if wrong else "NOT INSERTED - ") + why[:200]
                store.set(ref["ref_id"], link=link, link_status="BAD", status="LINK_BAD", note=why[:300])
                log(f"WEB SCRAPING {ref['ref_id']}: {link} refused - {why}")
                link = ""
            if ref["ref_id"] not in got:
                store.set(ref["ref_id"], status="NOT_FOUND" if not bad else "LINK_BAD",
                          note="no official link found" if not bad else f"{len(bad)} wrong link(s)")
                if source == "register":
                    updates[ref["ref_id"]] = {"status": last}
                    flow(f"  {ref['ref_id']:<16} {last[:110]} - fix the link in {register.path(cfg).name}")
                elif skipped:                  # the search did not run (no model / no network): not the link's fault
                    updates[ref["ref_id"]] = {"status": register.NO_LINK + " - search could not run"}
                    flow(f"  {ref['ref_id']:<16} stays {'LINK_BAD' if bad else 'PENDING'} - the search runs again next time")
                else:
                    updates[ref["ref_id"]] = {"status": (last if bad else register.NO_LINK + " - search found nothing"),
                                              "note": ("wrong links tried: " + "; ".join(bad))[:480] if bad else None}
                    flow(f"  {ref['ref_id']:<16} {'NOT FOUND' if not bad else 'LINK BAD'} - write the link in "
                         f"{register.path(cfg).name}")
    finally:
        register.write(cfg, updates)
        conn.close()
    if got and then_run:
        flow(f"WEB SCRAPING : {len(got)} new document(s) go through IKP -> GKP now")
        import main as law_arc
        kp_settings.load(fresh=True)   # 2026-11-22: main.py must see the PDFs just verified (a stale copy said "missing"
                                       # and downloaded an old url over the verified ACT_31_2018.pdf)
        law_arc.main(["--documents", ",".join(got)])
        conn = get_connection()
        try:
            _sync_inserted(RefStore(conn, get_db_schema(), rc.get("document_table", "ref_document"),
                                    rc.get("link_table", "mkp_ref")), cfg, register.load(cfg))
        finally:
            conn.close()
    elif got:
        flow("WEB SCRAPING : run  python main.py --documents " + ",".join(got) + "  to push them")
    return got


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LAW ARC web scraping of cross-referenced notifications / circulars")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--run", action="store_true", help="push the documents it got through the pipeline")
    a = ap.parse_args()
    run(a.limit, a.run)
