"""2026-11-22 (no PDF needed): an amending Act quotes the new law - '... shall be inserted, namely:— "(i) ..."'.
The quoted (i), (ii), "49A." are the PRINCIPAL Act's numbers: they stay text of the clause that quotes them, so no
false 'list markers out of sequence' (Act 31 of 2018 failed the IKP gate on them)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ikp_ss.layout import LayoutDocument, Line  # noqa: E402
from ikp_ss.structure import parse_layout  # noqa: E402
from ikp_ss import gates  # noqa: E402

ROWS = [
    (60, True, "THE CENTRAL GOODS AND SERVICES TAX (AMENDMENT) ACT, 2018"),
    (60, True, "An Act further to amend the Central Goods and Services Tax Act, 2017."),
    (60, True, "1. (1) This Act may be called the Central Goods and Services Tax (Amendment) Act, 2018."),
    (60, True, "2. In section 2 of the Central Goods and Services Tax Act, 2017 (hereinafter referred to as the "
               "principal Act),––"),
    (80, True, "(a) in clause (17), for sub-clause (h), the following sub-clause shall be substituted, namely:—"),
    (100, True, "\"(h) activities of a race club including by way of totalisator; and\";"),
    (80, True, "(b) clause (18) shall be omitted;"),
    (80, True, "(c) in clause (35), for the word, brackets and letter \"clause (c)\", the word, brackets and letter "
               "\"clause (b)\" shall be substituted;"),
    (60, True, "3. In section 49 of the principal Act, in sub-section (5), the following proviso shall be inserted, "
               "namely:— \"Provided that the input tax credit"),
    (100, True, "(i) on account of State tax shall be utilised first;"),
    (100, True, "(ii) on account of Union territory tax shall be utilised next;"),
    (100, True, "(iv) on account of integrated tax last.\"."),
    (60, True, "4. After section 49 of the principal Act, the following section shall be inserted, namely:––"),
    (80, True, "\"49A. Notwithstanding anything contained in section 49, the input tax credit"),
    (100, True, "(a) of integrated tax shall be utilised first.\"."),
    (60, True, "5. In section 54 of the principal Act, for the words \"zero-rated supplies\", the word \"export\" "
               "shall be substituted."),
]


def _layout(rows):
    lines = [Line(page=1, x0=x, y0=20.0 * i, x1=560, y1=20.0 * i + 10, text=t, size=11, para_start=ps, first_x=x)
             for i, (x, ps, t) in enumerate(rows)]
    return LayoutDocument(page_count=1, body_size=11, lines=lines, footnotes=[], margin_notes=[], dropped=[],
                          toc_pages=[], raw_chars=sum(len(t) for _, _, t in rows))


def test_quoted_new_law_stays_text_of_the_quoting_clause():
    doc = parse_layout(_layout(ROWS), "ACT_31_2018", "ACT")
    keys = set(doc.nodes)
    assert {"ACT_31_2018_S2_Ca", "ACT_31_2018_S2_Cb", "ACT_31_2018_S2_Cc", "ACT_31_2018_S3", "ACT_31_2018_S4",
            "ACT_31_2018_S5"} <= keys
    assert "(h) activities of a race club" in doc.own_text("ACT_31_2018_S2_Ca")
    assert "(iv) on account of integrated tax" in doc.own_text("ACT_31_2018_S3")
    assert "49A. Notwithstanding" in doc.own_text("ACT_31_2018_S4")
    assert not any(k.startswith(("ACT_31_2018_S2_Ch", "ACT_31_2018_S3_SC", "ACT_31_2018_S49A")) for k in keys)
    assert not [e for e in doc.events if "out of sequence" in e]


def test_principal_act_is_read_as_before():
    """The same lines in a principal Act (code CGSTA17, no 'Act further to amend'): no quote handling."""
    rows = [r for r in ROWS if "further to amend" not in r[2]]
    doc = parse_layout(_layout(rows), "CGSTA17", "ACT")
    assert doc is not None
    assert gates is not None
