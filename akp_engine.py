"""DARP LAW ARC - AKP: approved MKP -> conceptual AKPs -> checks -> comparison -> versioned storage.

Same flow as before:  Gemini AKP + Claude AKP  ->  Claude STD judge  ->  comparison (reconstructed MKP == source?)
Added:
  * Python checks between judge and comparison (akp_ss/guard.py): negation / qualifier / modal / number guard
    ("without" -> "with" is caught here) and structure gate (IDs, LE / G logic, orphans, SP coverage, facts).
    Failures go back to the judge with the exact errors (judge_repairs), then count as a FALSE attempt.
  * Comparison by a different model family (default gemini-2.5-pro) than the judge.
  * Fact registry (akp_ss/facts.py): facts reused across MKPs by one canonical id; atom -> fact edges.
  * HEADING / AMENDMENT / METADATA MKPs written by Python (no LLM); amendments become time facts.
  * Versions: a re-run saves akp_version n+1 (old rows SUPERSEDED, is_latest false); inserted_at on every row.
  * Model tiers SIMPLE / COMPLEX; Gemini + Claude in parallel; raw answers cached; embeddings PENDING-safe.
  * GROUPS: the MKP's TSG ids (mkp.group_names) go in with the MKP; every A atom gets its own groups field (kept from
    the MKP, or another TSG when the atom is about something else); the judge confirms; Python keeps only real
    TSG ids; saved in akp.group_names. GKP checks every atom's group (Python score, the LLM only when unsure).
  * IDS: short ids made by Python (akp_ss/ids.py, kp_ids.py) just before saving: <MKP id>_<TYPE>_<acronym>, facts F_..
  * FACT FLOW: one fact = one id in every law - the fact slice (layer 1), the same name (layer 2), the fact-check
    model (layer 3, again at save time) and --fact-review / --fact-merge (layer 4). Fact numbers after every run.
  * WORKER POOL: one scope per job, own connection; fact saves are serialised with a lock (one fact id per fact).
Requires the project modules: db_insert, llm_runtime, transport, vector_store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from psycopg2 import sql

from akp_ss import excel as excel_mod
from akp_ss.facts import FactRegistry, skip_rows, time_fact
from akp_ss.factcheck import check as check_facts
from akp_ss.ids import abbreviate, check as id_check, merge_same_tests
from akp_ss import timestep
from akp_ss.guard import engine_ready, legal_words, structure
from akp_ss.parse import (apply_python_review, family, field_list, parse_rows, parse_verdict, raw_rows, retry_text,
                           table_text)
from akp_ss.refs import RefFinder, set_references
from akp_ss.store import AkpStore
from kp_console import flow, log, say, stage
from transport import get_field, mkp_bundles, tg_of

BASE_DIR = Path(__file__).resolve().parent
PYTHON_KINDS = {"HEADING", "AMENDMENT", "METADATA"}           # written by Python, no LLM
NON_OPERATIVE = PYTHON_KINDS | {"CROSS_REF", "ILLUSTRATION"}
ROLES = ("generator_1", "generator_2", "judge", "comparison", "same")
_save_lock = threading.Lock()        # workers save atoms + facts one at a time (one fact id per fact)
TSG_RE = re.compile(r"\bTSG\d{2}_\d{2}\b")


def clean_atom_groups(rows: List[dict], valid: set) -> List[str]:
    """Keep only taxonomy TSG ids in the groups field of every A atom (groups: [..]); returns warnings."""
    warnings = []
    for r in rows:
        if r.get("TYPE") != "A":
            continue
        ids = list(dict.fromkeys(TSG_RE.findall(get_field(r, "groups") or "")))
        keep = [i for i in ids if i in valid]
        if len(keep) != len(ids):
            warnings.append(f"{r['ELEMENT']}: unknown group id(s) {', '.join(i for i in ids if i not in valid)} removed")
        text = re.sub(r";?\s*\bgroups:\s*(\[[^\]]*\]|[^;]*)", "", r.get("RESULTS") or "").strip(" ;")
        r["RESULTS"] = text + "; groups: [" + ", ".join(keep) + "]"
    return warnings


@dataclass
class AkpSession:
    reasons: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {"mkps": 0, "approved": 0, "cached": 0, "python_written": 0,
                                                  "atoms": 0, "unchanged": 0,
                                                  "failed": 0, "same_refs": 0, "legal_refs": 0, "new_facts": 0,
                                                  "fact_links": 0, "time_facts": 0, "embeddings_pending": 0,
                                                  "llm_calls": 0})
    failures: list = field(default_factory=list)


# ---------------------------------------------------------------- settings

def _permission() -> dict:
    from kp_settings import load
    return load()


def validate_akp_settings(raw: dict) -> dict:
    s = dict(raw or {})
    for key, default in (("enabled", False), ("use_cache", True), ("ask_user_input", True), ("save_to_db", True),
                         ("export_excel", True)):
        v = s.get(key, default)
        if type(v) is not bool:
            raise ValueError(f"akp.{key} must be true or false")
        s[key] = v
    if not s["save_to_db"]:
        raise ValueError("akp.save_to_db must be true")
    for k in ("mkp_table", "table", "cache_table", "llm_cache_table", "fact_table", "atom_fact_table",
              "time_fact_table", "gkp_table"):           # every table name from Config/settings.json (no name made here)
        if not str(s.get(k) or "").strip():
            raise ValueError(f"Config/settings.json akp.{k} (table name) is missing")
    if "reuse_old_cache_table" not in s:                 # 2026-11-19: '' = off; else the old AKP cache table
        raise ValueError("Config/settings.json akp.reuse_old_cache_table is missing ('' = off)")
    s["reuse_old_cache_table"] = str(s["reuse_old_cache_table"] or "").strip()
    if s["reuse_old_cache_table"] and s["reuse_old_cache_table"] == s["cache_table"]:
        raise ValueError("akp.reuse_old_cache_table must be the OLD cache table, not akp.cache_table")
    s["reuse_old_fact_table"] = str(s.get("reuse_old_fact_table") or "").strip()
    if s["reuse_old_cache_table"] and not s["reuse_old_fact_table"]:
        raise ValueError("Config/settings.json akp.reuse_old_fact_table is missing (the OLD fact table, e.g. akp_fact)")
    if s["reuse_old_fact_table"] and s["reuse_old_fact_table"] == s["fact_table"]:
        raise ValueError("akp.reuse_old_fact_table must be the OLD fact table, not akp.fact_table")
    s["max_attempts"] = max(1, min(3, int(s.get("max_attempts", 2))))
    s["judge_repairs"] = max(0, int(s.get("judge_repairs", 1)))
    s["complex_threshold"] = float(s.get("complex_threshold", 8))
    s["excel_every"] = max(1, int(s.get("excel_every", 25)))
    s["excel_file"] = s.get("excel_file", "akp.xlsx")
    s["fact_slice"] = int(s.get("fact_slice", 30))
    for k in ("fact_check_min_similarity", "fact_check_top_k", "gkp_table"):      # fact flow layers 1 + 3
        if k not in s:
            raise ValueError(f"Config/settings.json akp.{k} is missing")
    s["fact_check_min_similarity"] = float(s["fact_check_min_similarity"])
    s["fact_check_top_k"] = int(s["fact_check_top_k"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s.get("gst_start", ""))):
        raise ValueError("Config/settings.json akp.gst_start (YYYY-MM-DD) is missing")
    s["legal_word_check"] = str(s.get("legal_word_check", "STANDARD")).upper()   # BLOCK | STANDARD | WARN
    if s["legal_word_check"] not in ("BLOCK", "STANDARD", "WARN"):
        raise ValueError("akp.legal_word_check must be BLOCK, STANDARD or WARN")
    s["same_min_similarity"] = float(s.get("same_min_similarity", 0.90))
    if "same_band_min_similarity" not in s:
        raise ValueError("Config/settings.json akp.same_band_min_similarity is missing")
    s["same_band_min_similarity"] = float(s["same_band_min_similarity"])
    for k in ("same_concept_min_similarity", "same_concept_statement_floor"):     # SAME by law and concept
        if k not in s:
            raise ValueError(f"Config/settings.json akp.{k} is missing")
        s[k] = float(s[k])
    for k in ("same_review_min_similarity", "same_review_top_k", "same_review_batch", "same_review_file",
              "same_sweep_after_run", "fact_sweep_after_run"):                                          # SAME check of saved atoms
        if k not in s:
            raise ValueError(f"Config/settings.json akp.{k} is missing")
    s["same_review_min_similarity"] = float(s["same_review_min_similarity"])
    s["same_review_top_k"] = max(1, int(s["same_review_top_k"]))
    s["same_review_batch"] = max(1, int(s["same_review_batch"]))
    for k in ("same_sweep_after_run", "fact_sweep_after_run"):
        if type(s[k]) is not bool:
            raise ValueError(f"akp.{k} must be true or false")
    if "fact_review_min_similarity" not in s:
        raise ValueError("Config/settings.json akp.fact_review_min_similarity is missing")
    s["fact_review_min_similarity"] = float(s["fact_review_min_similarity"])
    for k in ("section", "scope_key", "only_mkp"):
        s[k] = str(s.get(k, "")).strip()
    models = s.get("models") or {}
    for tier in ("SIMPLE", "COMPLEX"):
        for role in ROLES:
            r = (models.get(tier) or {}).get(role)
            if not isinstance(r, dict) or not r.get("llm") or not r.get("model"):
                raise ValueError(f"permission.json llm.akp.{tier}.{role} needs llm and model")
    s["models"] = models
    return s


def _paths(permission: dict) -> dict:
    from kp_settings import build_paths
    return build_paths(permission)


def _prompts(permission: dict) -> dict:
    from kp_settings import resolve
    prompt_dir = resolve(permission["paths"]["prompt_dir"])
    files = permission["files"]
    names = {"akp": files["akp_prompt"], "judge": files["akp_judge_prompt"],
             "comparison": files["akp_comparison_prompt"], "same": files["akp_same_prompt"],
             "fact": files["akp_fact_prompt"], "time": files["akp_time_prompt"],
             "pair": files["akp_same_pair_prompt"], "concept": files["akp_concept_prompt"]}
    out = {}
    for label, name in names.items():
        path = prompt_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"AKP {label} prompt missing: {path}")
        out[label] = path.read_text(encoding="utf-8-sig")
    return out


def _concept_taxonomy(permission: dict, conn=None):
    """The GST taxonomy as short lines for the groups field, and its TSG ids - the gkp_taxonomy TABLE that GKP
    also uses (base groups + groups GKP created, notes section 9); the file only while the table is empty."""
    from gkp_ss import taxonomy as tax
    file = Path(permission.get("gkp", {}).get("taxonomy_file", "Config/gst_taxonomy.json"))
    if not file.is_absolute():
        file = BASE_DIR / file
    if conn is None:
        _, subs = tax.load(file)
    else:
        from db_insert import get_db_schema
        _, subs = tax.current(conn, get_db_schema(), permission["gkp"]["taxonomy_table"], file)
    return tax.compact_text(subs), {x["tsg_id"] for x in subs}


def _table(name: str):
    from db_insert import get_db_schema
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(name))


# ---------------------------------------------------------------- MKP source (kept: GKP imports _version)

def _in_scope(key, instrument, selection):
    if not selection or selection.upper() == "ALL":
        return True
    s = selection.strip()
    m = re.fullmatch(r"(\d+)\((\d+[A-Za-z]?)\)(?:\(([A-Za-z0-9]+)\))?", s)
    if m:
        sec, ss, cl = m.groups()
        s = f"{instrument}_S{sec}_SS{ss.upper()}" + (f"_C{cl}" if cl else "")
    if s.upper().startswith(instrument + "_"):
        return key == s or key.startswith(s + "_")
    if re.fullmatch(r"\d+[A-Za-z]?", s):
        prefix = f"{instrument}_S{s.upper()}"
        return key == prefix or key.startswith(prefix + "_")
    return key == s or key.startswith(s + "_")


def _version(conn, instrument, table, selection):
    """Latest MKP version (PDF version) that holds the selected scope."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT version_id FROM {} WHERE type='MKP' AND element LIKE %s AND version_id IS NOT NULL
            AND version_id <> 'LEGACY' AND COALESCE(is_latest, true) ORDER BY ctid DESC""").format(_table(table)),
                    (instrument + "%",))
        versions = list(dict.fromkeys(r[0] for r in cur.fetchall()))
    for version in versions:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT element FROM {} WHERE version_id=%s AND type='MKP' AND element LIKE %s "
                                "AND COALESCE(is_latest, true)").format(_table(table)), (version, instrument + "%"))
            if any(_in_scope(r[0].split("#", 1)[0], instrument, selection) for r in cur.fetchall()):
                return version
    raise RuntimeError(f"No Standard MKPs found for {instrument} and scope {selection}")


def _read_bundles(conn, instrument, table, version, selection, only_mkp):
    """Latest MKP + SP rows of the scope, children before parents (as before)."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT cluster, type, element, results, mkp_id, COALESCE(mkp_version, 1) FROM {}
            WHERE version_id=%s AND COALESCE(is_latest, true) AND (type='BATCH' OR element LIKE %s) ORDER BY ctid""")
                    .format(_table(table)), (version, instrument + "%"))
        records = cur.fetchall()
    cards, clusters, mkp_versions = {}, {}, {}
    for cluster, kind, element, results, mkp_id, mver in records:
        row = {"CLUSTER": cluster, "TYPE": kind, "ELEMENT": element, "RESULTS": results, "MKP_ID": mkp_id}
        if kind == "BATCH":
            cards[cluster] = {"id": cluster, "name": get_field(row, "cluster_name"), "question": get_field(row, "question")}
        elif kind in ("MKP", "SP"):
            clusters.setdefault(cluster, []).append(row)
            if kind == "MKP":
                mkp_versions[element] = mver
    out = []
    for cluster, rows in clusters.items():
        for b in mkp_bundles(rows):
            if not _in_scope(b["key"].split("#", 1)[0], instrument, selection):
                continue
            if only_mkp and only_mkp not in (b["key"], b["mkp_id"]):
                continue
            b["mkp_version"] = mkp_versions.get(b["key"], 1)
            out.append((cards.get(cluster, {"id": cluster, "name": cluster, "question": ""}), b))
    if not out:
        raise RuntimeError("Selected scope has no approved Standard MKPs in MKP.")
    keys = {b["key"].split("#", 1)[0] for _, b in out}
    out.sort(key=lambda cb: (0 if get_field(cb[1]["mkp_row"], "parent") in keys else 1, cb[1]["key"]))
    return out


def _law_date(permission: dict) -> dict:
    """The DATE fact that picks the law in force (Config/governance_master_map.json law_date) - the time step's
    placeholders read it, so AKP and the governance master use one invoice-date fact."""
    from kp_settings import resolve
    path = resolve(permission["governance_master"]["map_file"])
    law = (json.loads(path.read_text(encoding="utf-8")).get("law_date") or {})
    if not law.get("fact"):
        raise ValueError(f"{path.name}: law_date.fact is missing")
    return {"fact": law["fact"], "name": law.get("name") or law["fact"]}


def _mkp_groups(conn, table, version, elements) -> Dict[str, list]:
    """TSG ids the MKP step gave each MKP (mkp.group_names; older rows have none)."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT element, group_names FROM {} WHERE version_id=%s AND type='MKP' AND is_latest "
                                "AND element = ANY(%s)").format(_table(table)), (version, list(elements)))
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        return {}
    return {e: (g if isinstance(g, list) else json.loads(g) if g else []) for e, g in rows}


