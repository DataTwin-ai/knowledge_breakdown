"""The link register Config/ref_links.xlsx (ref_ss/register.py) - no database, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kp_settings  # noqa: E402
from ref_ss import register  # noqa: E402


def _cfg(tmp_path):
    p = tmp_path / "ref_links.xlsx"
    kp_settings_resolve = kp_settings.resolve
    return {"refs": {"link_register": str(p)}}, p, kp_settings_resolve


def test_write_load_and_keep_user_link(tmp_path, monkeypatch):
    monkeypatch.setattr(kp_settings, "resolve", lambda x: Path(x))
    cfg = {"refs": {"link_register": str(tmp_path / "r.xlsx")}}
    assert register.write(cfg, {"EXT_A_1949": {"kind": "OTHER_ACT", "document name": "The A Act, 1949",
                                               "link": "https://x/a.pdf", "status": "TO CHECK"}})
    # the script never replaces a link the user wrote
    register.write(cfg, {"EXT_A_1949": {"link": "https://x/other.pdf", "status": "WRONG URL - not this Act"}})
    row = register.load(cfg)["EXT_A_1949"]
    assert row["link"] == "https://x/a.pdf" and row["status"].startswith("WRONG URL") and row["checked on"]


def test_search_link_can_be_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(kp_settings, "resolve", lambda x: Path(x))
    cfg = {"refs": {"link_register": str(tmp_path / "r.xlsx")}}
    register.write(cfg, {"N_1": {"link": "https://x/bad.pdf", "status": register.FOUND}})
    register.write(cfg, {"N_1": {"status": register.VERIFIED, "link": "https://x/good.pdf"}})
    assert register.load(cfg)["N_1"]["link"] == "https://x/good.pdf"


def test_add_new_and_done(tmp_path, monkeypatch):
    monkeypatch.setattr(kp_settings, "resolve", lambda x: Path(x))
    cfg = {"refs": {"link_register": str(tmp_path / "r.xlsx")}}
    register.write(cfg, {"A": {"status": "SKIP - same as B"}, "C": {"status": "INSERTED"}})
    assert register.add_new(cfg, [{"ref_id": "A"}, {"ref_id": "D", "kind": "ACT", "title": "The D Act"}]) == 1
    reg = register.load(cfg)
    assert reg["D"]["status"] == register.NO_LINK and reg["D"]["document name"] == "The D Act"
    assert register.done(reg["A"]) and register.done(reg["C"]) and not register.done(reg["D"])
    assert register.skipped(reg["A"]) and not register.skipped(reg["C"])
