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
from typing import Iterable, Optional, Tuple

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


def verify_pdf(path: Path, number: Optional[int], year: Optional[int], kind: str) -> Tuple[bool, str]:
    """The number and year of the notification / circular must be in its first pages."""
    if not number or not year:
        return True, "no number / year to check"
    try:
        text = first_pages_text(path)
    except Exception as exc:
        return False, f"could not read the PDF ({type(exc).__name__})"
    text = " ".join(text.split())
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
