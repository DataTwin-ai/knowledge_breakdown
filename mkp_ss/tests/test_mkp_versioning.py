"""MKP versioning without a database: a re-run inserts the next mkp_version and never deletes.
Run: python -m pytest -q mkp_ss/tests/test_mkp_versioning.py
"""
import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mkp_ss.store import MkpStore  # noqa: E402

ROWS = []
COLS = ["cluster", "type", "element", "results", "embedding", "version_id", "mkp_id", "run_id", "source_version",
        "qc_status", "doc_type", "instrument_code", "mkp_version"]


def _text(q) -> str:
    """SQL text of a psycopg2 Composed without a live connection (repr lists the SQL pieces in order)."""
    if isinstance(q, str):
        return q
    try:
        return q.as_string(None)
    except Exception:
        return " ".join(re.findall(r"SQL\('((?:[^'\\]|\\.)*)'\)", repr(q)))


class Cur:
    def __init__(self):
        self.res = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, p=None):
        q, self.res = _text(q).strip(), []
        if "SELECT element, max(mkp_version)" in q:
            ver, els = p
            best = {}
            for r in ROWS:
                if r["version_id"] == ver and r["element"] in els:
                    best[r["element"]] = max(best.get(r["element"], 0), r["mkp_version"])
            self.res = list(best.items())
        elif "SELECT COALESCE(max(mkp_version)" in q:
            ver, cl = p
            self.res = [(max([r["mkp_version"] for r in ROWS if r["version_id"] == ver and r["cluster"] == cl
                              and r["type"] in ("BATCH", "SUM")] or [0]),)]
        elif q.startswith("SELECT mkp_id, element, results"):
            self.res = [(r["mkp_id"], r["element"], r["results"]) for r in ROWS
                        if r["type"] == "MKP" and r["is_latest"] and r["mkp_id"]]
        elif q.startswith("UPDATE"):
            ver, els, cl = p
            for r in ROWS:
                if r["version_id"] == ver and r["is_latest"] and (r["element"] in els or
                                                                  (r["cluster"] == cl and r["type"] in ("BATCH", "SUM"))):
                    r["is_latest"] = False
        elif q.startswith("INSERT INTO") and "mkp_version" in q:
            ROWS.append(dict(zip(COLS, p), is_latest=True))
        elif q.startswith("DELETE"):
            raise AssertionError("MKP must never delete rows")

    def fetchall(self):
        return self.res

    def fetchone(self):
        return self.res[0] if self.res else None


class Conn:
    def cursor(self):
        return Cur()

    def commit(self):
        pass

    def rollback(self):
        pass


STD = [{"CLUSTER": "C_X_S7", "TYPE": "BATCH", "ELEMENT": "DOC", "RESULTS": "instrument: X"},
       {"CLUSTER": "C_X_S7", "TYPE": "MKP", "ELEMENT": "X_S7#TEXT",
        "RESULTS": 'concept: "A B"; citation_key: X_S7; part: TEXT; text: "t1"'},
       {"CLUSTER": "C_X_S7", "TYPE": "SP", "ELEMENT": "X_S7#TEXT", "RESULTS": 'sp: SP1; text: "t1"'},
       {"CLUSTER": "C_X_S7", "TYPE": "SUM", "ELEMENT": "DOC", "RESULTS": "mkp_count: 1"}]


def test_rerun_inserts_next_version_and_keeps_history():
    ROWS.clear()
    store = MkpStore(Conn(), "public", "mkp")
    for run in (1, 2, 3):
        stats = store.commit("X", "V_1", "C_X_S7", copy.deepcopy(STD), {}, "X_S7", "sha", "h", "dh", lambda t: [0.1, 0.2])
        assert stats["versions"] == {"X_S7#TEXT": run}
    latest = [r for r in ROWS if r["is_latest"]]
    assert len(ROWS) == 12 and len(latest) == 4
    assert all(r["mkp_version"] == 3 for r in latest)


def test_mkp_id_unique_across_laws():
    """Two laws with the same concept name never share an MKP id; a re-run keeps each law's id."""
    ROWS.clear()
    store = MkpStore(Conn(), "public", "mkp")
    other = [dict(r, CLUSTER="C_X_S8", ELEMENT=r["ELEMENT"].replace("S7", "S8"),
                  RESULTS=r["RESULTS"].replace("X_S7", "X_S8")) for r in copy.deepcopy(STD)]
    a = store.commit("X", "V_1", "C_X_S7", copy.deepcopy(STD), {}, "X_S7", "sha", "h", "dh", lambda t: None)
    b = store.commit("X", "V_1", "C_X_S8", copy.deepcopy(other), {}, "X_S8", "sha", "h", "dh", lambda t: None)
    assert a["mkp_ids"] == {"X_S7#TEXT": "M_AB"} and b["mkp_ids"] == {"X_S8#TEXT": "M_AB2"}
    changed = copy.deepcopy(other)
    changed[1]["RESULTS"] = changed[1]["RESULTS"].replace('"t1"', '"t2"')
    changed[2]["RESULTS"] = changed[2]["RESULTS"].replace('"t1"', '"t2"')
    c = store.commit("X", "V_1", "C_X_S8", changed, {}, "X_S8", "sha", "h", "dh", lambda t: None)
    assert c["mkp_ids"] == {"X_S8#TEXT": "M_AB2"}          # new text, same concept -> keeps its id
    latest = {r["element"]: r["mkp_id"] for r in ROWS if r["is_latest"] and r["type"] == "MKP"}
    assert latest == {"X_S7#TEXT": "M_AB", "X_S8#TEXT": "M_AB2"}
