"""The link register: Config/ref_links.xlsx - one row per outside document, kept by hand (2026-11-23).

    ref_id | kind | document name | link | status | checked on | note

The link is taken from here first, before any web search. ref_engine.py checks it and writes the status back:
    TO CHECK                    a link you wrote that was not checked yet
    INSERTED                    downloaded, checked, IKP'd and used by the law that cites it
    VERIFIED - waiting for IKP  downloaded and checked; the next main.py run IKPs it
    WRONG URL - <reason>        the link opens, but the AI / Python check says it is not this document
    NOT INSERTED - <reason>     the link does not open, is not a PDF, or its site is not in refs.allowed_hosts
    NO LINK                     the link cell is empty (refs.search_empty_links = true: searched once)
    FOUND BY SEARCH - check     the web search wrote this link; it is checked like any other
    LOCAL - already in Law/     the PDF is already in Law/ (Config/documents.json)
    SKIP - <why>                written by hand: never fetched (e.g. "SKIP - same as EXT_FA_1994")
The script never changes a link you wrote. Fix a wrong link in the Excel and run again: only the rows that are
not INSERTED / SKIP are checked again. New documents MKP finds are added as NO LINK rows.
"""
from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional

from kp_console import flow, log

COLUMNS = ["ref_id", "kind", "document name", "link", "status", "checked on", "note"]
WIDTHS = [20, 14, 60, 70, 45, 12, 50]
INSERTED, VERIFIED, NO_LINK, FOUND, LOCAL = ("INSERTED", "VERIFIED - waiting for IKP", "NO LINK",
                                             "FOUND BY SEARCH - check", "LOCAL - already in Law/")
_lock = threading.Lock()


def path(cfg: dict) -> Path:
    import kp_settings
    return kp_settings.resolve((cfg.get("refs") or {}).get("link_register") or "Config/ref_links.xlsx")


def done(row: Optional[dict]) -> bool:
    """INSERTED or SKIP rows are never checked again."""
    s = str((row or {}).get("status") or "").upper()
    return s.startswith("INSERTED") or s.startswith("SKIP")


def skipped(row: Optional[dict]) -> bool:
    return str((row or {}).get("status") or "").upper().startswith("SKIP")


def load(cfg: dict) -> Dict[str, dict]:
    """ref_id -> row (column name -> value). An Excel that is missing gives {}."""
    p = path(cfg)
    if not p.is_file():
        return {}
    from openpyxl import load_workbook
    ws = load_workbook(p, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}
    head = [str(h or "").strip().lower() for h in rows[0]]
    out = {}
    for r in rows[1:]:
        row = {h: ("" if v is None else str(v).strip()) for h, v in zip(head, r)}
        if row.get("ref_id"):
            out[row["ref_id"]] = row
    return out


def _new_book():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "links"
    ws.append(COLUMNS)
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        c = ws.cell(1, i)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    return wb


def write(cfg: dict, updates: Dict[str, dict]) -> bool:
    """Update (or add) rows: ref_id -> {column: value}. A link already written is changed only when the row had no
    link. 'checked on' is set to today for every updated row. False when the Excel is open in Excel (locked)."""
    if not updates:
        return True
    p = path(cfg)
    with _lock:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(p) if p.is_file() else _new_book()
            ws = wb.active
            head = [str(c.value or "").strip().lower() for c in ws[1]]
            for col in COLUMNS:                       # a column the user deleted comes back at the end
                if col not in head:
                    ws.cell(1, len(head) + 1, col)
                    head.append(col)
            idx = {h: i + 1 for i, h in enumerate(head)}
            at = {str(ws.cell(r, idx["ref_id"]).value or "").strip(): r for r in range(2, ws.max_row + 1)}
            today = _dt.date.today().isoformat()
            for ref_id, fields in updates.items():
                r = at.get(ref_id)
                if not r:
                    r = ws.max_row + 1
                    ws.cell(r, idx["ref_id"], ref_id)
                    at[ref_id] = r
                # never overwrite a link you wrote (only the search's own "FOUND BY SEARCH" link)
                keep_link = bool(str(ws.cell(r, idx["link"]).value or "").strip()) and not \
                    str(ws.cell(r, idx["status"]).value or "").upper().startswith("FOUND BY SEARCH")
                for col, val in fields.items():
                    if col not in idx or val is None:
                        continue
                    if col == "link" and keep_link:
                        continue
                    if col == "document name" and str(ws.cell(r, idx[col]).value or "").strip():
                        continue
                    ws.cell(r, idx[col], str(val)[:500])
                if "status" in fields:
                    ws.cell(r, idx["checked on"], today)
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(p)
            return True
        except PermissionError:
            flow(f"LINK REGISTER: {p.name} is open in Excel - close it; the status is written next run")
            return False
        except Exception as exc:                      # the register never stops a run
            log(f"LINK REGISTER: could not write {p} - {type(exc).__name__}: {exc}")
            return False


def add_new(cfg: dict, refs: Iterable[dict], have: Optional[Dict[str, dict]] = None) -> int:
    """Rows for documents the register does not have yet (status NO LINK; the name from ref_document)."""
    have = load(cfg) if have is None else have
    new = {r["ref_id"]: {"kind": r.get("kind") or "", "document name": r.get("title") or "", "status": NO_LINK}
           for r in refs if r.get("ref_id") and r["ref_id"] not in have}
    if new and write(cfg, new):
        flow(f"LINK REGISTER: {len(new)} new document(s) added to {path(cfg).name} - fill their links")
        return len(new)
    return 0
