"""Fact origin -> rulebook yaml (2026-11-26) - tiny rulebook, no database."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fact_origin_ss import yaml_origin as yo  # noqa: E402

RULEBOOK = """# GOVERNANCE MASTER TSG99_01 Tiny (TG99 Test)
name: TSG99_01 Tiny - GOVERNANCE MASTER rulebook
facts:
  # ---- level 1: gate facts - if one is false, that law stops here
  F_SKOT:  # 1. 1 gate | 2 law(s)
    name: Supply Kind Of Transaction
    type: CODE
    options:
    - GOODS
    - SERVICES
    tier: T1
  # ---- level 2: asked only when that law's gates pass
  F_FOS:  # 2. 2 | 1 law(s)
    name: Form Of Supply
    type: CODE
    options:
    - SALE
    - RENTAL
    tier: T1
  F_DOI:
    name: Date Of Invoice
    type: DATE
predicates:
  M_A_X:
    rule: F_SKOT = 'GOODS'
"""

ORIGINS = {
    "F_SKOT": {"document": "SALE_INVOICE", "method": "LOOKUP", "uses": "SALE_INVOICE.hsn_sac",
               "how_to_get": "99xx = services, else goods", "fallback": "ask the user", "sensitive": False,
               "reviewed": False, "note": "not written", "model": "not written"},
    "F_FOS": {"document": "SALE_INVOICE", "method": "READ", "uses": "SALE_INVOICE.line_description, CONTRACT.subject",
              "how_to_get": "read the description: sale, rental ...", "ai_level": "MEDIUM"},
}


def test_every_fact_with_an_origin_gets_its_block_and_nothing_else_changes():
    new, n = yo.apply_text(RULEBOOK, ORIGINS)
    assert n == 2
    old, d = yaml.safe_load(RULEBOOK), yaml.safe_load(new)
    assert d["facts"]["F_SKOT"]["origin"] == {"document": "SALE_INVOICE", "method": "LOOKUP",
                                              "uses": ["SALE_INVOICE.hsn_sac"], "how_to_get": "99xx = services, else goods",
                                              "fallback": "ask the user", "sensitive": False, "reviewed": False}
    assert d["facts"]["F_FOS"]["origin"]["uses"] == ["SALE_INVOICE.line_description", "CONTRACT.subject"]
    assert "origin" not in d["facts"]["F_DOI"]                     # no origin row: no block
    for f, v in d["facts"].items():
        v = dict(v)
        v.pop("origin", None)
        assert v == old["facts"][f]
    assert d["predicates"] == old["predicates"] and d["name"] == old["name"]


def test_comments_stay_and_the_level_header_stays_above_its_fact():
    new, _ = yo.apply_text(RULEBOOK, ORIGINS)
    lines = new.split("\n")
    assert new.count("  # ----") == 2 and "# 1. 1 gate | 2 law(s)" in new
    i = lines.index("  # ---- level 2: asked only when that law's gates pass")
    assert lines[i + 1].startswith("  F_FOS:") and lines[i - 1].startswith("      ")   # SKOT's origin ends above it


def test_running_again_replaces_the_block_never_doubles_it():
    once, _ = yo.apply_text(RULEBOOK, ORIGINS)
    twice, _ = yo.apply_text(once, ORIGINS)
    assert twice == once and once.count("origin:") == 2
    changed = dict(ORIGINS, F_SKOT=dict(ORIGINS["F_SKOT"], method="FIELD"))
    again, _ = yo.apply_text(once, changed)
    assert yaml.safe_load(again)["facts"]["F_SKOT"]["origin"]["method"] == "FIELD" and again.count("origin:") == 2


def test_apply_dir_writes_only_rulebooks_that_change(tmp_path):
    (tmp_path / "TG99").mkdir()
    f = tmp_path / "TG99" / "TSG99_01_rulebook.yaml"
    f.write_text(RULEBOOK, encoding="utf-8")
    assert yo.apply_dir(tmp_path, ORIGINS) == (1, 2)
    assert yo.apply_dir(tmp_path, ORIGINS) == (0, 2)              # already there: nothing rewritten


def test_tag_writes_the_origins_into_the_yaml_files(monkeypatch, tmp_path):
    import json
    import types
    import fact_origin_engine as eng
    import kp_settings
    (tmp_path / "TG99").mkdir()
    f = tmp_path / "TG99" / "TSG99_01_rulebook.yaml"
    f.write_text(RULEBOOK, encoding="utf-8")
    table = {}

    class Store:
        def __init__(self, *a): pass
        def ensure(self): pass
        def rows(self): return dict(table)
        def save(self, rows, overwrite_reviewed=False):
            table.update({r["fact_id"]: r for r in rows})
            return len(rows)
    monkeypatch.setattr("fact_origin_ss.store.OriginStore", Store)
    db = types.ModuleType("db_insert")
    db.get_connection = lambda: types.SimpleNamespace(close=lambda: None)
    db.get_db_schema = lambda: "s"
    monkeypatch.setitem(sys.modules, "db_insert", db)
    docs = {"SALE_INVOICE": {"fields": ["hsn_sac"]}}
    monkeypatch.setattr(eng, "settings", lambda: {"documents": docs, "batch_size": 5, "write_yaml": True})
    monkeypatch.setattr(kp_settings, "load", lambda *a, **k: {"governance_master": {"tables": {"fact": "f"},
                                                                                     "output_dir": str(tmp_path)}})
    monkeypatch.setattr(kp_settings, "require", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_facts", lambda *a: ([{"fact_id": "F_SKOT", "name": "Supply Kind"}], {}))
    answer = {"fact_id": "F_SKOT", "document": "SALE_INVOICE", "method": "LOOKUP", "how_to_get": "99xx = services",
              "uses": "SALE_INVOICE.hsn_sac", "as_of": "invoice date", "level": "INVOICE", "fallback": "ask",
              "when_missing": "ASK", "cost": "LOW", "ai_level": "NONE", "example": "GOODS", "confidence": "high",
              "sensitive": False}
    eng.tag(ask=lambda prompt: json.dumps([answer]))
    origin = yaml.safe_load(f.read_text(encoding="utf-8"))["facts"]["F_SKOT"]["origin"]
    assert origin["method"] == "LOOKUP" and origin["uses"] == ["SALE_INVOICE.hsn_sac"]
