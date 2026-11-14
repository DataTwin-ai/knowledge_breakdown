"""Cross references / one fixed ID for every outside document, made by Python (never by the LLM).

The LLM and the law write the same notification many ways: "Notification No. 4/2018-Central Tax (Rate)",
"notfn. 04/2018-CT(R)", "N.4/2018-Central Tax (Rate)". All of them become N_CTR_04_2018, so the MKP that cites it,
the cross-reference table and the notification's own document code always meet on the same ID.

    N_<series>_<NN>_<YYYY>   notification     series: CT, CTR, IT, ITR, UT, UTR, CC, CCR, ST, STR, X (not said)
    CIR_<NN>_<YYYY>          circular         (47/21/2018-GST -> CIR_47_2018; the middle number is a file number)
    ACT_<NN>_<YYYY>          amending Act     ("Act 31 of 2018")
    <DOC CODE>               an Act / Rules of the corpus (CGSTA17, CGSTR17, IGSTA17 ...)
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

SERIES = [  # longest first
    (r"central\s*tax\s*\(\s*rate\s*\)|ct\s*\(\s*r\s*\)", "CTR"),
    (r"integrated\s*tax\s*\(\s*rate\s*\)|it\s*\(\s*r\s*\)", "ITR"),
    (r"union\s*territory\s*tax\s*\(\s*rate\s*\)|ut\s*\(\s*r\s*\)", "UTR"),
    (r"compensation\s*cess\s*\(\s*rate\s*\)", "CCR"),
    (r"state\s*tax\s*\(\s*rate\s*\)", "STR"),
    (r"central\s*tax|\bct\b", "CT"),
    (r"integrated\s*tax|\bit\b", "IT"),
    (r"union\s*territory\s*tax|\but\b", "UT"),
    (r"compensation\s*cess", "CC"),
    (r"state\s*tax", "ST"),
]
NOTIF_RE = re.compile(r"(?:\bnotif\w*|\bnotfn|\bntfn|\bN(?=\.\s*\d{1,3}\s*/\s*(?:19|20)\d{2}\s*[-–]\s*[A-Za-z]))\.?\s*(?:no\.?|number)?\s*(?P<n>\d{1,3})\s*/\s*(?P<y>(?:19|20)\d{2})"
                      r"(?P<tail>\s*[-–]?\s*[A-Za-z ()]{0,40})?", re.I)
CIRC_RE = re.compile(r"\bcircular\s*(?:no\.?|number)?\s*(?P<n>\d{1,4})\s*/\s*(?:(?P<f>\d{1,4})\s*/\s*)?(?P<y>(?:19|20)\d{2})", re.I)
ACT_RE = re.compile(r"\bAct\b,?\s*(?:(?:19|20)\d{2}\s*)?\(?\s*(?:No\.?\s*)?(?P<n>\d{1,3})\s+of\s+(?P<y>(?:19|20)\d{2})", re.I)
CORPUS = [  # name in the law -> document code
    (r"central\s+goods\s+and\s+services\s+tax\s+rules|cgst\s+rules", "CGSTR17"),
    (r"integrated\s+goods\s+and\s+services\s+tax\s+rules|igst\s+rules", "IGSTR17"),
    (r"central\s+goods\s+and\s+services\s+tax\s+act|cgst\s+act", "CGSTA17"),
    (r"integrated\s+goods\s+and\s+services\s+tax\s+act|igst\s+act", "IGSTA17"),
    (r"union\s+territory\s+goods\s+and\s+services\s+tax\s+act|utgst\s+act", "UTGSTA17"),
    (r"goods\s+and\s+services\s+tax\s+\(compensation\s+to\s+states\)\s+act", "GSTCA17"),
]


def series_of(text: str) -> str:
    t = (text or "").lower()
    for pattern, code in SERIES:
        if re.search(pattern, t):
            return code
    return "X"


def notification_id(number, year, series: str = "X") -> str:
    return f"N_{series or 'X'}_{int(number):02d}_{int(year)}"


def circular_id(number, year) -> str:
    return f"CIR_{int(number):02d}_{int(year)}"


def parse(text: str) -> Optional[Tuple[str, str, dict]]:
    """First outside document named in `text` -> (ref_id, kind, details) or None."""
    m = NOTIF_RE.search(text or "")
    if m:
        series = series_of(m.group("tail") or "")
        return notification_id(m.group("n"), m.group("y"), series), "NOTIFICATION", {
            "number": int(m.group("n")), "year": int(m.group("y")), "series": series}
    m = CIRC_RE.search(text or "")
    if m:
        return circular_id(m.group("n"), m.group("y")), "CIRCULAR", {"number": int(m.group("n")), "year": int(m.group("y"))}
    m = ACT_RE.search(text or "")
    if m:
        return f"ACT_{int(m.group('n')):02d}_{int(m.group('y'))}", "AMENDING_ACT", {
            "number": int(m.group("n")), "year": int(m.group("y"))}
    return None


def corpus_code(text: str) -> Optional[str]:
    for pattern, code in CORPUS:
        if re.search(pattern, text or "", re.I):
            return code
    return None


def ref_id_for_document(name: str, doc_type: str, code: str) -> str:
    """The fixed ID of a document we hold, so a cross reference finds it: notification / circular by its name,
    Acts and Rules by their code."""
    t = str(doc_type or "").upper()
    if t in ("NOTIFICATION", "CIRCULAR"):
        found = parse(("notification " if t == "NOTIFICATION" and not re.search(r"notif", name, re.I) else "")
                      + ("circular " if t == "CIRCULAR" and not re.search(r"circular", name, re.I) else "") + name)
        if found:
            return found[0]
    return str(code).upper()