# ---------------------------------------------------------------- models / embeddings

class Models:
    def __init__(self, paths: dict, store: AkpStore, use_cache: bool, session: AkpSession,
                 fake: Optional[Callable] = None):
        self.paths, self.store, self.use_cache, self.session, self.fake = paths, store, use_cache, session, fake
        self.runtimes: Dict[str, dict] = {}
        self.unit = ""                    # the MKP being split (saved with each cached answer)

    def call(self, provider: str, model: str, prompt: str, retries: int = 1) -> str:
        key = hashlib.sha256(f"{provider}|{model}|{prompt}".encode("utf-8")).hexdigest()
        if self.use_cache:
            hit = self.store.llm_get(key)
            if hit is not None:
                return hit
        last = None
        for attempt in range(retries + 1):
            try:
                if self.fake is not None:
                    raw = self.fake(provider, model, prompt)
                else:
                    from llm_runtime import call_llm, create_llm_runtime
                    if provider not in self.runtimes:
                        self.runtimes[provider] = create_llm_runtime(provider, self.paths)
                    rt = dict(self.runtimes[provider])
                    rt["model"] = model
                    raw, _ = call_llm(rt, prompt)
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError(f"{provider} {model} returned an empty response")
                self.session.counts["llm_calls"] += 1
                self.store.llm_put(key, f"{provider}:{model}", raw, self.unit)
                return raw
            except Exception as exc:
                last = exc
                from llm_runtime_errors import is_rate_limited
                if is_rate_limited(exc):
                    raise
                log(f"MODEL ERROR            : {provider} {model}: {type(exc).__name__}: {str(exc)[:160]}"
                    + (" - retrying" if attempt < retries else ""))
        raise RuntimeError(f"{provider} {model} failed: {last}")

    def role(self, role: dict, prompt: str) -> str:
        return self.call(role["llm"], role["model"], prompt)

    def pair(self, models: dict, prompt: str):
        with ThreadPoolExecutor(max_workers=2) as pool:
            from kp_usage import carry
            g = pool.submit(carry(self.role), models["generator_1"], prompt)
            c = pool.submit(carry(self.role), models["generator_2"], prompt)
            return g.result(), c.result()


def make_embedder() -> Callable:
    from vector_store import embed_text, load_vector_config
    cfg = load_vector_config()
    if not cfg["enabled"]:
        log("AKP / EMBEDDINGS   : vector.enabled=false - atoms saved without embeddings")
        return lambda text: None
    return lambda text: embed_text(text, "RETRIEVAL_DOCUMENT", cfg)


def _vectors(rows: List[dict], embed: Callable, session: AkpSession) -> Dict[int, tuple]:
    out, cache = {}, {}
    for i, row in enumerate(rows):
        if row["TYPE"] != "A":
            continue
        pair = []
        for text in (f"{row['CONCEPT']} - {family(row)}", get_field(row, "statement")):
            if text not in cache:
                try:
                    cache[text] = embed(text)
                except Exception as exc:
                    log(f"EMBEDDING              : pending ({type(exc).__name__}) - filled on the next run")
                    cache[text] = None
            pair.append(cache[text])
        out[i] = tuple(pair)
        if not all(pair):
            session.counts["embeddings_pending"] += 1
    return out


def complexity(text: str, sp_rows: list, knowledge_type: str, threshold: float):
    words = len(text.split())
    quals = len(re.findall(r"\b(provided that|unless|except|notwithstanding|subject to|either|neither|whichever|"
                           r"other than|without)\b", text.lower()))
    joins = len(re.findall(r"\b(and|or)\b", text.lower()))
    score = round(words / 25 + len(sp_rows) + 2 * quals + joins / 3, 1)
    tier = "COMPLEX" if score >= threshold or knowledge_type in ("COMPUTATION", "RATE", "TIMELINE") else "SIMPLE"
    return tier, score


# ---------------------------------------------------------------- SAME (existing atom reused)

def _same_band(rows: List[dict], candidates: List[dict], high: float, concept_high: float = 2.0) -> List[dict]:
    """SAME candidates (notes section 7): above `high` always; found by its concept (concept similarity >=
    concept_high) always; between the band floor and `high` only when the existing atom reads the same facts with
    the same rule (different words, same test)."""
    norm = lambda x: " ".join(str(x or "").replace('"', "").split()).upper()
    by_el = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "A"}
    out = []
    for item in candidates:
        row = by_el.get(item["element"])
        mine = (sorted(field_list(row, "facts")), norm(get_field(row, "rule"))) if row else None
        keep = []
        for c in item.get("candidates", []):
            theirs = (sorted(field_list({"RESULTS": c.get("results") or ""}, "facts")),
                      norm(get_field({"RESULTS": c.get("results") or ""}, "rule")))
            if float(c["similarity"]) > high or float(c.get("concept_similarity") or 0) >= concept_high \
                    or (mine and mine[0] and mine == theirs):
                keep.append(c)
        out.append(dict(item, candidates=keep))
    return out


