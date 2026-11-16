"""Fact flow layer 4: the fact review (no database: the SQL parts are replaced)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from akp_ss import review  # noqa: E402


class Store:
    def __init__(self):
        self.saved = []

    def cache_put(self, *a):
        self.saved.append(a)


def test_review_lists_pairs_and_saves_them(monkeypatch):
    monkeypatch.setattr(review, "pairs", lambda conn, reg, m: [
        {"a": {"fact": "F_SN", "name": "Supply nature", "entity": "SUPPLY", "type": "CODE", "options": [], "nl": ""},
         "b": {"fact_id": "F_ST", "name": "Supply type", "entity": "SUPPLY", "type": "CODE", "options": [], "nl": "",
               "aliases": [], "similarity": 0.94}}])
    monkeypatch.setattr(review, "measures", lambda conn, reg: {"facts": 2, "fact_links": 3, "reuse_rate": 0.33,
                                                                "new_facts_per_mkp": 1.0, "facts_used_by_one_atom": 1})
    asked = []
    ask = lambda req: asked.append(req) or json.dumps({"facts": [{"fact": "F_SN", "decision": "SAME", "existing": "F_ST",
                                                                  "reason": "same input"}]})
    store = Store()
    out = review.review(None, None, store, ask, 0.9)
    assert out["pairs"][0]["decision"] == "SAME" and out["pairs"][0]["other"] == "F_ST"
    assert asked[0]["candidates"] == {"F_SN": [asked[0]["candidates"]["F_SN"][0]]} and store.saved[0][1] == review.REVIEW_KEY


def test_bad_answer_does_not_stop_the_review(monkeypatch):
    monkeypatch.setattr(review, "pairs", lambda conn, reg, m: [
        {"a": {"fact": "F_A", "name": "a", "type": "BOOL"}, "b": {"fact_id": "F_B", "name": "b", "similarity": 0.95}}])
    monkeypatch.setattr(review, "measures", lambda conn, reg: {})
    out = review.review(None, None, Store(), lambda req: "not json", 0.9)
    assert out["pairs"][0]["decision"] == "UNKNOWN"
