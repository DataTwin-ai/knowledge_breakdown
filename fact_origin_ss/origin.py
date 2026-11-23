"""Fact origin - the prompt, the AI answer check (Python) and the table (2026-11-23, new feature, no change to AKP).

One row per fact in fact_origin (fact_id = akp_fact_new.fact_id):
    document      where the value starts (a key of Config/fact_origin.json documents)
    method        FIELD (printed on the document) / DERIVED (worked out from other facts) / LOOKUP (a key from the
                  document, looked up in a register / API) / READ (an AI reads free text on the document) / ASK
    how_to_get    step by step, in plain words, how to get the value from what we have
    uses          the document fields (DOC.field) / facts (F_...) it is built from
    as_of         the date the value must be true on
    level         one value per INVOICE / LINE_ITEM / RETURN / TAXPAYER / TRANSACTION / PERIOD / DOCUMENT
    fallback      what to do when the main way fails (another document, or the question to ask)
    when_missing  ASK / UNKNOWN (the law is skipped) / STOP
    cost          LOW / MEDIUM / HIGH - how hard the value is to get (cheap facts first)
    ai_level      NONE / LOW / HIGH - which AI the engine needs to GET the fact (not to check the law)
    example, confidence (high / medium / low), sensitive (personal data), origin_by (AI / PERSON), reviewed, note
How to CHECK a law (e.g. "filed after the due date") is not here - it stays in the law's atoms / the rulebook yaml,
because one fact is reused by many laws.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

METHODS = ("FIELD", "DERIVED", "LOOKUP", "READ", "ASK")
LEVELS = ("INVOICE", "LINE_ITEM", "RETURN", "TAXPAYER", "TRANSACTION", "PERIOD", "DOCUMENT")
MISSING = ("ASK", "UNKNOWN", "STOP")
COSTS = ("LOW", "MEDIUM", "HIGH")
AI_LEVELS = ("NONE", "LOW", "HIGH")
CONFIDENCE = ("high", "medium", "low")
COLUMNS = ["fact_id", "document", "method", "how_to_get", "uses", "as_of", "level", "fallback", "when_missing",
           "cost", "ai_level", "example", "confidence", "sensitive", "origin_by", "reviewed", "note", "model"]

PROMPT = """You decide WHERE the value of each fact comes from when an Indian GST document is validated, and HOW to
get it. A fact is one piece of data a law needs (a date, a status, a type). Do not explain the law and do not say how
the law checks the fact - only how to GET the fact's value.

The documents we can get, with their fields (use only these names):
{documents}

Methods:
  FIELD    the value is printed on the document - uses = "DOC.field"
  DERIVED  Python works it out from other facts / fields (date maths, a sum, a comparison of two fields) - uses = the
           facts (F_...) and / or fields (DOC.field) it is built from
  LOOKUP   take a key from a document (a GSTIN, an HSN) and look it up in a register / API / table (Python, no AI)
  READ     an AI reads free text on the document (a line description, a contract) and decides the value
  ASK      no document has it: ask the user - document = USER, how_to_get = the question, in plain words
Notifications / circulars are looked up on our side (document NOTIFICATION, never asked). The meaning of a term in
the law itself is document SYSTEM.

For each fact answer:
  document      one of: {doc_names}
  method        FIELD / DERIVED / LOOKUP / READ / ASK
  how_to_get    step by step, plain words, enough for an engine to follow (which document, which field, what to do
                with it, what the value is when ...)
  uses          comma separated DOC.field / F_... (empty when none)
  as_of         the date the value must be true on (e.g. "invoice date", "end of the tax period", "date of supply")
  level         INVOICE / LINE_ITEM / RETURN / TAXPAYER / TRANSACTION / PERIOD / DOCUMENT
  fallback      what to do when the main way fails: another document, or the question to ask the user
  when_missing  ASK / UNKNOWN / STOP
  cost          LOW (read off a document) / MEDIUM (lookup, two documents) / HIGH (ask the user, or AI reading)
  ai_level      NONE (Python only) / LOW (small model, simple reading) / HIGH (strong model, understanding free text)
  example       one sample value
  confidence    high / medium / low
  sensitive     true for personal data (Aadhaar, PAN, bank account, phone, address of a person), else false
  note          one short sentence: why this document

Facts (id | name | entity | data type | allowed values | derived from | description | laws that use it):
{facts}

