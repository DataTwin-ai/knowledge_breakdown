"""Fact origin: prompt, Python check of the AI answer, and a run with a fake AI / database (no network)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fact_origin_ss import origin as fo  # noqa: E402

DOCS = {"SALE_INVOICE": {"about": "invoice", "fields": ["supplier_gstin", "invoice_date"]},
        "GST_REGISTRATION": {"about": "reg", "fields": ["sez_unit"]},
        "GST_RETURN": {"about": "ret", "fields": ["ret_period", "arn_date"]}, "USER": {"fields": []}}
GOOD = {"fact_id": "F_SSS", "document": "gst_registration", "method": "lookup",
        "how_to_get": "Take SALE_INVOICE.supplier_gstin, look it up; SEZ unit -> YES", "uses": "SALE_INVOICE.supplier_gstin",
        "as_of": "invoice date", "level": "TAXPAYER", "fallback": "ask for REG-06", "when_missing": "ASK",
        "cost": "MEDIUM", "ai_level": "NONE", "example": "YES", "confidence": "high", "sensitive": False, "note": "x"}


def test_good_answer_passes():
    row, problems = fo.check(GOOD, DOCS, {"F_SSS"})
    assert problems == [] and row["document"] == "GST_REGISTRATION" and row["method"] == "LOOKUP"
    assert row["confidence"] == "high" and row["sensitive"] is False


def test_unknown_document_field_and_fact_are_flagged():
    bad = dict(GOOD, document="BANK", uses="SALE_INVOICE.colour, F_NOPE", method="FIELD", ai_level="HIGH")
    row, problems = fo.check(bad, DOCS, {"F_SSS"})
    assert row["document"] == "USER" and row["confidence"] == "low" and "CHECK:" in row["note"]
    assert any("BANK" in p for p in problems) and any("colour" in p for p in problems)
    assert any("F_NOPE" in p for p in problems) and any("usually needs no AI" in p for p in problems)


def test_derived_needs_uses_and_prompt_lists_documents():
    _, problems = fo.check(dict(GOOD, method="DERIVED", uses=""), DOCS, set())
    assert any("DERIVED names nothing" in p for p in problems)
    prompt = fo.build_prompt(DOCS, [{"fact_id": "F_SSS", "name": "Supplier SEZ Status"}],
                             {"F_SSS": [{"mkp": "IGSTA17_S8_SS2_P1", "tsgs": ["TSG14_06"], "text": "supply to SEZ"}]})
    assert "SALE_INVOICE" in prompt and "supplier_gstin" in prompt and "F_SSS | Supplier SEZ Status" in prompt
    assert "TSG14_06" in prompt


def test_parse_takes_the_json_array():
    assert fo.parse("Here:\n" + json.dumps([GOOD]) + "\nthanks")[0]["fact_id"] == "F_SSS"


def test_run_with_fake_ai_and_store(monkeypatch):
    import types
    import fact_origin_engine as eng
    import kp_settings
    saved = []

    class Store:
        def __init__(self, *a): pass
        def ensure(self): pass
        def rows(self): return {"F_DONE": {"reviewed": True}}
        def save(self, rows, overwrite_reviewed=False): saved.extend(rows); return len(rows)
    monkeypatch.setattr("fact_origin_ss.store.OriginStore", Store)
    db = types.ModuleType("db_insert")
    db.get_connection = lambda: types.SimpleNamespace(close=lambda: None)
    db.get_db_schema = lambda: "s"
    monkeypatch.setitem(sys.modules, "db_insert", db)
    monkeypatch.setattr(eng, "settings", lambda: {"documents": DOCS, "batch_size": 5})
    monkeypatch.setattr(kp_settings, "load", lambda *a, **k: {"governance_master": {"tables": {"fact": "f"}}})
    facts = [{"fact_id": "F_SSS", "name": "Supplier SEZ Status"}, {"fact_id": "F_DONE", "name": "reviewed one"}]
    monkeypatch.setattr(eng, "_facts", lambda *a: (facts, {}))
    out = eng.tag(ask=lambda prompt: json.dumps([GOOD]))
    assert [r["fact_id"] for r in out] == ["F_SSS"] and saved[0]["origin_by"] == "AI" and not saved[0]["reviewed"]
