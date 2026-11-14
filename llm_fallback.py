"""Claude -> Gemini fallback (permission.json llm.fallback). No LLM SDK is imported here, so the engines and tests
can use route() without the SDKs.

When a Claude call fails because the credit is finished, the key expired or is wrong (llm_runtime_errors.is_fatal),
llm_runtime marks Claude down for the rest of the run, prints one CLAUDE line, and answers that call and every later
Claude call with the mapped Gemini model instead of stopping the run. The run stops only if Gemini also fails so.
"""
from __future__ import annotations

import threading
from typing import Optional

STATE = {"enabled": False, "CLAUDE": {}, "second_opinion": {}, "down": "", "gemini": None}
_lock = threading.Lock()


def configure(fallback: Optional[dict]) -> None:
    with _lock:
        STATE.update(enabled=False, CLAUDE={}, second_opinion={}, down="", gemini=None)
        if fallback:
            STATE.update(enabled=bool(fallback.get("enabled")), CLAUDE=dict(fallback.get("CLAUDE") or {}),
                         second_opinion=dict(fallback.get("second_opinion") or {}))


def claude_down() -> str:
    """Why Claude is not used any more in this run ("" while it works)."""
    return STATE["down"]


def mark_down(why: str) -> None:
    with _lock:
        if STATE["down"]:
            return
        STATE["down"] = why or "no credit / key expired"
    try:
        from kp_console import flow
        flow(f"CLAUDE     : {' '.join(str(why).split())[:120]} - using GEMINI for the rest of this run "
             f"(permission.json llm.fallback)")
    except Exception:
        pass


def fallback_model(model: str) -> str:
    m = STATE["CLAUDE"].get(model)
    if not m:
        raise RuntimeError(f"CLAUDE is down and permission.json llm.fallback.CLAUDE has no Gemini model for {model}")
    return m


def route(models: dict) -> dict:
    """The models of one tier ({role: {llm, model}}) as used now: once Claude is down, Claude roles become their Gemini
    fallback. The two models of a pair (generator_1 + generator_2, verifier_1 + verifier_2) stay different
    (second_opinion), so there are still two opinions."""
    if not (STATE["enabled"] and STATE["down"]):
        return models
    out = {}
    for role, v in models.items():
        if isinstance(v, dict) and str(v.get("llm", "")).upper() == "CLAUDE":
            out[role] = {**v, "llm": "GEMINI", "model": fallback_model(v["model"])}
        else:
            out[role] = v
    for a, b in (("generator_1", "generator_2"), ("verifier_1", "verifier_2")):
        if a in out and b in out and out[a]["model"] == out[b]["model"]:
            moved = b if out[b] is not models[b] else a if out[a] is not models[a] else ""
            other = STATE["second_opinion"].get(out[moved]["model"]) if moved else ""
            if other:                          # only a model that fell back is changed
                out[moved] = {**out[moved], "model": other}
    return out