def _old_standard(data) -> Optional[List[dict]]:
    """2026-11-19: the approved atoms of an old AKP cache answer, made ready for a fresh fact + SAME check: an old
    SAME atom (A_REF, decided by the old check) becomes a plain atom again with its own wording. None when the answer
    is not approved, has no atoms, or an old A_REF row kept no wording (that MKP then runs AKP from the start)."""
    import copy
    if not isinstance(data, dict) or str(data.get("status", "")).upper() != "APPROVED" or data.get("python"):
        return None
    rows = copy.deepcopy(data.get("standard") or [])
    if not rows or not any(r.get("TYPE") in ("A", "A_REF") for r in rows):
        return None
    for r in rows:
        if r.get("TYPE") == "A_REF":
            if not str(r.get("RESULTS") or "").strip() or not get_field(r, "statement"):
                return None
            r["TYPE"] = "A"
            for k in ("SAME_AKP_ID", "SAME_REASON", "SAME_BY", "_facts"):
                r.pop(k, None)
    return rows


FACT_TOKEN = re.compile(r"(?<![A-Za-z0-9_])F_[A-Z0-9]+(?:_[A-Z0-9]+)*(?![A-Za-z0-9_])")


def _detach_old_facts(rows: List[dict], old_defs: Dict[str, dict]) -> List[str]:
    """2026-11-19 (old-cache reuse only): an old atom's fact ids belong to the OLD fact table. The same short id can be
    another fact in the new one (old F_PRS 'Person Relation Status' vs new F_PRS 'Person Registration Status'), so
    every old fact gets a temporary id and is matched again by its NAME and meaning only (fact check -> SAME / NEW,
    then a new short id). A fact the old atom read without declaring it is added from the old fact table (old_defs).
    Returns log notes."""
    declared = {r["ELEMENT"] for r in rows if r["TYPE"] == "FACTS"}
    used = {f for r in rows if r["TYPE"] != "FACTS" for f in FACT_TOKEN.findall(r.get("RESULTS") or "")}
    notes = []
    for fid in sorted(used - declared):
        d = old_defs.get(fid)
        if not d:
            notes.append(f"OLD FACT MISSING        : {fid} is read by an old atom but is not in the old fact table")
            continue
        q = lambda v: '"' + str(v or "").replace('"', "'") + '"'
        lst = lambda v: "[" + ", ".join(str(x) for x in (v or [])) + "]"
        res = (f"name: {q(d['name'])}; entity: {d.get('entity') or ''}; type: {d.get('type') or ''}"
               + (f"; options: {lst(d.get('options'))}" if d.get("options") else "")
               + (f"; unit: {d['unit']}" if d.get("unit") else "")
               + (f"; evidence: {lst(d.get('evidence'))}" if d.get("evidence") else "")
               + (f"; derived_from: {lst(d.get('derived_from'))}" if d.get("derived_from") else "")
               + f"; registry: NEW; new_reason: \"reused atom - from the old fact table\"; nl: {q(d.get('nl'))}")
        rows.append({"MKP": "TXN", "TYPE": "FACTS", "ELEMENT": fid, "CONCEPT": d.get("concept") or "NA", "RESULTS": res})
        declared.add(fid)
    rename = {fid: f"F_REUSED_{i}_FACT" for i, fid in enumerate(sorted(declared), 1)}
    if not rename:
        return notes
    pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(k) for k in sorted(rename, key=len, reverse=True))
                     + r")(?![A-Za-z0-9_])")
    for r in rows:
        if r["TYPE"] == "FACTS" and r["ELEMENT"] in rename:
            r["ELEMENT"] = rename[r["ELEMENT"]]
        r["RESULTS"] = pat.sub(lambda m: rename[m.group(1)], r.get("RESULTS") or "")
    notes.append(f"OLD FACTS               : {len(rename)} old fact id(s) set aside - each is matched again by name "
                 f"({', '.join(sorted(rename))})")
    return notes


def _kept_id(canonical: str, cand_ids) -> str:
    """The saved atom the SAME answer names: its akp_id as sent (M_LTPTOG_A_SIG), or the old long form
    (instrument::version::source_mkp::akp_id) cut to that akp_id. '' when it is none of the candidates."""
    c = str(canonical or "").strip().strip('"\'')
    if c in cand_ids:
        return c
    tail = c.split("::")[-1].strip()
    if tail in cand_ids:
        return tail
    return next((a for a in cand_ids if c.endswith(a)), "")


def _python_same(rows: List[dict], candidates: List[dict]) -> List[dict]:
    """SAME without the model (bug list A3): the candidate is an exact copy - the same statement word for word, the
    same facts and the same rule. Returns SAME answers in the model's format (found_by: python)."""
    norm = lambda x: " ".join(str(x or "").replace('"', "").replace("'", "").split()).strip(" .").upper()
    by_el = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "A"}
    out = []
    for item in candidates:
        row = by_el.get(item["element"])
        if not row or not get_field(row, "statement"):
            continue
        mine = (norm(get_field(row, "statement")), sorted(field_list(row, "facts")), norm(get_field(row, "rule")))
        for c in item.get("candidates", []):
            theirs = {"RESULTS": c.get("results") or ""}
            if (norm(get_field(theirs, "statement")), sorted(field_list(theirs, "facts")),
                    norm(get_field(theirs, "rule"))) == mine:
                out.append({"element": item["element"], "canonical": c["akp_id"], "same_verdict": "TRUE",
                            "reason": "exact copy: same statement, facts and rule (Python)", "found_by": "python"})
                break
    return out


def _apply_same(rows: List[dict], matches: List[dict], candidates: List[dict], min_similarity: float = 0.90,
                note: Optional[Callable[[str], None]] = None) -> int:
    """SAME confirmed (by Python for an exact copy, else by the SAME model) -> the new atom is not saved as a new atom
    (bug list A): TYPE A_REF, SAME_AKP_ID = the saved atom's akp_id; ids.abbreviate() then gives the row that same
    akp_id, so this law's groups / LE use the saved id. RESULTS keep this law's wording only as a record. Every SAME
    answer that is not applied is logged with its reason (it used to be dropped silently). Python checks only that
    the answer names one of the candidates that were sent for that atom."""
    note = note or (lambda _m: None)
    sent = {item["element"]: {c["akp_id"]: c for c in item.get("candidates", [])} for item in candidates}
    by_element = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "A"}
    used = {r.get("SAME_AKP_ID") for r in rows if r["TYPE"] == "A_REF"}
    count = 0
    for match in matches:
        new_el = str(match.get("element", "")).strip()
        if str(match.get("same_verdict", "")).strip().upper() != "TRUE":
            continue                                     # NOT SAME: nothing to apply
        cands = sent.get(new_el, {})
        kept = _kept_id(match.get("canonical", ""), set(cands))
        row = by_element.get(new_el)
        why = ("no reason given" if not str(match.get("reason", "")).strip() else
               f"{new_el} is not a new atom of this law" if not row else
               f"{match.get('canonical')!r} is not one of the candidates sent for {new_el}" if not kept else
               f"{kept} is already used by another atom of this law" if kept in used else
               "the new atom has its own time window (valid_from / valid_to)"
               if get_field(row, "valid_from") or get_field(row, "valid_to") else "")
        if why:
            note(f"SAME NOT APPLIED        : {new_el} -> {match.get('canonical')} - {why}")
            continue
        # 2026-11-19: same atom = same rule = same facts - the A_REF tests the SAVED atom's facts and rule (it used to
        # keep this law's own facts: one atom id pointing to two facts, e.g. F_PIISD and F_PSTC)
        saved = {"RESULTS": cands[kept].get("results") or ""}
        own_facts = field_list(row, "facts")
        row["_facts"] = field_list(saved, "facts") or own_facts
        if field_list(saved, "facts"):
            row["RESULTS"] = _with_field(_with_field(row.get("RESULTS") or "", "facts",
                                                     "[" + ", ".join(row["_facts"]) + "]"),
                                         "rule", '"' + get_field(saved, "rule").replace('"', "'") + '"'
                                         if get_field(saved, "rule") else "")
        row["TYPE"] = "A_REF"
        row["SAME_AKP_ID"] = kept
        row["SAME_REASON"] = str(match.get("reason", ""))[:300]
        row["SAME_BY"] = match.get("found_by", "model")
        used.add(kept)
        note(f"SAME                    : {new_el} = {kept} ({row['SAME_BY']}) - {row['SAME_REASON'][:120]}")
        if own_facts and set(own_facts) != set(row["_facts"]):
            note(f"SAME FACTS              : {new_el} uses the saved atom's facts {', '.join(row['_facts'])} "
                 f"(not {', '.join(own_facts)})")
        count += 1
    if count:
        for f in _unused_facts(rows):                    # a fact only that A_REF used is not registered
            rows.remove(f)
            note(f"FACT DROPPED            : {f['ELEMENT']} - only a SAME atom used it (it uses the saved atom's facts)")
    return count


def _with_field(results: str, name: str, value: str) -> str:
    """RESULTS with field `name` set to `value` ('' removes it)."""
    text = re.sub(rf";?\s*\b{name}:\s*(\[[^\]]*\]|\"(?:[^\"\\]|\\.)*\"|[^;]*)", "", results or "").strip(" ;")
    return text + (f"; {name}: {value}" if value else "")


def _unused_facts(rows: List[dict]) -> List[dict]:
    """FACTS rows this AKP declared that no row uses any more (an A_REF tests the saved atom's facts)."""
    text = " ".join(r.get("RESULTS") or "" for r in rows if r["TYPE"] not in ("FACTS", "A_REF"))
    text += " " + " ".join(" ".join(r.get("_facts") or []) for r in rows if r["TYPE"] == "A_REF")
    return [r for r in rows if r["TYPE"] == "FACTS"
            and not re.search(rf"(?<![A-Za-z0-9_]){re.escape(r['ELEMENT'])}(?![A-Za-z0-9_])", text)]


# ---------------------------------------------------------------- one scope