Answer with ONE JSON array only, one object per fact, in the same order:
[{{"fact_id": "...", "document": "...", "method": "...", "how_to_get": "...", "uses": "...", "as_of": "...",
   "level": "...", "fallback": "...", "when_missing": "...", "cost": "...", "ai_level": "...", "example": "...",
   "confidence": "...", "sensitive": false, "note": "..."}}]"""


def documents_text(documents: Dict[str, dict]) -> str:
    out = []
    for name, d in documents.items():
        if name.startswith("_"):
            continue
        fields = ", ".join(d.get("fields") or []) or "(no fields)"
        out.append(f"  {name}: {d.get('about', '')}\n      fields: {fields}")
    return "\n".join(out)


def fact_line(f: dict, laws: List[dict]) -> str:
    law_text = " || ".join(f"{l['mkp']} [{', '.join(l.get('tsgs') or []) or 'no TSG'}]: {l.get('text', '')[:300]}"
                           for l in laws) or "none"
    opts = ", ".join(str(o) for o in (f.get("options") or [])) or "-"
    derived = ", ".join(f.get("derived_from") or []) or "-"
    return (f"{f['fact_id']} | {f.get('name', '')} | {f.get('entity', '') or '-'} | {f.get('data_type', '') or '-'} | "
            f"{opts} | {derived} | {(f.get('description') or '')[:200] or '-'} | {law_text}")


def build_prompt(documents: Dict[str, dict], facts: List[dict], laws: Dict[str, List[dict]]) -> str:
    names = [n for n in documents if not n.startswith("_")]
    return PROMPT.format(documents=documents_text(documents), doc_names=", ".join(names),
                         facts="\n".join(fact_line(f, laws.get(f["fact_id"], [])) for f in facts))


def parse(text: str) -> List[dict]:
    t = (text or "").strip()
    start, end = t.find("["), t.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("the answer has no JSON array")
    data = json.loads(t[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("the answer is not a JSON array")
    return [d for d in data if isinstance(d, dict)]


def _pick(value, allowed, default):
    v = str(value or "").strip()
    for a in allowed:
        if v.upper() == a.upper():
            return a
    return default


def check(row: dict, documents: Dict[str, dict], fact_ids: set) -> Tuple[dict, List[str]]:
    """Python check of one AI answer. Returns (clean row, problems). A problem never drops the row: it lowers the
    confidence and is written in note, so the person sees it first in the review."""
    problems: List[str] = []
    out = {c: row.get(c) for c in COLUMNS}
    out["fact_id"] = str(row.get("fact_id") or "").strip()
    doc = str(row.get("document") or "").strip().upper()
    if doc not in documents:
        problems.append(f"document {doc or '(empty)'} is not in Config/fact_origin.json - set to USER")
        doc = "USER"
    out["document"] = doc
    out["method"] = _pick(row.get("method"), METHODS, "ASK")
    if out["method"] != str(row.get("method") or "").strip().upper():
        problems.append(f"method {row.get('method')!r} unknown - set to ASK")
    if out["method"] == "ASK" and doc not in ("USER", "PROFILE"):
        problems.append(f"method ASK but document {doc}")
    out["level"] = _pick(row.get("level"), LEVELS, "TRANSACTION")
    out["when_missing"] = _pick(row.get("when_missing"), MISSING, "ASK")
    out["cost"] = _pick(row.get("cost"), COSTS, "HIGH")
    out["ai_level"] = _pick(row.get("ai_level"), AI_LEVELS, "HIGH")
    out["confidence"] = _pick(row.get("confidence"), CONFIDENCE, "low")
    out["sensitive"] = str(row.get("sensitive")).strip().lower() in ("true", "1", "yes")
    for c in ("how_to_get", "as_of", "fallback", "example", "note"):
        out[c] = str(row.get(c) or "").strip()
    if not out["how_to_get"]:
        problems.append("how_to_get is empty")
    # uses: every DOC.field must be a real field, every F_... a real fact
    uses = [u.strip() for u in re.split(r"[,;]", str(row.get("uses") or "")) if u.strip()]
    for u in uses:
        if re.fullmatch(r"F_[A-Z0-9_]+", u):
            if fact_ids and u not in fact_ids:
                problems.append(f"uses {u}: no such fact")
        elif "." in u:
            d, field = u.split(".", 1)
            if d.upper() not in documents or field not in (documents[d.upper()].get("fields") or []):
                problems.append(f"uses {u}: no such document field")
        else:
            problems.append(f"uses {u}: write DOC.field or F_...")
    out["uses"] = ", ".join(uses)
    if out["method"] == "FIELD" and not any("." in u for u in uses):
        problems.append("method FIELD names no DOC.field in uses")
    if out["method"] == "DERIVED" and not uses:
        problems.append("method DERIVED names nothing in uses")
    if out["method"] in ("FIELD", "DERIVED", "LOOKUP") and out["ai_level"] != "NONE":
        problems.append(f"method {out['method']} usually needs no AI (ai_level {out['ai_level']})")
    if problems:
        out["confidence"] = "low"
        out["note"] = (out["note"] + " | CHECK: " + "; ".join(problems)).strip(" |")[:900]
    return out, problems
