"""Fact origin -> rulebook yaml (2026-11-26).

Every fact in a governance master rulebook (Governance_Master/<TG>/<TSG>_rulebook.yaml) gets an `origin:` block with
WHERE its value comes from and HOW to get it, so the validation engine needs the yaml only:

    F_FOS:
      name: Form Of Supply
      type: CODE
      options: [...]
      tier: T1
      origin:
        document: SALE_INVOICE
        method: READ                      # FIELD / DERIVED / LOOKUP / READ / ASK
        uses: [SALE_INVOICE.line_description]
        how_to_get: Read the line description ...
        fallback: ...
        ...

The file is edited as text, so every comment the governance master wrote (levels, ask order) stays. A fact with no
origin row keeps no origin block. An older origin block is replaced, never doubled.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

# the keys written into the yaml, in this order (empty ones are left out)
KEYS = ["document", "method", "uses", "how_to_get", "fallback", "when_missing", "as_of", "level", "ai_level", "cost",
        "example", "confidence", "sensitive", "reviewed", "origin_by"]

_FACT = re.compile(r"^  (F_[A-Za-z0-9_]+):")


def block(row: dict) -> dict:
    """One fact_origin row -> the origin block (uses as a list, empty values left out)."""
    out = {}
    for k in KEYS:
        v = row.get(k)
        if k == "uses":
            v = [u.strip() for u in str(v or "").replace(";", ",").split(",") if u.strip()]
        if k in ("sensitive", "reviewed"):
            out[k] = bool(v)
            continue
        if v in (None, "", []):
            continue
        out[k] = v
    return out


def _lines(blk: dict) -> List[str]:
    text = yaml.safe_dump({"origin": blk}, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None)
    return ["    " + ln for ln in text.rstrip("\n").split("\n")]


def apply_text(text: str, origins: Dict[str, dict]) -> Tuple[str, int]:
    """Put (or replace) the origin block of every fact of the facts: section that has an origin row.
    Returns the new text and how many facts got a block."""
    lines = text.split("\n")
    out: List[str] = []
    inside, fact, skip, n = False, None, False, 0

    def close():
        nonlocal n
        if fact and fact in origins:
            blk = block(origins[fact])
            if blk:
                # the block goes before any blank / comment lines that belong to the next fact
                tail = []
                while out and (not out[-1].strip() or out[-1].lstrip().startswith("#")) and \
                        not out[-1].startswith("    "):
                    tail.insert(0, out.pop())
                out.extend(_lines(blk))
                out.extend(tail)
                n += 1

    for line in lines:
        if re.match(r"^\S", line):                     # a top-level key ends the facts section
            if inside:
                close()
                fact = None
            inside = line.startswith("facts:")
            skip = False
            out.append(line)
            continue
        if inside:
            m = _FACT.match(line)
            if m:
                close()
                fact, skip = m.group(1), False
                out.append(line)
                continue
            if fact and line.startswith("    origin:"):   # an older block: drop it and its lines
                skip = True
                continue
            if skip:
                if line.startswith("      "):       # the old block's own lines (deeper than its key)
                    continue
                skip = False
        out.append(line)
    if inside:
        close()
    return "\n".join(out), n


def apply_dir(folder: Path, origins: Dict[str, dict]) -> Tuple[int, int]:
    """Every <TSG>_rulebook.yaml under the governance master folder. Returns (files changed, fact blocks written)."""
    files = changed = 0
    blocks = 0
    for p in sorted(Path(folder).glob("TG*/*_rulebook.yaml")):
        old = p.read_text(encoding="utf-8")
        new, n = apply_text(old, origins)
        blocks += n
        files += 1
        if new != old:
            yaml.safe_load(new)                       # never write a file that does not load
            p.write_text(new, encoding="utf-8")
            changed += 1
    return changed, blocks
