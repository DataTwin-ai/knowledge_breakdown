"""Same-fact fixes (bug list B, 2026-11-18) - no database, no model.

B11: 60 weak concepts in the runs of 16-17 Nov (the concept repeated the fact name, so the concept search found
nothing new). A NEW fact with a weak concept now gets one more try; a still-weak answer keeps the first concept.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("psycopg2")

from akp_ss.facts import concept_warnings, retry_concepts  # noqa: E402


def facts():
    return [
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_SUPPLIER_PAYMENT_RECEIPT_DATE",
         "CONCEPT": "Supplier Payment Receipt Date",
         "RESULTS": 'name: "Supplier Payment Receipt Date"; entity: SUPPLY; type: DATE'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_FOS", "CONCEPT": "NA",       # registered: never retried
         "RESULTS": 'name: "Form Of Supply"; entity: SUPPLY; type: CODE; options: [SALE]'},
        {"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": "F_OPERATOR_PRESENCE", "CONCEPT": "Presence Status",
         "RESULTS": 'name: "Operator Physical Presence"; entity: PERSON; type: BOOL'},
    ]


def test_weak_concept_of_a_new_fact_is_retried_once():
    asked = []

    def ask(prompt):
        asked.append(json.loads(prompt.split("\nINPUT:\n", 1)[1]))
        return json.dumps({"concepts": [{"fact": "F_SUPPLIER_PAYMENT_RECEIPT_DATE", "concept": "Payment Date"}]})
    rows = facts()
    assert retry_concepts(rows, {"F_FOS": {"name": "Form Of Supply"}}, ask, "PROMPT") == 1
    assert [f["fact"] for f in asked[0]["facts"]] == ["F_SUPPLIER_PAYMENT_RECEIPT_DATE"]
    assert rows[0]["CONCEPT"] == "Payment Date" and concept_warnings(rows[:1]) == []
    assert rows[2]["CONCEPT"] == "Presence Status"                   # a good concept is never asked again


def test_still_weak_answer_keeps_the_first_concept():
    rows = facts()
    ask = lambda p: json.dumps({"concepts": [{"fact": "F_SUPPLIER_PAYMENT_RECEIPT_DATE",
                                              "concept": "Supplier Payment Receipt Date Of Money"}]})
    assert retry_concepts(rows, {}, ask, "PROMPT") == 0
    assert rows[0]["CONCEPT"] == "Supplier Payment Receipt Date"


def test_no_call_when_every_concept_is_fine():
    rows = facts()[2:]
    assert retry_concepts(rows, {}, lambda p: pytest.fail("no call expected"), "PROMPT") == 0
