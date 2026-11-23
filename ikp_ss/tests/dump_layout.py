"""Test helper (2026-11-22): save the IKP layout (lines read from the PDF) of a PDF as JSON, so the parser can be
tested on a machine without PyMuPDF.  Run from the project folder:

    python ikp_ss/tests/dump_layout.py Law/Amendments/ACT_31_2018.pdf Law/Acts/CGST-Act.pdf

Writes Output/layout_<file name>.json next to nothing else; reads only, changes nothing.
"""
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ikp_ss.layout import read_layout  # noqa: E402

for arg in sys.argv[1:]:
    pdf = Path(arg)
    lay = read_layout(pdf)
    out = ROOT / "Output" / f"layout_{pdf.stem}.json"
    out.write_text(json.dumps(dataclasses.asdict(lay), ensure_ascii=False), encoding="utf-8")
    print(f"LAYOUT : {pdf} -> {out} ({len(lay.lines)} lines, {lay.page_count} pages)")
