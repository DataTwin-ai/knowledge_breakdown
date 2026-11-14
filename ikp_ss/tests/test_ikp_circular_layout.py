"""Circular parsing on a hand-made layout (no PDF needed): a short right-aligned line in the middle of the
body must not break the next sub-paragraph (Circular 47/21/2018 crashed on "'NoneType' has no attribute 'key'")."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ikp_ss.layout import LayoutDocument, Line  # noqa: E402
from ikp_ss.structure import parse_layout  # noqa: E402


def _layout(rows):
    lines = [Line(page=1, x0=x, y0=20.0 * i, x1=x + 300 if x < 300 else 560, y1=20.0 * i + 10, text=t, size=11,
                  para_start=True, first_x=x) for i, (x, t) in enumerate(rows)]
    return LayoutDocument(page_count=1, body_size=11, lines=lines, footnotes=[], margin_notes=[], dropped=[],
                          toc_pages=[], raw_chars=sum(len(t) for _, t in rows))


def test_sub_paragraph_after_a_short_right_aligned_line():
    rows = [(60, "Subject: Clarification on issues related to moulds and dies - reg."),
            (60, "1. Representations have been received seeking clarification on the following issue."),
            (60, "2. The issue has been examined and is clarified as below."),
            (400, "Table 1"),                                  # short, right-aligned: looks like a signature line
            (400, "(see para 2)"),
            (60, "2.1 Moulds and dies supplied free of cost are not to be added to the value of supply."),
            (60, "3. Difficulty, if any, may be brought to the notice of the Board.")]
    doc = parse_layout(_layout(rows), "C47_21_2018", "CIRCULAR")
    keys = set(doc.nodes)
    assert "C47_21_2018_PA2_SP1" in keys and doc.nodes["C47_21_2018_PA2_SP1"].parent == "C47_21_2018_PA2"
    assert "C47_21_2018_PA3" in keys
