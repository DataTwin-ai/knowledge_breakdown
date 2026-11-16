"""kp_ids: the abbreviated id of every knowledge point (no database needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import kp_ids  # noqa: E402
from transport import derive_id  # noqa: E402


def test_manager_examples():
    assert kp_ids.mkp_id("Supply Includes All Forms Of Supply") == "M_SIAFOS"
    assert kp_ids.mkp_id("Import Of Services For Consideration") == "M_IOSFC"
    assert kp_ids.mkp_id("Activities Neither Goods Nor Services") == "M_ANGNS"


def test_goods_and_services_stay_apart():
    assert kp_ids.mkp_id("Liability To Pay Tax On Goods") == "M_LTPTOG"
    assert kp_ids.mkp_id("Liability To Pay Tax On Services") == "M_LTPTOS"


def test_numerals_kept_whole():
    assert kp_ids.acronym("Supplies In Schedule II") == "SISII"
    assert kp_ids.acronym("Supplies In Schedule III") == "SISIII"
    assert kp_ids.acronym("Credit Under Section 16") == "CUS16"
    assert kp_ids.acronym("Rule 36A Limit") == "R36AL"


def test_ordinary_words_are_not_roman_numerals():
    # "Civil", "Mix", "Did" are made only of Roman letters but are words, not numbers
    assert kp_ids.acronym("Civil Mix Did Lid") == "CMDL"
    assert kp_ids.acronym("Part I Supply") == "PIS"
    assert derive_id("M", "Civil Works Contract Supply") == "M_CWCS"


def test_hyphen_and_apostrophe_are_one_word():
    assert kp_ids.acronym("Self-Supply By The Supplier's Branch") == "SBTSB"


def test_too_long_needs_a_short_name():
    with pytest.raises(kp_ids.IdError):
        kp_ids.mkp_id("One Two Three Four Five Six Seven Eight Nine Ten Eleven")


def test_element_and_fact_ids():
    assert kp_ids.element_id("M_SIAFOS", "A", "Supply Is A Sale") == "M_SIAFOS_A_SIAS"
    with pytest.raises(kp_ids.IdError):      # 11 letters: the atom needs a short name
        kp_ids.element_id("M_SIAFOS", "A", "The supply of goods or services or both is a sale")
    assert kp_ids.element_id("M_SIAFOS", "LE", "Supply Of Goods") == "M_SIAFOS_LE_SOG"
    assert kp_ids.element_id("M_SIAFOS", "PD", "Supply Of Goods") == "M_SIAFOS_PD_SOG"
    assert kp_ids.fact_id("Supplier Complies With Composition Conditions") == "F_SCWCC"
    with pytest.raises(kp_ids.IdError):
        kp_ids.element_id("M_SIAFOS", "XX", "anything")
    with pytest.raises(kp_ids.IdError):
        kp_ids.element_id("CGSTA17_S7", "A", "Supply is a sale")


def test_gate():
    assert kp_ids.kind_of("M_SIAFOS") == "MKP"
    assert kp_ids.kind_of("M_SIAFOS_A_SIAS") == "A"
    assert kp_ids.kind_of("M_SIAFOS_OR_SOG2") == "OR"
    assert kp_ids.kind_of("F_SCWCC") == "F"
    assert kp_ids.check("F_SCWCC", "F") == []
    assert kp_ids.check("CGSTA17_S7_SS1_Ca#TEXT::A_SALE")           # full-form id fails
    assert kp_ids.check("F_supply_form")                              # lower case fails
    assert kp_ids.check("M_SIAFOS_A_SIAS", "F")                       # wrong kind fails


def test_unique_mkp_id_across_laws():
    taken = {"M_LAATOS": ("CGSTA17_S12_SS1#TEXT", "Liability Arises At Time Of Supply")}
    # same concept name for another law -> a number
    assert kp_ids.unique_mkp_id("Liability Arises At Time Of Supply", "CGSTA17_S13_SS1#TEXT", taken) == "M_LAATOS2"
    # the law that owns the id keeps it
    assert kp_ids.unique_mkp_id("Liability Arises At Time Of Supply", "CGSTA17_S12_SS1#TEXT", taken) == "M_LAATOS"
    # a different word with the same initial -> its second letter is added
    taken = {"M_SOG": ("X#TEXT", "Supply Of Goods")}
    assert kp_ids.unique_mkp_id("Sale Of Goods", "Y#TEXT", taken) == "M_SAOG"
    # a re-run keeps its own numbered id
    taken = {"M_LAATOS": ("X#TEXT", "Liability Arises At Time Of Supply")}
    assert kp_ids.unique_mkp_id("Liability Arises At Time Of Supply", "Y#TEXT", taken, own="M_LAATOS2") == "M_LAATOS2"


def test_unique_ids_in_one_law():
    assert kp_ids.unique_ids(["M_A_A_X", "M_A_A_X", "M_A_A_Y", "M_A_A_X"]) == ["M_A_A_X", "M_A_A_X2", "M_A_A_Y", "M_A_A_X3"]
    assert kp_ids.with_number("F_SF", {"F_SF", "F_SF2"}) == "F_SF3"


def test_link_rows_get_short_ids_too():
    assert kp_ids.kind_of("M_ATIOSC_LK_TF") == "LK"
    assert kp_ids.element_id("M_SIAFOS", "LK", "Definition Of Supply") == "M_SIAFOS_LK_DOS"
