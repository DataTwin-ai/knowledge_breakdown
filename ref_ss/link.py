"""Cross references / the official link of an outside document, found while the law is split (MKP).

When MKP records a reference to a notification, circular or amending Act that has no link yet, the link finder
looks for it ONCE per document (ref_document has one row per document, however many laws cite it):

  1. search  the ref_link model (permission.json llm.ref_link, Claude Haiku) with Claude's web search, only on
             the official sites (Config/settings.json refs.allowed_hosts)
  2. check   Python, no model: the link it gives, then the PDF links among the search results (best match of
             number / year first): official host, it opens (HTTP 200) and is a PDF (ref_ss/fetch.check_link).
             A web PAGE that lists the document (the model's "page", or a result page) is opened by Python and
             its PDF links are checked the same way (ref_ss/fetch.pdf_links_in_page).
  3. retry   only when 1-2 found nothing: the stronger ref_link_retry model (permission.json llm.ref_link_retry,
             e.g. Claude Sonnet) searches once more (refs.link_retry_max_uses), told which links were wrong.
  4. save    ref_document.link + link_status:
               OK         status LINK_FOUND  -> ref_engine.py downloads it, checks its number / year and content
               BAD        status LINK_BAD    -> ref_engine.py looks again (the wrong links are in note)
               NOT_FOUND  status PENDING     -> ref_engine.py looks again

Needs access.internet = true and refs.link_on_split = true; otherwise nothing is searched (said once). A search
that could not run (Claude down, no network) leaves the row as it was, so the next split tries again. The MKP
is never stopped by a link.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from kp_console import flow, log

KINDS = ("NOTIFICATION", "CIRCULAR", "AMENDING_ACT")
DONE = ("LINK_FOUND", "DOWNLOADED", "VERIFIED", "QUEUED", "INSERTED")

SEARCH_PROMPT = """Find the official PDF of this Indian GST document with the web search.
Document: {title}
Kind: {kind}   Number: {number}   Year: {year}   Series: {series}
Search only official sites: e-Gazette (egazette.gov.in), India Code (indiacode.nic.in), the Legislative
Department (legislative.gov.in), CBIC (cbic-gst.gov.in, cbic.gov.in, taxinformation.cbic.gov.in), GST Council.
An Act of Parliament is on India Code / the Legislative Department / the e-Gazette; a notification or circular
is on CBIC / the e-Gazette.
{previous}Answer with ONE JSON object only:
{{"url": "<the DIRECT link to the PDF file, as it appears in the search results, or empty>",
  "page": "<an official web page that lists or links this document, or empty>",
  "title": "<official title>"}}
If the search did not show this exact document, leave both empty. Never guess or build a link."""

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_said = set()


def _lock(ref_id: str) -> threading.Lock:
    """One lock per document: two workers never search for the same document."""
    with _locks_guard:
        return _locks.setdefault(ref_id, threading.Lock())


def needs_link(row: Optional[dict]) -> bool:
    """A document is searched only once: no link, never tried (link_status empty), not fetched yet."""
    return bool(row) and row["kind"] in KINDS and not row.get("link") and not row.get("link_status") \
        and row.get("status") not in DONE and not row.get("doc_code")


def parse_answer(text: str) -> Dict[str, str]:
    t = (text or "").strip()
    try:
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1] or "{}")
    except ValueError:
        obj = {}
    return {k: str(obj.get(k) or "").strip() for k in ("url", "page", "title")}


def claude_search(cfg: dict, key: str = "link_finder") -> Callable:
    """search(prompt, allowed_domains, max_uses) -> (answer text, result links) with one Claude model."""
    role = (cfg.get("refs") or {}).get(key)
    if not role:
        raise RuntimeError(f"permission.json llm.{'ref_link' if key == 'link_finder' else 'ref_link_retry'} is missing")
    import kp_settings
    from llm_runtime import call_claude_search, create_llm_runtime
    rt = dict(create_llm_runtime(role["llm"], kp_settings.build_paths(cfg)))
    rt["model"] = role["model"]
    return lambda prompt, domains, uses: call_claude_search(rt, prompt, domains, uses)


def searchers(cfg: dict) -> List[Tuple[str, Callable, int]]:
    """The models that search, cheapest first: ref_link (refs.link_search_max_uses searches), then - only when it
    found nothing - ref_link_retry (refs.link_retry_max_uses), when permission.json gives one."""
    rc = cfg.get("refs") or {}
    out = [("ref_link", claude_search(cfg), int(rc["link_search_max_uses"]))]
    if rc.get("link_retry"):
        out.append(("ref_link_retry", claude_search(cfg, "link_retry"), int(rc["link_retry_max_uses"])))
    return out


def _words(ref: dict) -> List[str]:
    return [str(x) for x in (ref.get("number"), ref.get("year")) if x]


def _best(links: Iterable[str], words: List[str], pdf: bool) -> List[str]:
    """Search result links ranked by how many of the document's number / year they contain."""
    ls = [l for l in dict.fromkeys(links) if (".pdf" in l.lower()) == pdf]
    return sorted(ls, key=lambda l: -sum(w in l for w in words))


