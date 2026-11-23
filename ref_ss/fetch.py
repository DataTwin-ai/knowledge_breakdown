"""Cross references / downloads (only with permission.json access.internet).

download_missing(cfg)  at the start of main.py: a document listed in Config/documents.json with a "url" whose PDF
                       is not in its Law folder is downloaded (features.download_missing_pdfs).
fetch_pdf(url, ...)    one download: HTTP 200, the bytes really are a PDF, size limit.
verify_pdf(...)        the downloaded file really is the cited notification / circular: its number and year are in
                       the first pages (Python, PyMuPDF), so a wrong link is never pushed.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import kp_settings
from kp_console import flow, log

USER_AGENT = "LAW-ARC/1.0 (document download)"
MAX_BYTES = 60 * 1024 * 1024


class FetchError(RuntimeError):
    pass


def fetch_pdf(url: str, timeout: int = 60, allowed_hosts: Optional[Iterable[str]] = None) -> bytes:
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError(f"not a web link: {url}")
    if allowed_hosts and parts.hostname not in set(allowed_hosts):
        raise FetchError(f"host {parts.hostname} is not in refs.allowed_hosts (Config/settings.json)")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                raise FetchError(f"HTTP {resp.status}")
            data = resp.read(MAX_BYTES + 1)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise FetchError("file is larger than 60 MB")
    if not data.startswith(b"%PDF"):
        raise FetchError("the link did not return a PDF")
    return data


def check_link(url: str, timeout: int = 30, allowed_hosts: Optional[Iterable[str]] = None) -> Tuple[bool, str]:
    """Does the link work? An official host, HTTP 200, and the first bytes are a PDF (only the start is read)."""
    parts = urllib.parse.urlparse(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, f"not a web link: {url}"
    if allowed_hosts and parts.hostname not in set(allowed_hosts):
        return False, f"host {parts.hostname} is not in refs.allowed_hosts"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*",
                                               "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status not in (200, 206):
                return False, f"HTTP {status}"
            head = resp.read(1024)
    except Exception as exc:
        return False, f"does not open - {type(exc).__name__}: {str(exc)[:120]}"
    if not head.startswith(b"%PDF"):
        return False, "opens, but it is not a PDF (a web page?)"
    return True, "opens, PDF"


def pdf_links_in_page(url: str, timeout: int = 30, allowed_hosts: Optional[Iterable[str]] = None,
                      words: Iterable[str] = ()) -> List[str]:
    """The PDF links on an official web page (a page that lists the document), best first: the links whose
    address or text has the document's number / year come first. Only official hosts; nothing is downloaded."""
    parts = urllib.parse.urlparse(url or "")
    hosts = set(allowed_hosts or [])
    if parts.scheme not in ("http", "https") or (hosts and parts.hostname not in hosts):
        return []
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(3_000_000).decode("utf-8", "replace")
    except Exception:
        return []
    words = [str(w).lower() for w in words if str(w).strip()]
    found = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.I | re.S):
        link = urllib.parse.urljoin(url, m.group(1).strip())
        if ".pdf" not in link.lower() or (hosts and urllib.parse.urlparse(link).hostname not in hosts):
            continue
        text = (link + " " + re.sub(r"<[^>]+>", " ", m.group(2))).lower()
        found.append((sum(w in text for w in words), link))
    found.sort(key=lambda x: -x[0])
    return list(dict.fromkeys(link for _, link in found))


def first_pages_text(path: Path, pages: int = 2) -> str:
    try:
        import pymupdf
    except ImportError:          # older PyMuPDF
        import fitz as pymupdf
    doc = pymupdf.open(str(path))
    try:
        return "\n".join(doc[i].get_text() for i in range(min(pages, len(doc))))
    finally:
        doc.close()


_TITLE_SKIP = {"the", "and", "of", "act", "rules", "section", "rule", "for", "to", "on", "in", "with", "tax", "goods",
               "services"}


