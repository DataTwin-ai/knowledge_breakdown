"""What you see in the command prompt, and the full detail in Output/logs.

flow(...)  the flow of the run: documents, workers taking / finishing units, retries, errors, summary.
           Always on screen and in the log.
say(...)   an engine line (MKP / AKP / GKP detail). On screen only when permission.json console.show = "DETAIL";
           always in the log.
log(...)   detail (model calls, candidates, scores): written to the log only.
The log file is Output/logs/law_arc_<date>_<time>.log, one per run. Every line written by a worker carries its
name ([W2]) so the log can be read per worker. Safe to call from many threads.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_log_path: Optional[Path] = None
_lock = threading.RLock()
_local = threading.local()
VERBOSE = False            # tests may set True to see everything on screen
SHOW = "DETAIL"            # main.py sets it from permission.json console.show (FLOW or DETAIL)


def set_show(mode: str) -> None:
    global SHOW
    SHOW = "FLOW" if str(mode).upper() == "FLOW" else "DETAIL"


def set_worker(name: str = "") -> None:
    """Tag the lines of this thread with the worker name (W1, W2 ...)."""
    _local.worker = name


def worker() -> str:
    return getattr(_local, "worker", "") or ""


def _file() -> Optional[Path]:
    global _log_path
    if _log_path is None:
        try:
            from kp_settings import build_paths
            folder = build_paths()["log_dir"]
        except Exception:
            folder = Path(__file__).resolve().parent / "Output" / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _log_path = folder / f"law_arc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        except Exception:
            return None
    return _log_path


def _write(text: str) -> None:
    path = _file()
    if path is None:
        return
    w = worker()
    if w:
        text = "\n".join(f"[{w}] {line}" for line in text.split("\n"))
    try:
        with _lock, path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def _print(text: str) -> None:
    with _lock:
        _board_clear()
        print(text)
        _board_draw()
        sys.stdout.flush()


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    if VERBOSE:
        _print(text)
    _write(text)


def say(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    if SHOW == "DETAIL" or VERBOSE:
        _print(text)
    _write(text)


def flow(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    _print(text)
    _write(text)


def log_path() -> str:
    p = _file()
    return str(p) if p else ""


def show_change(label: str, old: str, new: str, width: int = 400) -> None:
    """When the LLM is called because a text changed: show both versions."""
    say(f"  {label} v1 : {' '.join(str(old).split())[:width]}")
    say(f"  {label} v2 : {' '.join(str(new).split())[:width]}")


# ---------------------------------------------------------------- live worker board (worker pool)
#
# While the pool runs, a block at the bottom of the screen shows the overall progress bar and what every worker is
# doing right now (job, step, time on it). It redraws in place (VS Code / Windows Terminal / PowerShell); flow lines
# print above it. In a window that cannot redraw (or output sent to a file) the block is printed every 15 s instead.

_board = {"on": False, "ansi": False, "lines": 0, "header": None, "workers": {}}


def _ansi_ok() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:                                   # Windows 10+: switch on ANSI escape codes for this console
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def board_start(workers, header) -> None:
    """header() -> the progress line. workers: their names (W1 ...)."""
    with _lock:
        _board.update(on=True, ansi=_ansi_ok(), lines=0, header=header,
                      workers={w: {"job": "", "stage": "idle", "since": time.time()} for w in workers})


def board_stop() -> None:
    with _lock:
        _board_clear()
        _board["on"] = False
        _board["lines"] = 0


def stage(text: str, job: str = None) -> None:
    """The step this worker is on now (shown on the board; no-op outside the pool)."""
    w = worker()
    if not _board["on"] or w not in _board["workers"]:
        return
    with _lock:
        cur = _board["workers"][w]
        if job is not None:
            cur["job"] = job
            cur["since"] = time.time()
        cur["stage"] = text


def board_lines():
    lines = [_board["header"]() if _board["header"] else ""]
    now = time.time()
    for w, v in _board["workers"].items():
        if v["stage"] in ("idle", "finished"):
            lines.append(f"  {w:<4} " + ("idle - waiting for a job" if v["stage"] == "idle" else "finished - no jobs left"))
            continue
        sec = int(now - v["since"])
        lines.append(f"  {w:<4} {v['job'][:36]:<36} {v['stage'][:28]:<28} {sec // 60}:{sec % 60:02d}")
    return lines


def _board_clear() -> None:
    if _board["on"] and _board["ansi"] and _board["lines"]:
        sys.stdout.write(f"\x1b[{_board['lines']}F\x1b[J")      # up to the board's first line, clear to the end
        _board["lines"] = 0


def _board_draw() -> None:
    if not (_board["on"] and _board["ansi"]):
        return
    lines = board_lines()
    sys.stdout.write("\n".join(lines) + "\n")
    _board["lines"] = len(lines)


def board_tick() -> None:
    """Redraw the board (ANSI) - called every second by the pool."""
    with _lock:
        if _board["on"] and _board["ansi"]:
            _board_clear()
            _board_draw()
            sys.stdout.flush()


def board_print() -> None:
    """The board as plain lines (windows that cannot redraw), every 15 s."""
    with _lock:
        if _board["on"] and not _board["ansi"]:
            print("\n".join(board_lines()))
            sys.stdout.flush()