def find(cfg: dict, ref: dict, search_with: List[Tuple[str, Callable, int]], check: Callable,
         follow: Optional[Callable] = None, previous: Iterable[str] = (), max_checks: int = 8) -> dict:
    """Search + check one document, cheapest model first. Returns the ref_document fields to save."""
    rc = cfg.get("refs") or {}
    hosts = list(rc.get("allowed_hosts") or [])
    timeout = int(rc.get("request_timeout_seconds", 60))
    words = _words(ref)
    if follow is None:
        from ref_ss.fetch import pdf_links_in_page
        follow = pdf_links_in_page
    tried: Dict[str, str] = {u: "wrong before" for u in previous if u}
    for label, search, uses in search_with:
        prev = ("These links were already tried and are wrong (not this document, or not a PDF): "
                + "; ".join(list(tried)[:8]) + "\n") if tried else ""
        prompt = SEARCH_PROMPT.format(title=ref.get("title") or ref["ref_id"], kind=ref["kind"],
                                      number=ref.get("number") or "", year=ref.get("year") or "",
                                      series=ref.get("series") or "", previous=prev)
        text, seen = search(prompt, hosts, uses)
        ans = parse_answer(text)
        checks = 0

        def try_links(links, how):
            nonlocal checks
            for u in links:
                if not u or u in tried or checks >= max_checks:
                    continue
                checks += 1
                ok, why = check(u, timeout, hosts)
                if ok:
                    return {"link": u, "link_status": "OK", "status": "LINK_FOUND",
                            "note": f"{label} web search while splitting ({how}): {why}"[:300]}
                tried[u] = why
            return None
        got = try_links([ans["url"]], "its link") or try_links(_best(seen, words, True), "a search result")
        if got:
            return got
        pages = [p for p in [ans["page"], ans["url"]] + _best(seen, words, False)[:3] if p]
        for page in dict.fromkeys(pages):        # a page that lists the document: Python opens it
            got = try_links(follow(page, timeout, hosts, words)[:5], f"found on {page}"[:120])
            if got:
                return got
    if tried and any(v != "wrong before" for v in tried.values()):
        bad = "; ".join(f"{u} - {w}" for u, w in tried.items() if w != "wrong before")
        return {"link": "", "link_status": "BAD", "status": "LINK_BAD",
                "note": f"web search while splitting: no link works - {bad}"[:300]}
    return {"link": "", "link_status": "NOT_FOUND", "status": "PENDING",
            "note": "web search while splitting: no official link found"}


def on_split(cfg: dict, store, ref_ids: Iterable[str], search: Optional[Callable] = None,
             check: Optional[Callable] = None, follow: Optional[Callable] = None) -> int:
    """Find the link of every document in ref_ids that has none yet. Returns how many were searched."""
    rc = cfg.get("refs") or {}
    ids = sorted(set(ref_ids or []))
    if not ids or not rc.get("link_on_split"):
        return 0
    import kp_settings
    if not kp_settings.allowed(cfg, "INTERNET"):
        if "internet" not in _said:
            _said.add("internet")
            flow("LINKS      : not searched while splitting - permission.json access.internet is false")
        return 0
    todo = [r for r in store.link_state(ids) if needs_link(r)]
    if not todo:
        return 0
    if check is None:
        from ref_ss.fetch import check_link
        check = check_link
    done = 0
    for ref in todo:
        with _lock(ref["ref_id"]):
            fresh = next(iter(store.link_state([ref["ref_id"]])), None)
            if not needs_link(fresh):               # another worker found it meanwhile
                continue
            try:
                with_ = ([("test", search, int(rc["link_search_max_uses"]))] if search else searchers(cfg))
                got = find(cfg, fresh, with_, check, follow)
            except Exception as exc:                # not saved: the next split tries again
                from llm_runtime_errors import is_fatal
                log(f"LINK {ref['ref_id']}: search not done - {type(exc).__name__}: {str(exc)[:160]}")
                if is_fatal(exc) and "fatal" not in _said:
                    _said.add("fatal")
                    flow(f"LINKS      : web search unavailable ({str(exc)[:100]}) - ref_engine.py will find the links")
                continue
            store.set(ref["ref_id"], **got)
            done += 1
            flow(f"LINK       : {ref['ref_id']:<16} {got['link_status']:<9} {got['link'] or got['note'][:90]}")
    return done