def run_akp_scope(pdf_path, instrument=None, section=None, scope_key=None, only_mkp=None,
                    fake_llm: Optional[Callable] = None, embed: Optional[Callable] = None, settings_override=None,
                    only_keys=None):
    """AKP for one scope: Gemini / Claude -> judge -> Python checks -> comparison -> save (next version).
    only_keys: split only these MKPs of the scope (the worker pool's heading job of a split unit)."""
    permission = _permission()
    s = validate_akp_settings({**permission.get("akp", {}), **(settings_override or {})})
    if not s["enabled"]:
        raise RuntimeError("AKP is disabled in permission.json")
    prompts = _prompts(permission)
    from llm_runtime_marker import CACHE_BREAK
    paths = _paths(permission)
    pdf_path = Path(pdf_path)
    docs = {d["code"].upper(): d for d in permission.get("documents", {}).values()}
    if not instrument:
        found = [d["code"] for d in docs.values() if Path(d.get("file", "")).name.lower() == pdf_path.name.lower()]
        if len(found) != 1:
            raise ValueError("Provide instrument explicitly (PDF filename is not unique)")
        instrument = found[0]
    instrument = instrument.upper()
    doc = docs.get(instrument, {"code": instrument, "name": instrument, "type": "ACT"})
    selection = scope_key or section or s["scope_key"] or s["section"] or "ALL"
    only_mkp = only_mkp or s["only_mkp"]
    session = AkpSession()
    from db_insert import get_connection, get_db_schema
    from dotenv import load_dotenv
    if paths["env_file"].exists():
        load_dotenv(paths["env_file"], override=False)
    conn = get_connection()
    # the one taxonomy of MKP / AKP / GKP: the gkp_taxonomy table (base groups + groups GKP created)
    taxonomy, valid_tsgs = _concept_taxonomy(permission, conn)
    store = AkpStore.from_settings(conn, get_db_schema(), s)
    link_table = permission["refs"]["link_table"]          # mkp_ref: the outside documents each MKP names
    registry = FactRegistry.from_settings(conn, get_db_schema(), s)
    models_api = Models(paths, store, s["use_cache"], session, fake_llm)
    embed = embed if embed is not None else make_embedder()
    short = {code: d.get("name", code) for code, d in docs.items()}
    pending_excel = [0]

    def excel(force=False):
        if not s["export_excel"]:
            return
        pending_excel[0] += 0 if force else 1
        if force or pending_excel[0] >= s["excel_every"]:
            p = excel_mod.write(paths["output_dir"] / s["excel_file"], store, registry.export_rows(), short)
            pending_excel[0] = 0
            if force:
                log(f"AKP EXCEL          : {p}")

    def judge_table(prompt, mkp_id):
        for repair in range(2):
            log(f"MODEL                  : Standard Judge ({tier_models['judge']['model']})" + (" / FORMAT RETRY" if repair else ""))
            raw = models_api.role(tier_models["judge"], prompt)
            try:
                return parse_rows(raw, mkp_id)
            except ValueError as exc:
                log(f"STANDARD FORMAT ERROR  : {exc}")
                if repair:
                    raise RuntimeError(f"Standard Judge output is not parseable: {exc}") from exc
                prompt += ("\n\nFORMAT CORRECTION REQUIRED. Return the COMPLETE final table again. Use header "
                           "MKP|TYPE|ELEMENT|CONCEPT|RESULTS. Put TC in TYPE and TC2 in ELEMENT, never TC2 in TYPE. Keep "
                           "exactly five pipe-delimited columns, every A must contain family: in RESULTS, and do not change "
                           "legal meaning. Previous error: " + str(exc) + "\nPREVIOUS RESPONSE:\n" + raw[:8000])

    def comparison(prompt):
        for repair in range(2):
            log(f"MODEL                  : MKP/AKP Comparison ({tier_models['comparison']['model']})" + (" / JSON RETRY" if repair else ""))
            raw = models_api.role(tier_models["comparison"], prompt)
            try:
                return parse_verdict(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                log(f"COMPARISON FORMAT ERROR: {exc}")
                if repair:
                    raise RuntimeError(f"Comparison output is not valid JSON: {exc}") from exc
                prompt += ("\n\nOUTPUT FORMAT FIX: return ONE JSON object only with verdict TRUE/FALSE, reconstructed_mkp, "
                           "missing, added_or_changed, logic_problems, same_matches and references. Previous error: " + str(exc))

    advisory: List[str] = []
    findings: List[str] = []             # word / number / reference errors: the comparison model referees them

    def checks(standard, bundle, source_text, knowledge_type):
        """Blocking errors; advisory notes go to the comparison model instead (legal_word_check). The word, number
        and reference errors are also kept in `findings`; the structure / fact / engine errors are the rest."""
        operative = knowledge_type not in NON_OPERATIVE
        reg_ids = registry.all_ids()
        errs = structure(standard, bundle["sp_rows"], operative, reg_ids)
        errs += registry.check(standard)
        if operative:
            errs += engine_ready(standard, reg_ids)
        words = legal_words(source_text, standard)
        ck = get_field(bundle["mkp_row"], "citation_key") or bundle["key"].split("#", 1)[0]
        for r in standard:
            if r["TYPE"] == "A":
                _, _, invented = refs.for_atom(get_field(r, "statement"), ck, source_text)
                for t in invented:
                    errs.append(f"REFERENCE ADDED: {r['ELEMENT']} refers to {t}, which the source MKP does not refer to")
        mode = s["legal_word_check"]
        soft = [w for w in words if mode == "WARN" or (mode == "STANDARD" and w.startswith("QUALIFIER"))]
        advisory[:] = soft
        findings[:] = [e for e in errs if e.startswith("REFERENCE ADDED")] + [w for w in words if w not in soft]
        return [e for e in errs if not e.startswith("REFERENCE ADDED")] + findings

    def show_false(key, attempt, source_text, standard, verdict):
        log("\n" + "=" * 72 + f"\nSTATUS                  : FALSE / {key} / ATTEMPT {attempt}\n" + "=" * 72)
        log("ORIGINAL STANDARD MKP:\n" + source_text)
        log("\nRECONSTRUCTED FROM STANDARD AKPs:\n" + (verdict.get("reconstructed_mkp") or "(not provided)"))
        log("\nSTANDARD ELEMENT STRUCTURE:")
        for r in standard:
            k = r["TYPE"]
            if k == "A":
                log(f"A  {r['ELEMENT']}: {get_field(r, 'statement')}  facts={get_field(r, 'facts')}")
            elif k == "G":
                log(f"G  {r['ELEMENT']}: {get_field(r, 'logic')} {get_field(r, 'members')}")
            elif k == "LE":
                log(f"LE {r['ELEMENT']}: {get_field(r, 'expr')}")
            elif k in ("OR", "PD", "LK"):
                log(f"{k} {r['ELEMENT']}: {r['RESULTS'][:200]}")
        log("\nWHY FALSE:")
        n = 0
        for f in ("missing", "added_or_changed", "logic_problems"):
            for reason in verdict.get(f, []):
                log(f"- {f.upper()}: {reason}")
                n += 1
        if not n:
            log("- Comparator returned FALSE without a specific reason.")
        log("=" * 72)

    def late_same(key, source_text, standard, vectors, atoms_seen):
        """Bug list A5 (2026-11-19): atoms other workers saved AFTER this law's SAME check (laws run at the same time).
        Inside the save lock, like the facts: the same RAG + Python + SAME model, only against those new atoms."""
        if not any(r["TYPE"] == "A" for r in standard):
            return [], 0
        found = store.same_candidates(key, standard, vectors, min_similarity=s["same_band_min_similarity"],
                                      concept_min=s["same_concept_min_similarity"],
                                      concept_floor=s["same_concept_statement_floor"])
        found = [dict(i, candidates=[c for c in i["candidates"] if c["akp_id"] not in atoms_seen]) for i in found]
        nearest = _same_band(standard, found, s["same_min_similarity"], s["same_concept_min_similarity"])
        if not any(i["candidates"] for i in nearest):
            return [], 0
        log(f"SAME AT SAVE            : {sum(len(i['candidates']) for i in nearest)} atom(s) saved by other workers "
            "since the SAME check - checking them")
        py = _python_same(standard, nearest)
        done = {m["element"] for m in py}
        matches = py + same_check(key, source_text, standard, [i for i in nearest if i["element"] not in done])
        n = _apply_same(standard, matches, nearest, s["same_band_min_similarity"], note=log)
        return [{"element": m.get("element"), "canonical": m.get("canonical"), "same": str(m.get("same_verdict", "")).upper(),
                 "reason": m.get("reason", ""), "found_by": m.get("found_by", "model"), "at_save": True}
                for m in matches], n

    def save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, cache_data, tfact=None, facts_seen=None,
             atoms_seen=None):
        with _save_lock:
            if facts_seen is not None:      # facts other workers registered meanwhile (clauses run at the same time)
                late = fact_check(key, get_field(bundle["mkp_row"], "text"), standard, since=facts_seen)
                if late:
                    cache_data["fact_check"] = list(cache_data.get("fact_check") or []) + late
            if atoms_seen is not None and vectors:   # atoms other workers saved meanwhile (bug list A5)
                try:
                    late, n = late_same(key, get_field(bundle["mkp_row"], "text"), standard, vectors, atoms_seen)
                except Exception as exc:
                    from llm_runtime_errors import is_rate_limited
                    if is_rate_limited(exc):
                        raise
                    log(f"SAME AT SAVE SKIPPED    : {type(exc).__name__}: {str(exc)[:150]} - the after-run sweep checks them")
                    late, n = [], 0
                if late:
                    cache_data["same_check"] = list(cache_data.get("same_check") or []) + late
                    cache_data["same_late"] = n
                    for i in [i for i in vectors if standard[i]["TYPE"] != "A"]:
                        vectors.pop(i)
            # reference design item 7: only the exact atom that names an outside document holds it (outside_refs)
            from ref_ss.auto import mark_outside
            for n in mark_outside(standard, store.outside_refs(link_table, version, key)):
                log(n)
            # abbreviated ids for every element and fact (notes section 3), in place - the cached standard too
            _, notes = abbreviate(standard, mkp_id, registry.all_ids())
            for n in notes:
                log(n)
            bad = id_check(standard, mkp_id)
            if bad:
                raise RuntimeError("ID GATE: " + "; ".join(bad[:8]))
            return _save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, cache_data, tfact)

    def reuse_old(key, bundle, mkp_id, fingerprint, standard, tier, source_text):
        """2026-11-19: atoms from the old AKP cache - no generator / judge / comparison call. Only what was wrong is
        done again: the fact check, the SAME atom check (RAG top 5 -> Python / AI) and the check at save, then saved
        in the new tables (akp + akp cache). Returns the AKP version."""
        log(f"STATUS                  : REUSED old AKP cache ({s['reuse_old_cache_table']}) - fact + SAME check only")
        used = {f for r in standard if r["TYPE"] != "FACTS" for f in FACT_TOKEN.findall(r.get("RESULTS") or "")}
        for n in _detach_old_facts(standard, store.old_fact_defs(s["reuse_old_fact_table"], sorted(used))):
            log(n)
        for w in clean_atom_groups(standard, valid_tsgs):
            log(f"GROUPS                 : {w}")
        facts_seen = set(registry.all_ids())
        fact_decisions = fact_check(key, source_text, standard)
        for n in merge_same_tests(standard):
            log(n)
        vectors, nearest, atoms_seen, matches = {}, [], None, []
        try:
            atoms_seen = store.atom_ids()
            vectors = _vectors(standard, embed, session)
            nearest = _same_band(standard, store.same_candidates(
                key, standard, vectors, min_similarity=s["same_band_min_similarity"],
                concept_min=s["same_concept_min_similarity"], concept_floor=s["same_concept_statement_floor"]),
                s["same_min_similarity"], s["same_concept_min_similarity"])
            py = _python_same(standard, nearest)
            done = {m["element"] for m in py}
            matches = py + same_check(key, source_text, standard, [i for i in nearest if i["element"] not in done])
        except Exception as exc:
            from llm_runtime_errors import is_rate_limited
            if is_rate_limited(exc):
                raise
            log(f"SAME CHECK SKIPPED      : {type(exc).__name__}: {str(exc)[:150]} - atoms kept as new")
        same_n = _apply_same(standard, matches, nearest, s["same_band_min_similarity"], note=log)
        same_decisions = [{"element": m.get("element"), "canonical": m.get("canonical"),
                           "same": str(m.get("same_verdict", "")).upper(), "reason": m.get("reason", ""),
                           "found_by": m.get("found_by", "model")} for m in matches]
        vectors = {i: v for i, v in vectors.items() if standard[i]["TYPE"] == "A"}
        done_data = {"status": "APPROVED", "reused_from": s["reuse_old_cache_table"], "tier": tier,
                     "standard": standard, "fact_check": fact_decisions, "same_check": same_decisions}
        ver = save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, done_data,
                   facts_seen=facts_seen, atoms_seen=atoms_seen)
        same_n += int(done_data.get("same_late") or 0)
        session.counts["approved"] += 1
        session.counts["same_refs"] += same_n
        session.counts["reused_old"] = session.counts.get("reused_old", 0) + 1
        log(f"STATUS                  : TRUE (version {ver})" + (f" - {same_n} atom(s) SAME as existing" if same_n else ""))
        return ver

    def _save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, cache_data, tfact=None):
        mkp_version = bundle.get("mkp_version", 1)
        session.counts["atoms"] += sum(1 for r in standard if r["TYPE"] in ("A", "A_REF"))
        try:
            with conn.cursor() as cur:
                same = store.same_as_latest(cur, instrument, version, key, standard)
                if same is not None:          # identical to the saved AKP: keep that version, note the new MKP version
                    store.touch_mkp_version(cur, instrument, version, key, mkp_version, standard, vectors)
                    ver, new, edges = same, 0, 0
                    session.counts["unchanged"] += 1
                else:
                    ver = store.save_version(cur, instrument, version, key, mkp_id, mkp_version, fingerprint, standard, vectors, tier)
                    new, edges = registry.save(cur, standard, instrument, version, key, ver, embed)
                if tfact:
                    store.save_time_fact(cur, instrument, version, key, tfact)
            store.cache_put(instrument, version, key, fingerprint, {**cache_data, "akp_version": ver}, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        session.counts["new_facts"] += new
        session.counts["fact_links"] += edges
        session.counts["time_facts"] += 1 if tfact else 0
        return ver

    def time_step(key, citation_key, source_text, standard, changes):
        """Old / new law (notes section 6): one call per amended MKP; Python adds valid_from / valid_to and the
        ANY groups. A result the engine could not run is not used (the current law stays). Never blocks the AKP."""
        if not changes:
            return standard, []

        def ask():
            req = timestep.request({"source_mkp": key, "citation_key": citation_key, "text": source_text}, standard, changes)
            log(f"MODEL                  : TIME step ({tier_models['judge']['model']}) - {len(changes)} change(s) after "
                f"{s['gst_start']}")
            return models_api.role(tier_models["judge"], prompts["time"] + "\nINPUT:\n" +
                                   json.dumps(req, ensure_ascii=False, default=str))
        try:
            rows, notes = timestep.run(standard, changes, ask, law_date,
                                       lambda t: engine_ready(t, registry.all_ids()))
        except Exception as exc:
            from llm_runtime_errors import is_fatal, is_rate_limited
            if is_rate_limited(exc) or is_fatal(exc):
                raise
            rows, notes = standard, [f"TIME STEP SKIPPED: {type(exc).__name__}: {str(exc)[:150]} - current law only"]
        for n in notes:
            log(n)
        session.counts["time_changes"] = session.counts.get("time_changes", 0) + sum(n.startswith("TIME T") for n in notes)
        return rows, notes

    def fact_check(key, source_text, standard, since=None):
        """Fact flow layer 3: new facts of an approved AKP against ALL registered facts (one call per MKP, the SAME
        model). SAME -> the existing id, DERIVED -> derived_from + formula, NEW -> kept. Never blocks the AKP."""
        known = registry.all_ids()
        if since is None:
            from akp_ss.facts import concept_warnings, retry_concepts
            # bug list B11: a weak concept finds fewer same facts -> one more try for the NEW facts, then warn
            retry_concepts(standard, known, lambda p: models_api.role(tier_models["same"], p), prompts["concept"],
                           note=log)
            for w in concept_warnings(standard):
                log(f"FACT CONCEPT           : {w}")

        def ask(req):
            prompt = prompts["fact"] + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False, default=str)
            log(f"MODEL                  : FACT check ({tier_models['same']['model']}) - {len(req['new_facts'])} new fact(s)")
            return models_api.role(tier_models["same"], prompt)
        try:
            decisions, asked = check_facts(standard, known, embed,
                                           lambda v, cv=None: registry.nearest(v, s["fact_check_top_k"],
                                                                                 s["fact_check_min_similarity"], cv),
                                           ask, {"source_mkp": key, "text": source_text}, since=since)
        except Exception as exc:
            from llm_runtime_errors import is_fatal, is_rate_limited
            if is_rate_limited(exc) or is_fatal(exc):
                raise
            log(f"FACT CHECK SKIPPED     : {type(exc).__name__}: {str(exc)[:150]} - new facts kept as new")
            return []
        for d in decisions:
            if d["decision"] != "NEW" or asked:
                log(f"FACT                   : {d['fact']} {d['decision']} {d['existing']} - {str(d['reason'])[:120]}")
        session.counts["facts_reused"] = session.counts.get("facts_reused", 0) + sum(d["decision"] == "SAME" for d in decisions)
        session.counts["facts_derived"] = session.counts.get("facts_derived", 0) + sum(d["decision"] == "DERIVED" for d in decisions)
        return decisions

    def same_check(key, source_text, standard, nearest):
        """Separate SAME90 call: which new atoms already exist (similarity > 0.90 candidates only)."""
        if not any(item["candidates"] for item in nearest):
            return []
        data = {"source": {"source_mkp": key, "text": source_text, "instrument": instrument},
                "new_atoms": [r for r in standard if r["TYPE"] == "A"],
                "same_candidates": [{**i, "candidates": [{k: v for k, v in c.items() if k != "element"}
                                                         for c in i["candidates"]]}
                                    for i in nearest if i["candidates"]]}
        prompt = prompts["same"] + "\nINPUT:\n" + json.dumps(data, ensure_ascii=False, default=str)
        for repair in range(2):
            log(f"MODEL                  : SAME check ({tier_models['same']['model']}) - "
                f"{sum(len(i['candidates']) for i in nearest)} candidate(s) > {s['same_min_similarity']}")
            raw = models_api.role(tier_models["same"], prompt)
            try:
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
                obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
                if not isinstance(obj.get("same_matches", []), list):
                    raise ValueError("same_matches must be a list")
                return obj.get("same_matches", [])
            except (ValueError, json.JSONDecodeError) as exc:
                if repair:
                    log(f"SAME CHECK FORMAT ERROR: {exc} - atoms kept as new")
                    return []
                prompt += '\n\nOUTPUT FORMAT FIX: return ONE JSON object {"same_matches": [...]} only. Error: ' + str(exc)
        return []

    from db_insert import get_db_schema as _schema
    refs = RefFinder(conn, instrument, _schema(), permission.get("ikp", {}).get("table_prefix", "ikp"))
    from llm_fallback import route
    tier_models = route(s["models"]["SIMPLE"])
    try:
        store.ensure()
        registry.ensure()
        version = _version(conn, instrument, s["mkp_table"], selection)
        bundles = _read_bundles(conn, instrument, s["mkp_table"], version, selection, only_mkp)
        if only_keys is not None:
            bundles = [cb for cb in bundles if cb[1]["key"] in only_keys]
        mkp_groups = _mkp_groups(conn, s["mkp_table"], version, [b["key"] for _, b in bundles])
        # amendments of the provisions in scope: footnotes of this run + those saved before (old / new law)
        time_facts = store.time_facts(instrument, version) + [
            time_fact(b["mkp_row"], instrument, b["key"]) for _, b in bundles
            if get_field(b["mkp_row"], "knowledge_type").upper() == "AMENDMENT"]
        law_date = _law_date(permission)
        filled = store.backfill(instrument, embed)
        facts_filled = registry.backfill(embed, note=log)          # bug list B7: facts get both embeddings too
        if facts_filled:
            log(f"FACT EMBEDDINGS    : {facts_filled} fact(s) got their missing name / concept embedding")
        log(f"\nAKP / DOCUMENT     : {instrument}")
        log(f"AKP / SOURCE       : {s['mkp_table']} / {version}")
        log(f"AKP / SCOPE        : {selection} / {len(bundles)} MKPs")
        log(f"AKP / TABLES       : {s['table']} + {s['cache_table']} + facts + time facts")
        if filled:
            log(f"AKP / EMBEDDINGS   : {filled} pending atom embeddings filled in")
        for n_b, (cluster, bundle) in enumerate(bundles, 1):
            key, mkp_id = bundle["key"], bundle["mkp_id"]
            row = bundle["mkp_row"]
            source_text = get_field(row, "text")
            knowledge_type = get_field(row, "knowledge_type").upper()
            session.counts["mkps"] += 1
            log(f"\nMKP                    : {key}  [{knowledge_type}]")
            found = store.mkp_id(version, key)
            if found:
                mkp_id = found[0] or mkp_id
            models_api.unit = key
            base = {"source_mkp": key, "mkp_id": mkp_id, "mkp": row, "sp_rows": bundle["sp_rows"], "cluster": cluster,
                    "knowledge_type": knowledge_type, "mkp_groups": mkp_groups.get(key, []),
                    "instrument": {"code": instrument, "name": doc.get("name"), "type": doc.get("type")}}
            mver = int(bundle.get("mkp_version") or 1)
            ck_now = get_field(row, "citation_key") or key.split("#", 1)[0]
            changes = (timestep.changes_for(ck_now, time_facts, s["gst_start"])
                       if knowledge_type not in PYTHON_KINDS else [])
            fp_data = {"mkp_text": source_text, "knowledge_type": knowledge_type,
                       "phrases": [get_field(x, "text") for x in bundle["sp_rows"]], "mkp_version": mver}
            if changes:                       # a new amendment of this provision makes a new AKP (old / new law)
                fp_data["changes"] = [(c["id"], c["kind"], c["effective_from"], c["replaced_text"]) for c in changes]
            fingerprint = hashlib.sha256(json.dumps(fp_data, sort_keys=True, ensure_ascii=False,
                                                    default=str).encode()).hexdigest()
            latest = store.latest(instrument, version, key)
            # CACHE: the saved AKP was made from this same MKP version (and the same amendments) -> reuse it, no model call
            # (headings / amendments are written by Python, no LLM: always rebuilt - a new version only when different)
            if s["use_cache"] and latest and knowledge_type not in PYTHON_KINDS \
                    and (latest["mkp_version"] is None or int(latest["mkp_version"]) == mver) \
                    and (not changes or latest["source_hash"] == fingerprint):
                log(f"STATUS                  : CACHED (AKP version {latest['akp_version']}, MKP version {mver})")
                session.counts["cached"] += 1
                session.counts["atoms"] += latest.get("atoms", 0)
                continue
            reason = ("use_cache=false" if not s["use_cache"] else "no AKP saved yet" if not latest
                      else f"MKP changed (MKP v{latest['mkp_version']} -> v{mver})")
            session.reasons.append((key, reason))

            # ---- HEADING / AMENDMENT / METADATA: Python, no LLM
            if knowledge_type in PYTHON_KINDS:
                tfact = time_fact(row, instrument, key) if knowledge_type == "AMENDMENT" else None
                rows = skip_rows(bundle, knowledge_type, tfact)
                ver = save(key, bundle, mkp_id, fingerprint, rows, {}, "PYTHON", {"status": "APPROVED", "python": True,
                                                                                  "standard": rows}, tfact)
                session.counts["python_written"] += 1
                log(f"STATUS                  : TRUE (PYTHON {knowledge_type}, version {ver})"
                      + (f" - time fact {tfact['kind']} {tfact['effective_from'] or ''}" if tfact else ""))
                excel()
                continue

            tier, score = complexity(source_text, bundle["sp_rows"], knowledge_type, s["complex_threshold"])
            tier_models = route(s["models"][tier])     # Claude down: its Gemini fallback (llm.fallback)
            log(f"TIER                   : {tier} (score {score}) - judge {tier_models['judge']['model']}, "
                f"comparison {tier_models['comparison']['model']}")
            # 2026-11-19: not in the new tables, but approved in the OLD AKP cache for this same MKP input -> its atoms,
            # with only the fact check + SAME check done again (use_cache true; '' setting = off)
            old = _old_standard(store.old_cache(s["reuse_old_cache_table"], instrument, version, key, fingerprint)) \
                if s["use_cache"] and s["reuse_old_cache_table"] else None
            if old:
                try:
                    reuse_old(key, bundle, mkp_id, fingerprint, old, tier, source_text)
                    excel()
                    continue
                except Exception as exc:
                    from llm_runtime_errors import is_fatal, is_rate_limited
                    if is_rate_limited(exc) or is_fatal(exc):
                        raise
                    log(f"REUSE FAILED            : {type(exc).__name__}: {str(exc)[:150]} - AKP runs from the start")

            m = re.match(r"^([A-Z0-9]+_(?:S|R|SCH|PA|AR|EN|O)[0-9A-Z]*)", key.split("#", 1)[0])
            unit_prefix = m.group(1) if m else key.split("#", 1)[0]          # facts already used in this section
            try:
                mkp_vec = embed(source_text)
            except Exception:
                mkp_vec = None
            try:                                # provisions this MKP refers to: their facts are offered first
                _, ref_keys, _ = refs.for_atom(source_text, ck_now, source_text)
            except Exception:
                ref_keys = []
            input_data = {**base, "fact_registry": registry.slice(unit_prefix, mkp_vec, s["fact_slice"],
                                                                  mkp_groups.get(key, []), s["gkp_table"],
                                                                  [k for k in ref_keys if k and k != ck_now])}
            approved, feedback, last_error, last_table = False, "", "", ""
            for attempt in range(1, s["max_attempts"] + 1):
                gemini_raw = claude_raw = ""
                standard, gemini, claude, nearest, vectors = [], [], [], [], {}
                payload = json.dumps(input_data, ensure_ascii=False, default=str)
                if feedback:                        # retry: the AKP is told what went wrong (prompt section RETRY)
                    payload += retry_text(json.loads(feedback), attempt - 1, last_table)
                log(f"ATTEMPT                : {attempt}/{s['max_attempts']}")
                try:
                    gen_prompt = (prompts["akp"] + "\nGST TAXONOMY - the only TSG ids allowed in the groups field:\n" +
                                  taxonomy + CACHE_BREAK + "INPUT:\n" + payload)
                    log(f"MODEL                  : AKP {tier_models['generator_1']['model']} + {tier_models['generator_2']['model']}")
                    stage(f"AKP {n_b}/{len(bundles)} 2 models")
                    gemini_raw, claude_raw = models_api.pair(tier_models, gen_prompt)
                    gemini, claude = raw_rows(gemini_raw, mkp_id), raw_rows(claude_raw, mkp_id)
                    judge_data = {"source": input_data, "gemini": gemini_raw, "claude": claude_raw}
                    if feedback:                    # the judge sees why the comparison said FALSE last time
                        judge_data["previous_comparison_false"] = json.loads(feedback)
                    judge_input = json.dumps(judge_data, ensure_ascii=False, default=str)
                    judge_prompt = (prompts["judge"] + "\n\nGST TAXONOMY - the only TSG ids allowed in the groups field:\n" +
                                    taxonomy + "\nRUNTIME NOTE: gemini and claude inputs are RAW candidate responses. They may "
                                    "have missing headers or minor TYPE/row formatting mistakes. Interpret and correct these "
                                    "while judging. Return one COMPLETE, valid five-column Standard table." + CACHE_BREAK +
                                    "INPUT:\n" + judge_input)
                    stage(f"AKP {n_b}/{len(bundles)} judge")
                    standard = judge_table(judge_prompt, mkp_id)
                    errs = checks(standard, bundle, source_text, knowledge_type)
                    hard = [e for e in errs if e not in findings]
                    for repair in range(s["judge_repairs"]):      # structure errors: the judge repairs them first
                        if not hard:
                            break
                        log(f"PYTHON CHECK           : {len(hard)} structure issue(s) - back to the judge")
                        for e in hard[:6]:
                            log(f"  - {e[:180]}")
                        standard = judge_table(judge_prompt + "\n\nPYTHON_CHECK_FAILED - fix exactly these items:\n" +
                                               "\n".join(f"- {e}" for e in hard) + "\n\nYOUR PREVIOUS TABLE:\n" +
                                               table_text(standard), mkp_id)
                        errs = checks(standard, bundle, source_text, knowledge_type)
                        hard = [e for e in errs if e not in findings]
                    if hard:                        # the engine could not run it: no model can excuse this
                        verdict = {"verdict": "FALSE", "reconstructed_mkp": "(not compared - Python checks failed)",
                                   "missing": [e for e in errs if e.startswith(("MISSING", "NUMBER LOST", "QUALIFIER"))],
                                   "added_or_changed": [e for e in errs if e.startswith(("NEGATION", "MODAL", "NUMBER ADDED"))],
                                   "logic_problems": [e for e in errs if not e.startswith(("MISSING", "NUMBER", "QUALIFIER",
                                                                                           "NEGATION", "MODAL"))],
                                   "same_matches": [], "references": []}
                    else:
                        cmp_data = {"source": input_data, "standard": standard}
                        if advisory:                # e.g. a qualifier Python could not find in words - you decide
                            cmp_data["python_notes"] = advisory
                        found = list(findings)      # word / number findings: the model says REAL or PYTHON_WRONG
                        if found:
                            log(f"PYTHON CHECK           : {len(found)} word / number finding(s) - the comparison decides")
                            for e in found[:6]:
                                log(f"  - {e[:180]}")
                            cmp_data["python_findings"] = [{"id": i, "finding": f} for i, f in enumerate(found, 1)]
                        cmp_input = json.dumps(cmp_data, ensure_ascii=False, default=str)
                        stage(f"AKP {n_b}/{len(bundles)} comparison")
                        verdict = apply_python_review(comparison(prompts["comparison"] + "\nINPUT:\n" + cmp_input), found)
                        for f in verdict["python_findings_rejected"]:
                            log(f"PYTHON FINDING REJECTED: {f[:220]}")
                except Exception as exc:
                    from llm_runtime_errors import is_fatal, is_rate_limited
                    if is_rate_limited(exc) or is_fatal(exc):   # requeue the unit / stop the run
                        raise
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"TECHNICAL ERROR        : {last_error}")
                    store.cache_put(instrument, version, key, fingerprint,
                                    {"status": "TECHNICAL_ERROR", "attempt": attempt, "error": last_error, "gemini": gemini,
                                     "claude": claude, "standard": standard, "gemini_raw": gemini_raw, "claude_raw": claude_raw})
                    continue                                        # next attempt (old engine stopped here)

                if verdict["verdict"] == "TRUE":
                    for w in clean_atom_groups(standard, valid_tsgs):
                        log(f"GROUPS                 : {w}")
                    facts_seen = set(registry.all_ids())
                    fact_decisions = fact_check(key, source_text, standard)
                    # raw-input check (notes section 8): a fact that is a legal conclusion goes back to the models
                    concl = [d for d in fact_decisions if str(d.get("reason", "")).strip().upper().startswith("CONCLUSION")]
                    if concl and attempt < s["max_attempts"]:
                        verdict = {"verdict": "FALSE", "reconstructed_mkp": "(not saved - a fact is a legal conclusion)",
                                   "missing": [], "added_or_changed": [],
                                   "logic_problems": [f"FACT {d['fact']} ('{d['name']}') is a legal conclusion, not a raw "
                                                      f"input: make it an atom / LE built from the raw facts it comes "
                                                      f"from - {str(d['reason'])[:200]}" for d in concl],
                                   "same_matches": [], "references": []}
                    elif concl:
                        for d in concl:
                            log(f"FACT REVIEW            : {d['fact']} looks like a legal conclusion (last attempt - kept)")
                if verdict["verdict"] == "TRUE":
                    for n in merge_same_tests(standard):
                        log(n)
                    standard, time_notes = time_step(key, ck_now, source_text, standard, changes)
                    # references: Python, from each atom's own words + the IKP cross-references
                    ck = get_field(row, "citation_key") or key.split("#", 1)[0]
                    legal = 0
                    for r in standard:
                        if r["TYPE"] == "A":
                            labels, keys, _ = refs.for_atom(get_field(r, "statement"), ck, source_text)
                            set_references(r, labels, keys)
                            legal += len(labels)
                    # SAME: separate check against existing atoms (cosine > 0.90), SAME90 prompt
                    vectors, nearest, atoms_seen = {}, [], None
                    try:
                        atoms_seen = store.atom_ids()     # what this SAME check sees; the save checks what came after
                        vectors = _vectors(standard, embed, session)
                        nearest = _same_band(standard, store.same_candidates(
                            key, standard, vectors, min_similarity=s["same_band_min_similarity"],
                            concept_min=s["same_concept_min_similarity"],
                            concept_floor=s["same_concept_statement_floor"]),
                            s["same_min_similarity"], s["same_concept_min_similarity"])
                        # exact copies are SAME by Python (no model); only the rest go to the SAME model
                        py_matches = _python_same(standard, nearest)
                        done = {m["element"] for m in py_matches}
                        matches = py_matches + same_check(key, source_text, standard,
                                                          [i for i in nearest if i["element"] not in done])
                    except Exception as exc:
                        from llm_runtime_errors import is_rate_limited
                        if is_rate_limited(exc):
                            raise
                        log(f"SAME CHECK SKIPPED      : {type(exc).__name__}: {str(exc)[:150]} - atoms kept as new")
                        matches = []
                    same_n = _apply_same(standard, matches, nearest, s["same_band_min_similarity"], note=log)
                    same_decisions = [{"element": m.get("element"), "canonical": m.get("canonical"),
                                       "same": str(m.get("same_verdict", "")).upper(), "reason": m.get("reason", ""),
                                       "found_by": m.get("found_by", "model")}
                                      for m in matches]          # SAME and NOT SAME both kept (review, no repeat)
                    vectors = {i: v for i, v in vectors.items() if standard[i]["TYPE"] == "A"}
                    done_data = {"status": "APPROVED", "attempt": attempt, "gemini": gemini, "claude": claude,
                                 "standard": standard, "comparison": verdict, "tier": tier,
                                 "fact_check": fact_decisions, "same_check": same_decisions, "time_step": time_notes}
                    ver = save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, done_data,
                               facts_seen=facts_seen, atoms_seen=atoms_seen)
                    same_n += int(done_data.get("same_late") or 0)     # SAME found at save (bug list A5)
                    session.counts["approved"] += 1
                    session.counts["same_refs"] += same_n
                    session.counts["legal_refs"] += legal
                    log(f"STATUS                  : TRUE (version {ver})" + (f" - {same_n} atom(s) SAME as existing" if same_n else ""))
                    approved = True
                    excel()
                    break
                show_false(key, attempt, source_text, standard, verdict)
                feedback = json.dumps(verdict, ensure_ascii=False)
                last_table = table_text(standard) if standard else ""
                store.cache_put(instrument, version, key, fingerprint,
                                {"status": "FAILED", "attempt": attempt, "gemini": gemini, "claude": claude,
                                 "standard": standard, "comparison": verdict})
            if not approved:
                session.counts["failed"] += 1
                session.failures.append(key)
                _log_failure(paths, instrument, key, last_error or feedback)
        excel(force=True)
        c = session.counts
        llm_done = c["approved"] + c["failed"]
        say(f"  AKP : {c['mkps']} MKP(s) - {c['cached']} from database, {llm_done} by LLM, {c['python_written']} by Python "
            f"(headings / amendments) - {c['atoms']} atoms - LLM calls {c['llm_calls']}")
        for key, reason in session.reasons:
            say(f"        LLM for {key}: {reason}")
        if session.failures:
            say("  AKP REVIEW REQUIRED : " + ", ".join(session.failures))
        log("AKP COUNTS " + json.dumps(c))
        return session
    finally:
        conn.close()


