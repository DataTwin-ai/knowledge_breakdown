"""AKP / reading model answers (same rules as the original engine)."""
from __future__ import annotations

import json
import re
from typing import Dict, List

from transport import get_field

COLS = ["MKP", "TYPE", "ELEMENT", "CONCEPT", "RESULTS"]
TYPES = {"MKP", "LE", "OR", "G", "PD", "A", "A_REF", "CT", "FACTS", "LK", "SP", "LW", "TC", "SUM"}


def rfield(row: dict, name: str) -> str:
    """A key from RESULTS only (get_field would return the TYPE / ELEMENT column for 'type' / 'element')."""
    from transport import parse_results
    return str(parse_results(row.get("RESULTS", "")).get(name.lower(), "")).strip()


def family(row: dict) -> str:
    return get_field(row, "family").strip()


def field_list(row: dict, name: str) -> List[str]:
    """'[A_X, A_Y]' -> ['A_X', 'A_Y']"""
    value = get_field(row, name).strip()
    value = value.strip("[]")
    return [v.strip().strip('"\'') for v in value.split(",") if v.strip().strip('"\'')]


def parse_rows(raw: str, mkp_id: str) -> List[dict]:
    """Five-column element table, tolerating harmless Markdown wrappers (original _parse_rows)."""
    text = (raw or "").strip()
    text = re.sub(r"^```[^\n]*\n|\n```\s*$", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = "|".join(COLS)

    def cells_of(line):
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            line = line[1:-1]
        return [cell.strip() for cell in line.split("|")]

    start = next((i for i, line in enumerate(lines) if "|".join(cells_of(line)).replace(" ", "").upper() == header), None)
    if start is not None:
        data_lines = lines[start + 1:]
    else:
        data_lines = lines
        if not data_lines or not all(len(cells_of(line)) == 5 and cells_of(line)[0] in (mkp_id, "TXN") for line in data_lines):
            raise ValueError("Missing MKP|TYPE|ELEMENT|CONCEPT|RESULTS header")
    rows = []
    for line in data_lines:
        cells = cells_of(line)
        if len(cells) == 5 and all(set(c.replace(" ", "")) <= set("-:") for c in cells):
            continue
        if len(cells) != 5:
            raise ValueError(f"Expected five columns: {line[:160]}")
        row = dict(zip(COLS, cells))
        if row["TYPE"] not in TYPES or row["MKP"] not in (mkp_id, "TXN"):
            raise ValueError(f"Invalid row TYPE/owner {row['TYPE']}/{row['MKP']}: {line[:160]}")
        if row["TYPE"] == "A" and len(row["CONCEPT"].split()) not in (2, 3):
            row["CONCEPT"] = concept_words(row["CONCEPT"], row["RESULTS"]) or row["CONCEPT"]   # 2026-11-27: Python repair
        if row["TYPE"] == "A" and len(row["CONCEPT"].split()) not in (2, 3):
            raise ValueError(f"Atom concept must be 2-3 words: {row['ELEMENT']}")
        if row["TYPE"] == "A" and not family(row):
            raise ValueError(f"Atom needs family in RESULTS: {row['ELEMENT']}")
        if not row["ELEMENT"] or not row["RESULTS"]:
            raise ValueError(f"Empty ELEMENT or RESULTS: {line[:160]}")
        rows.append(row)
    if not rows or not any(r["TYPE"] == "MKP" for r in rows):
        raise ValueError("MKP framing row missing")
    return rows


def parse_verdict(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Comparison did not return JSON")
    result = json.loads(text[start:end + 1])
    if result.get("verdict") not in ("TRUE", "FALSE"):
        raise ValueError("Comparison verdict must be TRUE or FALSE")
    for k in ("missing", "added_or_changed", "logic_problems", "same_matches", "references"):
        if not isinstance(result.get(k, []), list):
            raise ValueError(f"Comparison field {k} must be a JSON array")
        result.setdefault(k, [])
    return result


def apply_python_review(verdict: dict, findings: List[str]) -> dict:
    """The comparison model's answer on each Python word / number finding. A finding counts as REAL unless the model
    said PYTHON_WRONG for its id; TRUE stands only when no finding is REAL."""
    review = {}
    for r in verdict.get("python_findings_review") or []:
        if isinstance(r, dict):
            try:
                review[int(r.get("id"))] = (str(r.get("decision", "")).upper(), str(r.get("reason", "")))
            except (TypeError, ValueError):
                continue
    real, wrong = [], []
    for i, f in enumerate(findings, 1):
        decision, reason = review.get(i, ("", ""))
        (wrong if decision == "PYTHON_WRONG" else real).append(f + (f" (model: {reason})" if reason else ""))
    verdict["python_findings_rejected"] = wrong
    verdict["python_findings_real"] = real
    if real and verdict.get("verdict") == "TRUE":
        verdict["verdict"] = "FALSE"
        for f in real:
            key = "missing" if f.startswith(("MISSING", "NUMBER LOST", "QUALIFIER")) else "added_or_changed"
            verdict.setdefault(key, []).append(f)
    return verdict


def retry_text(verdict: dict, attempt: int, last_table: str) -> str:
    """What the AKP generator reads on a retry (prompt section RETRY): last time's errors and its rejected table."""
    lines = [f"- {x}" for k in ("missing", "added_or_changed", "logic_problems") for x in verdict.get(k) or []]
    if not lines:
        lines = ["- the comparison said the atoms do not mean the same as the MKP: "
                 + str(verdict.get("reconstructed_mkp") or "no reason given")[:400]]
    out = f"\n\nERRORS FROM LAST TIME (attempt {attempt}) - fix every one, keep everything that was right:\n" + \
        "\n".join(lines)
    if last_table:
        out += "\n\nYOUR TABLE LAST TIME (rejected):\n" + last_table
    return out


def table_text(rows: List[dict]) -> str:
    return "MKP|TYPE|ELEMENT|CONCEPT|RESULTS\n" + "\n".join("|".join(r.get(c, "") for c in COLS) for r in rows)


def raw_rows(raw: str, mkp_id: str) -> List[dict]:
    """Best-effort candidate rows for Excel (never blocks judging)."""
    try:
        return parse_rows(raw, mkp_id)
    except (ValueError, TypeError):
        raw = raw or ""
        return [{"MKP": mkp_id, "TYPE": "RAW", "ELEMENT": f"RAW_{i + 1}", "CONCEPT": "",
                 "RESULTS": raw[i * 30000:(i + 1) * 30000]} for i in range((len(raw) + 29999) // 30000)]

_CONCEPT_SKIP = {"A", "AN", "THE", "OF", "TO", "BY", "IN", "ON", "FOR", "AND", "OR", "IS", "ARE", "BE", "THAT", "WHICH",
                 "THIS", "ITS", "AS", "AT", "WITH", "FROM", "UNDER", "SUCH", "ANY", "EVERY"}


def concept_words(concept: str, results: str = "") -> str:
    """2026-11-27: the 2-3 word CONCEPT of an atom, repaired by Python when the model wrote an id or a phrase.
    A_RETURN_S44_NOT_FURNISHED -> 'Return Not Furnished'; one word -> the first content words of the statement.
    Returns '' when nothing usable is found (the row is then refused as before)."""
    words = [w for w in re.split(r"[\s_\-]+", str(concept or "").strip()) if w]
    if words and re.fullmatch(r"(A|G|LE|PD|OR|CT|TC|SP|M)", words[0], re.I) and "_" in str(concept):
        words = words[1:]
    words = [w for w in words if not re.search(r"\d", w) and w.upper() not in _CONCEPT_SKIP]
    if len(words) < 2:
        m = re.search(r"statement\s*:\s*\"?([^\";]*)", str(results or ""), re.I)
        st = [w for w in re.findall(r"[A-Za-z]+", m.group(1) if m else "") if w.upper() not in _CONCEPT_SKIP]
        words = (words + st)[:3]
    words = words[:3]
    return " ".join(w.capitalize() for w in words) if len(words) >= 2 else ""

