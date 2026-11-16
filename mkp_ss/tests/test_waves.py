"""Run waves: base laws first; a later wave starts only when the earlier waves are done (no database)."""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mkp_ss.waves import WavePlan  # noqa: E402

POOL = json.loads((Path(__file__).resolve().parents[2] / "Config" / "settings.json").read_text(encoding="utf-8"))["pool"]


def test_wave_of_units():
    w = WavePlan(POOL)
    assert w.wave_of("CGSTA17_S7_SS1_Ca", "ACT") == 1
    assert w.wave_of("CGSTA17_SCHIII_EN5", "ACT") == 1
    assert w.wave_of("CGSTA17_S22", "ACT") == 2                     # s.22 is not s.2
    assert w.wave_of("CGSTA17_S2_C52", "ACT") == 1
    assert w.wave_of("CGSTA17_S10_SS1", "ACT") == 3
    assert w.wave_of("CGSTR17_R46_SR1", "RULES") == 3
    assert w.wave_of("CGSTA17_S16", "ACT") == 4
    assert w.wave_of("N04_2018_PA1", "NOTIFICATION") == 5


def test_pool_hands_out_a_later_wave_only_after_the_earlier_is_done():
    import kp_pool
    pool = kp_pool.Pool({"pool": POOL}, {}, {}, 2, {})
    doc = {"code": "CGSTA17", "type": "ACT"}
    job = lambda root: kp_pool.Job(doc, SimpleNamespace(root=root, label=root, only_keys=None))
    pool.add([job("CGSTA17_S16"), job("CGSTA17_S7"), job("CGSTA17_S22"), job("CGSTA17_S9")])
    first, second = pool._next(), pool._next()
    assert {first.batch.root, second.batch.root} == {"CGSTA17_S7", "CGSTA17_S9"}
    got = []
    t = threading.Thread(target=lambda: got.append(pool._next()))
    t.start()
    t.join(0.3)
    assert t.is_alive()                                   # wave 2 waits while wave 1 is still running
    pool._finish(wave=first.wave)
    t.join(0.3)
    assert t.is_alive()
    pool._finish(wave=second.wave)
    t.join(2)
    assert got[0].batch.root == "CGSTA17_S22"
    pool._finish(wave=got[0].wave)
    assert pool._next().batch.root == "CGSTA17_S16"