def _log_failure(paths: dict, instrument: str, key: str, reason: str) -> None:
    out = Path(paths["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    with (out / "akp_failed_mkps.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"{instrument} | {key} | {' '.join(str(reason).split())[:1500]}\n")
    import kp_errors
    from kp_console import worker
    kp_errors.record(out, instrument, key, "AKP", reason or "comparison FALSE", worker())


def ensure_tables(settings_override=None) -> None:
    """Create / upgrade the AKP and fact tables once, before the worker pool starts."""
    permission = _permission()
    s = validate_akp_settings({**permission.get("akp", {}), **(settings_override or {})})
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        AkpStore.from_settings(conn, get_db_schema(), s).ensure()
        FactRegistry.from_settings(conn, get_db_schema(), s).ensure()
    finally:
        conn.close()


def fact_measures(conn, registry) -> dict:
    """The fact-flow numbers after every run (notes section 8): reuse rate, new facts per MKP, facts used by one
    atom, the open merge list. Python only, no model."""
    from akp_ss import review
    m = review.measures(conn, registry)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE possible_duplicate_of IS NOT NULL").format(
            registry.t(registry.fact_t)))
        m["marked_possible_duplicates"] = int(cur.fetchone()[0] or 0)
    conn.commit()
    flow(f"FACT FLOW  : {m['facts']} facts, {m['fact_links']} links | reuse rate {m['reuse_rate']:.1%} | "
         f"{m['new_facts_per_mkp']} new facts per MKP | {m['facts_used_by_one_atom']} used by one atom | "
         f"{m['marked_possible_duplicates']} marked as possible duplicates (python akp_engine.py --fact-review)")
    return m


