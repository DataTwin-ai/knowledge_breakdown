"""GKP Python score of the AKP's group and the one taxonomy from the table (no database)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gkp_ss import score  # noqa: E402
from gkp_ss import taxonomy as tax  # noqa: E402


def test_high_needs_similarity_closest_and_one_more_clue():
    sims = {"TSG06_03": 0.86, "TSG01_01": 0.70}
    c = score.clues("TSG06_03", sims, ["TSG06_03", "TSG01_01"], ["TSG06_03"], ["concept 0.86"])
    assert score.is_high(c, 0.80) and "mkp agrees" in score.why(c)
    alone = score.clues("TSG06_03", sims, ["TSG06_03"], [], ["concept 0.86"])
    assert not score.is_high(alone, 0.80)                          # embedding alone is not enough
    far = score.clues("TSG01_01", sims, ["TSG06_03"], ["TSG01_01"], ["anchor CGSTA17_S7"])
    assert not score.is_high(far, 0.80)                            # not among the closest, too low
    anchored = score.clues("TSG06_03", sims, ["TSG06_03"], [], ["anchor CGSTA17_S10"])
    assert score.is_high(anchored, 0.80)


def test_audit_is_stable_and_about_the_share():
    ids = [f"M_X_A_{i}" for i in range(4000)]
    picked = [i for i in ids if score.audited(i, 0.05)]
    assert picked == [i for i in ids if score.audited(i, 0.05)]    # same atoms every run
    assert 0.03 < len(picked) / len(ids) < 0.07
    assert not score.audited("M_X_A_1", 0.0)


def test_calibration_table():
    rows = [(0.95, True)] * 98 + [(0.95, False)] * 2 + [(0.72, False)] * 10
    t = {x["from_score"]: x for x in score.calibration(rows, buckets=(0.70, 0.90))}
    assert t[0.90]["llm_agrees"] == 0.98 and t[0.70]["atoms"] == 110


def test_taxonomy_from_table_rows_keeps_created_groups():
    meta = {"taxonomy_id": "GST_TAX", "amendment_tsg": "TSG14_02",
            "groups": [{"tg_id": "TG01", "name": "Supply", "description": "d"}]}
    rows = [("TSG01_02", "TG01", "Supply", "Mixed supply", "def b", ["mixed"], [], [], [], None),
            ("TSG01_01", "TG01", "Supply", "Meaning of supply", "def a", ["supply"], ["x"], [], ["CGSTA17_S7"], None),
            ("TSG16_01", "TG16", "Created", "New topic", "def c", ["new"], [], [], [], "GKP")]
    data, subs = tax.from_rows(meta, rows)
    assert [s["tsg_id"] for s in subs] == ["TSG01_01", "TSG01_02", "TSG16_01"]
    assert [g["tg_id"] for g in data["groups"]] == ["TG01", "TG16"] and data["taxonomy_id"] == "GST_TAX"
    assert subs[2]["created_by"] == "GKP" and subs[0]["anchors"] == ["CGSTA17_S7"]
    assert "TSG16_01 New topic" in tax.compact_text(subs)
