"""MKP / run waves - base laws first, so the core facts exist before the other laws need them (notes section 16).

Config/settings.json pool.waves lists the units of each wave (an IKP key and every key under it):
    wave 1  CGST s.2, s.7 + Schedules I-III, s.8, s.9            definitions, supply, levy
    wave 2  CGST s.22-25; IGST s.2, s.5, s.7, s.8                registration, inter / intra-State
    wave 3  sales-register laws (s.10-15, s.31-34, place of supply) and their Rules
    pool.other_waves gives the rest by document type: Acts and Rules (wave 4), notifications and circulars (wave 5).
The worker pool starts a wave only when every job of the waves before it is finished (wave 0 = IKP runs first).
A part of a split unit keeps its unit's wave.
"""
from __future__ import annotations

from typing import Dict, List


class WavePlan:
    def __init__(self, pool_cfg: dict):
        waves = pool_cfg.get("waves")
        other = pool_cfg.get("other_waves")
        if not isinstance(waves, list) or not isinstance(other, dict) or "default" not in other:
            raise ValueError("Config/settings.json pool.waves (list) and pool.other_waves (with default) are required")
        self.units: List[tuple] = []          # (key, wave), longest key first - the most specific unit wins
        self.names: Dict[int, str] = {}
        for w in waves:
            n = int(w["wave"])
            self.names[n] = str(w.get("name") or f"wave {n}")
            for u in w.get("units") or []:
                self.units.append((str(u).strip(), n))
        self.units.sort(key=lambda x: -len(x[0]))
        self.other = {str(k).upper(): int(v) for k, v in other.items() if not str(k).startswith("_")}
        for n in self.other.values():
            self.names.setdefault(n, f"wave {n}")

    def wave_of(self, root: str, doc_type: str) -> int:
        root = str(root or "")
        for key, n in self.units:
            if root == key or root.startswith(key + "_"):
                return n
        return self.other.get(str(doc_type or "").upper(), self.other["DEFAULT"])

    def name(self, n: int) -> str:
        return self.names.get(n, f"wave {n}")