def export_excel() -> Optional[Path]:
    """akp.xlsx once at the end of a pool run (workers do not write it), after the fact-flow numbers."""
    permission = _permission()
    s = validate_akp_settings(permission.get("akp", {}))
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = AkpStore.from_settings(conn, get_db_schema(), s)
        registry = FactRegistry.from_settings(conn, get_db_schema(), s)
        store.ensure()
        registry.ensure()
        try:
            fact_measures(conn, registry)
        except Exception as exc:                 # numbers only: never stops the run
            conn.rollback()
            log(f"FACT FLOW  : numbers not read ({type(exc).__name__}: {str(exc)[:120]})")
        if not s["export_excel"]:
            return None
        short = {code: d.get("name", code) for code, d in (permission.get("documents") or {}).items()}
        from akp_ss.review import latest_review
        return excel_mod.write(_paths(permission)["output_dir"] / s["excel_file"], store, registry.export_rows(), short,
                               latest_review(store))
    finally:
        conn.close()


def fact_review(fake_llm: Optional[Callable] = None) -> dict:
    """Fact flow layer 4 (notes section 8): measures + pairs of facts that may be one fact, reviewed by the
    fact-check model. Changes no law; the list is for a person (console, akp.xlsx "Fact review")."""
    from akp_ss import review
    permission = _permission()
    s = validate_akp_settings(permission.get("akp", {}))
    prompts = _prompts(permission)
    from db_insert import get_connection, get_db_schema
    from llm_fallback import route
    conn = get_connection()
    try:
        store = AkpStore.from_settings(conn, get_db_schema(), s)
        registry = FactRegistry.from_settings(conn, get_db_schema(), s)
        store.ensure()
        registry.ensure()
        models_api = Models(_paths(permission), store, s["use_cache"], AkpSession(), fake_llm)
        model = route(s["models"]["SIMPLE"])["same"]
        models_api.unit = review.REVIEW_KEY
        ask = lambda req: models_api.role(model, prompts["fact"] + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False, default=str))
        out = review.review(conn, registry, store, ask, s["fact_review_min_similarity"])
    finally:
        conn.close()
    m = out["measures"]
    say(f"  FACT REVIEW : {m['facts']} facts, {m['fact_links']} links - reuse rate {m['reuse_rate']:.0%}, "
        f"{m['new_facts_per_mkp']} facts per MKP, {m['facts_used_by_one_atom']} used by one atom only")
    same = [p for p in out["pairs"] if p["decision"] in ("SAME", "DERIVED")]
    say(f"  FACT REVIEW : {len(out['pairs'])} close pair(s), {len(same)} to look at (merge: --fact-merge OLD=KEEP)")
    for p in same:
        say(f"        {p['decision']:<8} {p['fact']} ({p['name']})  ~  {p['other']} ({p['other_name']})  "
            f"{p['similarity']} - {str(p['reason'])[:100]}")
    export_excel()
    return out


