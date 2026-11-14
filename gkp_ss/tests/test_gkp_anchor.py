"""GKP candidates without a database: the most specific taxonomy anchor wins.
Run: python -m pytest -q gkp_ss/tests/test_gkp_anchor.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gkp_ss import taxonomy as tax
from gkp_ss.candidates import CandidateFinder
data, subs = tax.load(ROOT / 'Config' / 'gst_taxonomy.json')
by_id={x["tsg_id"]:x for x in subs}
class S:
    def t(self,n): return n
    def approved_for_provision(self,p): return []
    def subgroups_by_fact(self,f,m): return []


def test_specific_anchor_beats_broad_section_anchor():
    """s.16(2)(aa) is anchored to TSG09_05 (2B matching); TSG09_01 / TSG09_04 only anchor all of s.16(2)."""
    f=CandidateFinder(S(),by_id,{"max_candidates":12})
    core={g["tg_id"] for g in data["groups"]}
    c=f.find({"source_mkp":"CGSTA17_S16_SS2_Caa#TEXT","results":'statement: "x"',"concept":"","family":""},core,5)
    src={x["tsg_id"]:x["sources"] for x in c}
    assert src["TSG09_05"]==["anchor CGSTA17_S16_SS2_Caa"]
    assert all(s.startswith("broad") for x in ("TSG09_01","TSG09_04") for s in src[x])
    assert [x["tsg_id"] for x in c][0]=="TSG09_05"

