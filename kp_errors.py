"""One error log per document: Output/<DOC>/error_log.csv (opens in Excel).

Every unit (section / sub-section / clause / rule / paragraph) that does not finish one step (IKP, MKP, AKP, GKP,
GOVERNANCE_MASTER) gets a row; the run goes on with the next unit. A unit that later finishes gets a DONE row, so
the last row of a unit is its state. `python main.py --retry-failed` runs again only the units whose last row is
FAILED.

columns: time | document | unit | stage | worker | error | retries | status
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

COLUMNS = ["time", "document", "unit", "stage", "worker", "error", "retries", "status"]
_lock = threading.Lock()


def _file(output_dir: Path, document: str) -> Path:
    folder = Path(output_dir) / str(document).upper()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "error_log.csv"


def record(output_dir: Path, document: str, unit: str, stage: str, error, worker: str = "", retries: int = 0,
           status: str = "FAILED") -> Path:
    path = _file(output_dir, document)
    text = " ".join(f"{type(error).__name__}: {error}".split()) if isinstance(error, BaseException) else \
        " ".join(str(error or "").split())
    row = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "document": str(document).upper(), "unit": unit,
           "stage": stage, "worker": worker, "error": text[:2000], "retries": retries, "status": status}
    with _lock:
        new = not path.exists()
        with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
    return path


def done(output_dir: Path, document: str, unit: str, worker: str = "") -> None:
    """Mark a unit DONE only when it has an earlier FAILED row (keeps the log short)."""
    if last_states(output_dir, document).get(unit, {}).get("status") == "FAILED":
        record(output_dir, document, unit, "ALL", "finished on a later run", worker, 0, "DONE")


def last_states(output_dir: Path, document: str) -> Dict[str, dict]:
    path = Path(output_dir) / str(document).upper() / "error_log.csv"
    if not path.exists():
        return {}
    out: Dict[str, dict] = {}
    with _lock, path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            out[row["unit"]] = row
    return out


def failed_units(output_dir: Path, document: str, stages: Optional[List[str]] = None) -> List[str]:
    return [u for u, r in last_states(output_dir, document).items()
            if r["status"] == "FAILED" and (not stages or r["stage"] in stages)]