def fact_merge(old: str, keep: str) -> List[str]:
    """A person approved: fact `old` is the same as `keep` - every law that reads it gets a new AKP version."""
    from akp_ss import review
    permission = _permission()
    s = validate_akp_settings(permission.get("akp", {}))
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = AkpStore.from_settings(conn, get_db_schema(), s)
        registry = FactRegistry.from_settings(conn, get_db_schema(), s)
        store.ensure()
        registry.ensure()
        done = review.merge(conn, registry, store, old, keep)
    finally:
        conn.close()
    say(f"  FACT MERGE : {old} -> {keep} in {len(done)} MKP(s): {', '.join(done[:10])}"
        + (" ..." if len(done) > 10 else ""))
    return done


def _same_tools(fake_llm=None):
    """Connection, stores, settings and the SAME pair model for the SAME check of saved atoms."""
    permission = _permission()
    s = validate_akp_settings(permission.get("akp", {}))
    prompts = _prompts(permission)
    from db_insert import get_connection, get_db_schema
    from llm_fallback import route
    from akp_ss import same_review
    conn = get_connection()
    store = AkpStore.from_settings(conn, get_db_schema(), s)
    registry = FactRegistry.from_settings(conn, get_db_schema(), s)
    store.ensure()
    registry.ensure()
    models_api = Models(_paths(permission), store, s["use_cache"], AkpSession(), fake_llm)
    models_api.unit = same_review.REVIEW_KEY
    model = route(s["models"]["SIMPLE"])["same"]
    ask = lambda prompt: models_api.role(model, prompt)
    return permission, s, prompts, conn, store, registry, ask