def title_words(title: str) -> List[str]:
    """The words that name an Act / Rules: 'section 8 of the Integrated Goods and Services Tax Act' -> ['integrated'];
    'The Cost and Works Accountants Act, 1959' -> ['cost', 'works', 'accountants']."""
    name = re.sub(r"(?i)^.*?\bof\s+the\s+", "", title or "")
    return [w for w in re.findall(r"[a-z]{4,}", name.lower()) if w not in _TITLE_SKIP]


def verify_pdf(path: Path, number: Optional[int], year: Optional[int], kind: str, title: str = "") -> Tuple[bool, str]:
    """The number and year of the notification / circular must be in its first pages. 2026-11-22: an Act / Rules
    (GST or another Act) has no number to check: the words of its name (and its year, when given) must be there."""
    if kind in ("ACT", "RULES", "OTHER_ACT"):
        try:
            text = " ".join(first_pages_text(path, 3).split()).lower()
        except Exception as exc:
            return False, f"could not read the PDF ({type(exc).__name__})"
        words = title_words(title)
        missing = [w for w in words if w not in text]
        if words and len(missing) > len(words) // 3:
            return False, f"the name is not in the first pages (missing: {', '.join(missing[:5])})"
        if kind == "OTHER_ACT" and year and str(year) not in text:
            return False, f"the year {year} is not in the first pages"
        return True, "the name is in the first pages"
    if not number or not year:
        return True, "no number / year to check"
    try:
        text = first_pages_text(path)
    except Exception as exc:
        return False, f"could not read the PDF ({type(exc).__name__})"
    text = " ".join(text.split())
    if kind == "AMENDING_ACT":                        # "No. 31 of 2018"
        pat = rf"(?<!\d){int(number)}\s+of\s+{int(year)}(?!\d)"
        if not re.search(pat, text, re.I):
            return False, f"Act {number} of {year} is not in the first pages"
        # bug list C13: the updated principal Act (200+ pages) names "Act 31 of 2018" in its footnotes - the
        # amending Act names its own number in its TITLE, at the top of page 1
        try:
            head = " ".join(first_pages_text(path, 1).split())[:1500]
        except Exception:
            head = text[:1500]
        if not re.search(pat, head, re.I):
            return False, (f"Act {number} of {year} is only in later text (footnotes), not in the title on page 1 - "
                           f"probably the principal Act as amended, not the amending Act")
        return True, "number and year found in the title"
    pat = rf"(?<!\d)0?{int(number)}\s*/\s*(?:\d+\s*/\s*)?{int(year)}(?!\d)"
    if not re.search(pat, text):
        return False, f"{kind.lower()} {number}/{year} is not in the first pages"
    return True, "number and year found"


def download_missing(cfg: dict) -> bool:
    """Download the PDFs listed in Config/documents.json (with a url) that are missing. True when any came."""
    if not cfg["features"]["download_missing_pdfs"]:
        return False
    paths = kp_settings.build_paths(cfg)
    got = False
    for d in kp_settings.selected_documents(cfg):
        if not d.get("missing"):
            continue
        dest = Path(paths["law_dir"]) / d["file"]
        if not d.get("url"):
            flow(f"DOWNLOAD  : {d['code']} has no PDF in Law/ and no url in Config/documents.json - skipped")
            continue
        if not kp_settings.allowed(cfg, "INTERNET"):
            flow(f"DOWNLOAD  : {d['code']} not downloaded - permission.json access.internet is false")
            continue
        kp_settings.require(cfg, "WRITE", dest)
        try:
            data = fetch_pdf(d["url"], int((cfg.get("refs") or {}).get("request_timeout_seconds", 60)))
        except FetchError as exc:
            flow(f"DOWNLOAD  : {d['code']} FAILED - {exc}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        flow(f"DOWNLOAD  : {d['name']} ({d['code']}) -> Law/{d['file']}")
        log(f"DOWNLOAD {d['code']} from {d['url']} ({len(data)} bytes)")
        got = True
    return got
