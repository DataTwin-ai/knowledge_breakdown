"""Tokens and time of a run: counted per job, per step (MKP / AKP / GKP) and per model; shown in the summary and
appended to Output/usage.xlsx (sheets Runs, Jobs, Models), so every run can be looked at later.

llm_runtime.py calls add() after every model answer with the provider's own counts:
    tokens_in      prompt tokens (including the part read from the provider's cache)
    tokens_out     answer tokens (Gemini "thinking" tokens included - they are paid as output)
    cached_in      prompt tokens the provider read from its cache (cheaper)
Answers reused from the database cost 0 tokens and are not counted.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

STEPS = ("MKP", "AKP", "GKP", "OTHER")
_lock = threading.Lock()
_local = threading.local()
_models: Dict[str, Dict[str, int]] = {}          # model -> {calls, in, out, cached}
_steps: Dict[str, Dict[str, float]] = {}          # step -> {calls, in, out, cached, seconds}


def _zero() -> Dict[str, float]:
    return {"calls": 0, "in": 0, "out": 0, "cached": 0, "seconds": 0.0}


def reset() -> None:
    with _lock:
        _models.clear()
        _steps.clear()


# ---------------------------------------------------------------- per worker: the job and the step it is on
def job_start() -> None:
    _local.job = {s: _zero() for s in STEPS}
    _local.step, _local.since = "OTHER", time.time()


def set_step(step: str) -> None:
    """Close the time of the current step and start another (MKP, AKP, GKP)."""
    _close_step()
    _local.step, _local.since = (step if step in STEPS else "OTHER"), time.time()


def _close_step() -> None:
    job = getattr(_local, "job", None)
    if job is None:
        return
    took = time.time() - getattr(_local, "since", time.time())
    step = getattr(_local, "step", "OTHER")
    job[step]["seconds"] += took
    with _lock:
        _steps.setdefault(step, _zero())["seconds"] += took
    _local.since = time.time()


def job_end() -> Dict[str, Dict[str, float]]:
    _close_step()
    job = getattr(_local, "job", None) or {s: _zero() for s in STEPS}
    _local.job = None
    return job


def carry(fn):
    """fn for a helper thread (the two models asked at the same time): it counts tokens for the same worker, job and
    step, and its log lines keep the worker name."""
    job, step = getattr(_local, "job", None), getattr(_local, "step", "OTHER")
    from kp_console import set_worker, worker
    name = worker()

    def run(*a, **kw):
        _local.job, _local.step = job, step
        set_worker(name)
        try:
            return fn(*a, **kw)
        finally:
            _local.job = None
            set_worker("")
    return run


# ---------------------------------------------------------------- every model answer
def add(provider: str, model: str, tokens_in: int, tokens_out: int, cached_in: int = 0) -> None:
    step = getattr(_local, "step", "OTHER")
    vals = {"calls": 1, "in": int(tokens_in or 0), "out": int(tokens_out or 0), "cached": int(cached_in or 0)}
    with _lock:
        m = _models.setdefault(f"{provider}:{model}", {"calls": 0, "in": 0, "out": 0, "cached": 0})
        s = _steps.setdefault(step, _zero())
        for k, v in vals.items():
            m[k] += v
            s[k] += v
    job = getattr(_local, "job", None)
    if job is not None:
        with _lock:                        # the two helper threads of one job add to the same row
            for k, v in vals.items():
                job[step][k] += v


def gemini_usage(response) -> tuple:
    u = getattr(response, "usage_metadata", None)
    if u is None:
        return 0, 0, 0
    g = lambda k: int(getattr(u, k, 0) or 0)
    return g("prompt_token_count"), g("candidates_token_count") + g("thoughts_token_count"), \
        g("cached_content_token_count")


def claude_usage(message) -> tuple:
    u = getattr(message, "usage", None)
    if u is None:
        return 0, 0, 0
    g = lambda k: int(getattr(u, k, 0) or 0)
    cached = g("cache_read_input_tokens")
    return g("input_tokens") + cached + g("cache_creation_input_tokens"), g("output_tokens"), cached


# ---------------------------------------------------------------- showing and saving
def k(n: float) -> str:
    n = float(n or 0)
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"


def job_text(job: Dict[str, Dict[str, float]]) -> str:
    tin = sum(v["in"] for v in job.values())
    tout = sum(v["out"] for v in job.values())
    return f"tokens {k(tin)} in, {k(tout)} out" if tin or tout else "tokens 0 (from database)"


def current_text() -> str:
    """Tokens of the job this worker is on, so far (for its "done" line)."""
    job = getattr(_local, "job", None)
    return job_text(job) if job else ""


def totals() -> Dict[str, Dict]:
    with _lock:
        return {"steps": {s: dict(v) for s, v in _steps.items()}, "models": {m: dict(v) for m, v in _models.items()}}


def summary_lines(run_seconds: float) -> List[str]:
    t = totals()
    lines = ["USAGE        calls   tokens in  tokens out   cached in      time"]
    tot = _zero()
    for s in STEPS:
        v = t["steps"].get(s)
        if not v or (s == "OTHER" and not v["calls"]):
            continue
        for key in tot:
            tot[key] += v[key]
        lines.append(f"  {s:<9} {int(v['calls']):>7} {k(v['in']):>11} {k(v['out']):>11} {k(v['cached']):>11} "
                     f"{v['seconds'] / 60:>8.1f} min")
    lines.append(f"  {'TOTAL':<9} {int(tot['calls']):>7} {k(tot['in']):>11} {k(tot['out']):>11} {k(tot['cached']):>11} "
                 f"   whole run {run_seconds / 60:.1f} min")
    lines.append("  (time per step = worker time added up; workers run at the same time, so it can be more than the run)")
    for m, v in sorted(t["models"].items()):
        lines.append(f"  {m:<34} {v['calls']:>5} calls  {k(v['in'])} in, {k(v['out'])} out, {k(v['cached'])} cached")
    return lines


def save_excel(path: Path, run: dict, jobs: List[dict]) -> Path:
    """Append this run to Output/usage.xlsx: Runs (one row per run), Jobs (one row per job), Models."""
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = load_workbook(path) if path.exists() else Workbook()
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1 and wb["Sheet"].max_row == 1:
        wb.remove(wb["Sheet"])
    heads = {
        "Runs": ["Run", "Started", "Documents", "Scope", "Workers", "Jobs", "Done", "Failed", "Run minutes",
                 "LLM calls", "Tokens in", "Tokens out", "Cached in",
                 "MKP tokens in", "MKP tokens out", "AKP tokens in", "AKP tokens out", "GKP tokens in", "GKP tokens out",
                 "MKP minutes", "AKP minutes", "GKP minutes"],
        "Jobs": ["Run", "Document", "Unit", "Worker", "Status", "Seconds", "MKP seconds", "AKP seconds", "GKP seconds",
                 "LLM calls", "Tokens in", "Tokens out", "Cached in", "Atoms", "Groups", "Law", "Failed at", "Reason"],
        "Models": ["Run", "Model", "Calls", "Tokens in", "Tokens out", "Cached in"],
    }
    for name, cols in heads.items():
        if name not in wb.sheetnames:
            ws = wb.create_sheet(name)
            ws.append(cols)
            for c in ws[1]:
                c.font = Font(bold=True)
            ws.freeze_panes = "A2"
        else:                              # a usage.xlsx from before: add the new columns at the end
            ws = wb[name]
            have = [c.value for c in ws[1]]
            for i, col in enumerate(cols, 1):
                if col not in have:
                    ws.cell(1, len(have) + 1, col).font = Font(bold=True)
                    have.append(col)
    t = totals()
    st = lambda s, key: t["steps"].get(s, {}).get(key, 0)
    rid = run["run_id"]
    tin = sum(v["in"] for v in t["steps"].values())
    tout = sum(v["out"] for v in t["steps"].values())
    wb["Runs"].append([rid, run["started"], run["documents"], run["scope"], run["workers"], run["jobs"], run["done"],
                       run["failed"], round(run["seconds"] / 60, 2),
                       int(sum(v["calls"] for v in t["steps"].values())), int(tin), int(tout),
                       int(sum(v["cached"] for v in t["steps"].values())),
                       int(st("MKP", "in")), int(st("MKP", "out")), int(st("AKP", "in")), int(st("AKP", "out")),
                       int(st("GKP", "in")), int(st("GKP", "out")),
                       round(st("MKP", "seconds") / 60, 2), round(st("AKP", "seconds") / 60, 2),
                       round(st("GKP", "seconds") / 60, 2)])
    for j in jobs:
        u = j["usage"]
        wb["Jobs"].append([rid, j["document"], j["unit"], j["worker"], j["status"], round(j["seconds"], 1),
                           round(u["MKP"]["seconds"], 1), round(u["AKP"]["seconds"], 1), round(u["GKP"]["seconds"], 1),
                           int(sum(v["calls"] for v in u.values())), int(sum(v["in"] for v in u.values())),
                           int(sum(v["out"] for v in u.values())), int(sum(v["cached"] for v in u.values())),
                           j.get("atoms", 0), j.get("groups", ""), j.get("label", ""),
                           j.get("stage", "") if j.get("status") != "DONE" else "",
                           j.get("error", "") if j.get("status") != "DONE" else ""])
    for m, v in sorted(t["models"].items()):
        wb["Models"].append([rid, m, v["calls"], v["in"], v["out"], v["cached"]])
    for ws in wb.worksheets:
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col[:200])
            ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 50)
    wb.save(path)
    return path


def run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