def _regroup(changed: List[tuple]) -> None:
    """GKP again for the laws that got a SAME atom (its A_REF takes the kept atom's TSGs)."""
    by_doc: Dict[str, set] = {}
    for instrument, key in changed:
        by_doc.setdefault(instrument, set()).add(key)
    try:
        from gkp_engine import run_gkp_scope
        for instrument, keys in by_doc.items():
            run_gkp_scope(instrument, "ALL", only_keys=keys)
    except Exception as exc:
        say(f"  SAME CHECK : GKP not run again for {sum(len(k) for k in by_doc.values())} law(s) - "
            f"{type(exc).__name__}: {str(exc)[:150]} (run gkp_engine.py)")


def same_review(fake_llm: Optional[Callable] = None) -> dict:
    """Bug list A5, step 1: pairs of saved atoms of different laws that may be one atom, checked by Python / the
    SAME model, written to Output/<same_review_file> with an APPROVE column. Changes no law."""
    from akp_ss import same_review as SR
    permission, s, prompts, conn, store, _registry, ask = _same_tools(fake_llm)
    try:
        pairs = SR.find_pairs(store, s["same_review_min_similarity"], s["same_concept_min_similarity"],
                              s["same_concept_statement_floor"], s["same_review_top_k"])
        for mkp, aid in SR.orphans(store):              # bug list F24
            say(f"  SAME REVIEW : {mkp} points to {aid}, which is no longer a saved atom - run it again")
        say(f"  SAME REVIEW : {len(pairs)} close pair(s) of saved atoms - checking")
        SR.decide(pairs, ask, prompts["pair"], s["same_review_batch"], note=log)
    finally:
        conn.close()
    from kp_settings import build_paths
    path = SR.write_excel(Path(build_paths(permission)["output_dir"]) / s["same_review_file"],
                          sorted(pairs, key=lambda p: (p.get("ai") != "SAME", -p["similarity"])))
    same = sum(p.get("ai") == "SAME" for p in pairs)
    say(f"  SAME REVIEW : {same} SAME (Python {sum(p.get('found_by') == 'python' for p in pairs)}), "
        f"{len(pairs) - same} not - {path}")
    say("  SAME REVIEW : mark APPROVE = YES on the rows to map, save the file, then run  "
        "python akp_engine.py --same-apply")
    return {"pairs": pairs, "file": str(path)}


def same_apply() -> List[tuple]:
    """Bug list A5, step 2: map the rows a person marked YES in Output/<same_review_file> (new AKP versions)."""
    from akp_ss import same_review as SR
    from kp_settings import build_paths
    permission, s, _prompts_, conn, store, registry, _ask = _same_tools()
    path = Path(build_paths(permission)["output_dir"]) / s["same_review_file"]
    if not path.is_file():
        conn.close()
        raise FileNotFoundError(f"{path} not found - run  python akp_engine.py --same-review  first")
    try:
        approved = SR.read_approved(path)
        say(f"  SAME APPLY : {len(approved)} row(s) marked YES in {path.name}")
        changed = SR.apply(conn, store, registry, SR.plan([dict(a, ai="SAME", similarity=1.0) for a in approved]),
                           note=say)
    finally:
        conn.close()
    say(f"  SAME APPLY : {len(changed)} law(s) got a new AKP version")
    if changed:
        _regroup(changed)
        say("  SAME APPLY : now run  python governance_master_engine.py --all  (and relationship_builder.py --full)")
    return changed


def same_sweep(source_mkps: List[str], fake_llm: Optional[Callable] = None) -> List[tuple]:
    """Bug list A4: after the workers, the atoms saved in this run are checked against every saved atom (laws run in
    parallel never saw each other). SAME pairs are mapped automatically. Returns the laws changed."""
    from akp_ss import same_review as SR
    if not source_mkps:
        return []
    _permission_, s, prompts, conn, store, registry, ask = _same_tools(fake_llm)
    try:
        if not s["same_sweep_after_run"]:
            return []
        for mkp, aid in SR.orphans(store):              # bug list F24: its saved atom changed or is gone
            say(f"  SAME CHECK : {mkp} points to {aid}, which is no longer a saved atom - run "
                f"{mkp.split('#')[0]} again (use_cache false for AKP)")
        pairs = SR.find_pairs(store, s["same_review_min_similarity"], s["same_concept_min_similarity"],
                              s["same_concept_statement_floor"], s["same_review_top_k"], only_mkps=source_mkps)
        if not pairs:
            say("  SAME CHECK : no close atoms among this run's laws")
            return []
        SR.decide(pairs, ask, prompts["pair"], s["same_review_batch"], note=log)
        mapping = SR.plan(pairs)
        say(f"  SAME CHECK : {len(pairs)} close pair(s), {len(mapping)} SAME - mapping")
        changed = SR.apply(conn, store, registry, mapping, note=say)
    finally:
        conn.close()
    if changed:
        _regroup(changed)
    return changed


def fact_sweep(source_mkps: List[str], fake_llm: Optional[Callable] = None) -> List[str]:
    """Bug list B8 (net 2): after the workers, the facts this run registered are checked against every registered
    fact (same rule as the fact check: name + nl and concept, >= fact_check_min_similarity, the fact-check model);
    a SAME pair is merged into the first-registered fact. Net 1 (the second check at save) stays as it is."""
    from akp_ss import review
    if not source_mkps:
        return []
    _permission_, s, prompts, conn, store, registry, ask = _same_tools(fake_llm)
    try:
        if not s["fact_sweep_after_run"]:
            return []
        merges = review.sweep(conn, registry, store,
                              lambda req: ask(prompts["fact"] + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False, default=str)),
                              s["fact_check_min_similarity"], source_mkps, note=say)
    finally:
        conn.close()
    say(f"  FACT CHECK : {len(merges)} fact(s) of this run merged into an earlier fact" if merges else
        "  FACT CHECK : no duplicate facts among this run's new facts")
    return merges


def main_akp():
    permission = _permission()
    settings = validate_akp_settings(permission.get("akp", {}))
    if not settings["enabled"]:
        log("AKP : SKIPPED")
        return []
    from kp_settings import selected_documents
    documents = selected_documents(permission)
    if not documents:
        raise RuntimeError("No PDF selected: check \"read\" in permission.json and the Law sub-folders")
    selection = settings["scope_key"] or settings["section"]
    if not selection and settings["ask_user_input"]:
        selection = input("AKP section number / citation key / ALL: ").strip() or "ALL"
    selection = selection or "ALL"
    from kp_settings import resolve
    law_dir = resolve(permission["paths"]["law_dir"])
    return [run_akp_scope(law_dir / d["file"], d["code"], scope_key=selection, only_mkp=settings["only_mkp"])
            for d in documents]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DARP LAW ARC AKP")
    parser.add_argument("--pdf", help="PDF filename (optional: use permission.json selected documents)")
    parser.add_argument("--instrument", help="Legal document code such as CGSTA17")
    parser.add_argument("--section", help="Section 7 or explicit citation key")
    parser.add_argument("--mkp", help="One MKP ID or citation key")
    parser.add_argument("--fact-review", action="store_true", help="fact clean-up: measures + facts that may be one fact")
    parser.add_argument("--fact-merge", metavar="OLD=KEEP", help="merge a fact a person approved (new AKP versions)")
    parser.add_argument("--same-review", action="store_true",
                        help="SAME atoms already saved: pairs checked by Python / the model -> Output/same_review.xlsx")
    parser.add_argument("--same-apply", action="store_true",
                        help="map the rows marked APPROVE = YES in Output/same_review.xlsx (new AKP versions)")
    args = parser.parse_args()
    if args.same_review:
        same_review()
    elif args.same_apply:
        same_apply()
    elif args.fact_review:
        fact_review()
    elif args.fact_merge:
        old_id, _, keep_id = args.fact_merge.partition("=")
        fact_merge(old_id.strip(), keep_id.strip())
    elif args.pdf:
        run_akp_scope(BASE_DIR / args.pdf, args.instrument, args.section, only_mkp=args.mkp)
    else:
        main_akp()
