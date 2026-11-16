"""DARP LAW ARC / GKP: approved AKP atoms -> GST groups (TG) / sub-groups (TSG).

Flow per MKP (latest approved AKP rows):
  A atom      1. groups the MKP and AKP judges already gave it (groups field)        -> APPROVED, no LLM (AKP_GROUP)
              2. notification / circular: the MKP's groups, else the document's groups (its other MKPs, or the
                 MKPs that cite it)                                                    -> APPROVED, no LLM
              3. nothing yet: candidates (concept + atom embeddings, include terms, anchor provisions, shared facts,
                 references, section neighbours) -> Gemini + Claude verify in parallel -> judge only on
                 disagreement -> wider embedding search
              4. still nothing: GKP CREATE (permission.json features.gkp_create_groups) - the proposer picks an
                 existing TSG after all, or a new sub-group under an existing TG, or a new TG; Python reuses an
                 existing TSG whose meaning is the same (embedding similarity) instead of creating a near copy.
                 New groups are written to the taxonomy file with created_by GKP. Nothing is left UNCLASSIFIED.
  A_REF       SAME atom: inherits the existing atom's approved groups (no LLM)
  AMENDMENT   Python: the taxonomy's amendment TSG (TSG14_02) + the groups of the provision it amends
  HEADING     skipped (no testable condition)
Saved per MKP as the next version (gkp_version, is_latest, inserted_at) with group / subgroup names and status
APPROVED / REVIEW / UNCLASSIFIED. After the document: coverage check of anchor provisions, Excel.
Uses project modules: db_insert, vector_store, llm_runtime, transport, akp_engine (_version, _in_scope).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor  # noqa: F401  (kept for parity with other passes)
from pathlib import Path
from typing import Callable, Dict, List, Optional

from gkp_ss import excel as excel_mod
from gkp_ss import taxonomy as tax
from gkp_ss import score as gscore
from gkp_ss.candidates import CandidateFinder
from gkp_ss.store import GkpStore, provision_prefix
from gkp_ss.verify import Verifier, parse_create
from kp_console import log, say, stage
from llm_fallback import route
from transport import get_field

BASE_DIR = Path(__file__).resolve().parent
CLASSIFIER_VERSION = "GKP_V3_GROUPS_FIRST"
TSG_RE = re.compile(r"\bTSG\d{2}_\d{2}\b")
_create_lock = __import__("threading").Lock()     # one worker at a time may create a group
ROLES = ("verifier_1", "verifier_2", "judge", "proposer")


def _permission() -> dict:
    from kp_settings import load
    return load()


def _path(v) -> Path:
    p = Path(v)
    return p if p.is_absolute() else BASE_DIR / p


def _hash(v) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


GKP_METHOD = "M3"     # M3: groups from MKP / AKP first, LLM only for the rest, created groups instead of unclassified


def validate_gkp_settings(raw: dict, permission: dict) -> dict:
    s = dict(raw or {})
    for k, d in (("enabled", False), ("use_cache", True), ("export_excel", True), ("ask_user_input", False),
                 ("taxonomy_synced", False), ("coverage", True)):
        v = s.get(k, d)
        if type(v) is not bool:
            raise ValueError(f"gkp.{k} must be true or false")
        s[k] = v
    default_tax = "Config/gst_taxonomy.json" if _path("Config/gst_taxonomy.json").is_file() \
        else "Config/gst_taxonomy.json"
    s["taxonomy_file"] = s.get("taxonomy_file", default_tax)
    s["excel_file"] = s.get("excel_file", "gkp.xlsx")
    s["scope_key"] = str(s.get("scope_key", "")).strip()
    s["top_k_per_vector"] = max(1, min(20, int(s.get("top_k_per_vector", 5))))
    s["broad_top_k_per_vector"] = max(10, min(30, int(s.get("broad_top_k_per_vector", 20))))
    s["max_candidates"] = max(4, min(25, int(s.get("max_candidates", 12))))
    s["max_atoms_per_request"] = max(1, min(50, int(s.get("max_atoms_per_request", 20))))
    s["max_attempts"] = max(1, min(3, int(s.get("max_attempts", 2))))
    s["complex_pairs"] = int(s.get("complex_pairs", 40))
    # meaning-based candidates from atoms of other documents (0 = off)
    s["similar_atoms"] = max(0, min(20, int(s.get("similar_atoms", 8))))
    s["similar_atom_min"] = float(s.get("similar_atom_min", 0.75))
    s["akp_table"] = s.get("akp_table") or permission.get("akp", {}).get("table", "akp")
    s["mkp_table"] = s.get("mkp_table") or permission.get("akp", {}).get("mkp_table", "mkp")
    s["table_prefix"] = s.get("table_prefix", "gkp")
    s["create_groups"] = bool(s["create_groups"] if "create_groups" in s
                              else (permission.get("features") or {}).get("gkp_create_groups", True))
    s["create_reuse_similarity"] = float(s.get("create_reuse_similarity", 0.85))
    # the Python check of the AKP's group (notes section 1)
    for k in ("confirm_min_similarity", "confirm_top_n", "audit_share", "calibrate"):
        if k not in s:
            raise ValueError(f"Config/settings.json gkp.{k} is missing")
    s["confirm_min_similarity"] = float(s["confirm_min_similarity"])
    s["confirm_top_n"] = max(1, int(s["confirm_top_n"]))
    s["audit_share"] = min(1.0, max(0.0, float(s["audit_share"])))
    if type(s["calibrate"]) is not bool:
        raise ValueError("gkp.calibrate must be true or false")
    refs = permission.get("refs") or {}
    s["ref_document_table"] = refs.get("document_table", "ref_document")
    s["ref_link_table"] = refs.get("link_table", "mkp_ref")
    models = s.get("models") or {}
    for tier in ("SIMPLE", "COMPLEX"):
        for role in ROLES:
            r = (models.get(tier) or {}).get(role)
            if not isinstance(r, dict) or not r.get("llm") or not r.get("model"):
                raise ValueError(f"permission.json llm.gkp.{tier}.{role} needs llm and model")
    s["models"] = models
    return s


def _paths(permission: dict) -> dict:
    from kp_settings import build_paths
    return build_paths(permission)


class Models:
    """llm_runtime with per-call model, raw answers cached in gkp_llm_cache."""

    def __init__(self, paths, store, use_cache, counts, fake=None):
        self.paths, self.store, self.use_cache, self.counts, self.fake = paths, store, use_cache, counts, fake
        self.runtimes = {}
        self.unit = ""                    # the MKP being grouped (saved with each cached answer)

    def __call__(self, provider: str, model: str, prompt: str) -> str:
        key = hashlib.sha256(f"{provider}|{model}|{prompt}".encode("utf-8")).hexdigest()
        if self.use_cache:
            hit = self.store.llm_get(key)
            if hit is not None:
                return hit
        if self.fake is not None:
            raw = self.fake(provider, model, prompt)
        else:
            from llm_runtime import call_llm, create_llm_runtime
            if provider not in self.runtimes:
                self.runtimes[provider] = create_llm_runtime(provider, self.paths)
            rt = dict(self.runtimes[provider])
            rt["model"] = model
            raw, _ = call_llm(rt, prompt)
        self.counts["llm_calls"] += 1
        self.store.llm_put(key, f"{provider}:{model}", raw, self.unit)
        return raw


def _embedder(embed=None):
    if embed is not None:
        return embed, "TEST"
    from vector_store import embed_text, load_vector_config
    cfg = load_vector_config()
    if not cfg["enabled"]:
        raise RuntimeError("Set vector.enabled=true: GKP compares taxonomy embeddings with AKP atom embeddings")
    return (lambda text: embed_text(text, "RETRIEVAL_DOCUMENT", cfg)), cfg["embedding_model"]


def _row(base: dict, sub: Optional[dict], status: str, method: str, reason: str, cand: Optional[dict] = None,
         dec: Optional[dict] = None, python_score: Optional[float] = None) -> dict:
    return dict(base, tg_id=sub["tg_id"] if sub else "", tg_name=sub["tg_name"] if sub else "",
                tsg_id=sub["tsg_id"] if sub else "", tsg_name=sub["name"] if sub else "",
                status=status, method=method, reason=reason,
                concept_score=(cand or {}).get("concept_score"), atom_score=(cand or {}).get("atom_score"),
                sources=(cand or {}).get("sources", []), model_1=(dec or {}).get("model_1"), model_2=(dec or {}).get("model_2"),
                python_score=python_score)


def run_gkp_scope(instrument, scope_key="ALL", version_id=None, fake_llm: Optional[Callable] = None,
                    embed: Optional[Callable] = None, settings_override: Optional[dict] = None, only_keys=None):
    """Group the latest approved AKP atoms of one document / scope into the GST taxonomy.
    only_keys: group only these MKPs of the scope (the worker pool's heading job of a split unit)."""
    permission = _permission()
    s = validate_gkp_settings({**permission.get("gkp", {}), **(settings_override or {})}, permission)
    if not s["enabled"]:
        log("GKP : SKIPPED")
        return {}
    data, subs = tax.load(_path(s["taxonomy_file"]))
    by_id = {x["tsg_id"]: x for x in subs}
    core = {g["tg_id"] for g in data["groups"]}
    tax_id = data.get("taxonomy_id", "TAXONOMY")
    tv = f"{tax_id}:{tax.taxonomy_hash(subs)}"
    tvm = f"{tv}|{GKP_METHOD}"                 # saved on every row: taxonomy + how candidates were found
    amend_tsg = data.get("amendment_tsg", "TSG14_02")
    doc_type = str(((permission.get("documents") or {}).get(str(instrument).upper()) or {}).get("type") or "").upper()
    # a notification / circular, or any outside document the scraper brought in (it has a ref_id, e.g. an amending
    # Act ACT_31_2018): its atoms take the groups of the laws that cite it, no grouping LLM (notes section 13)
    doc_info = (permission.get("documents") or {}).get(str(instrument).upper()) or {}
    subordinate = doc_type not in ("ACT", "RULES", "") or bool(doc_info.get("ref_id"))
    prompt = (_path(permission["paths"]["prompt_dir"]) / permission["files"]["gkp_prompt"]).read_text(encoding="utf-8-sig")
    paths = _paths(permission)
    from dotenv import load_dotenv
    if paths["env_file"].exists():
        load_dotenv(paths["env_file"], override=False)
    from db_insert import get_connection, get_db_schema
    from akp_engine import _in_scope, _version
    embed_fn, emb_model = _embedder(embed)
    conn = get_connection()
    store = GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"])
    reasons: List[tuple] = []
    counts = {"mkps": 0, "atoms": 0, "unchanged": 0, "approved_mappings": 0, "agree": 0, "judge": 0, "same_inherited": 0,
              "amendment_rows": 0, "akp_group": 0, "mkp_group": 0, "review": 0, "unclassified": 0, "groups_created": 0,
              "create_existing": 0, "create_reused": 0, "cached_mkps": 0, "skipped_non_operative": 0,
              "coverage_gaps": 0, "llm_calls": 0, "taxonomy_embeddings": 0}
    created: List[str] = []
    base_ids = {x["tsg_id"] for x in subs}
    by_tg_name = {g["tg_id"]: g["name"] for g in data["groups"]}
    from llm_runtime_marker import CACHE_BREAK
    call = Models(paths, store, s["use_cache"], counts, fake_llm)
    finder = CandidateFinder(store, by_id, s, f"{s['table_prefix']}_taxonomy")
    try:
        store.ensure()
        if not s["taxonomy_synced"]:          # the worker pool syncs once before the workers start
            counts["taxonomy_embeddings"] = tax.sync(conn, get_db_schema(), subs, embed_fn, emb_model,
                                                     f"{s['table_prefix']}_taxonomy")
        # the ONE taxonomy (notes section 9): the gkp_taxonomy table - base groups + the groups GKP created
        data, subs = tax.current(conn, get_db_schema(), f"{s['table_prefix']}_taxonomy", _path(s["taxonomy_file"]))
        by_id.clear()
        by_id.update({x["tsg_id"]: x for x in subs})
        core.clear()
        core.update(g["tg_id"] for g in data["groups"])
        by_tg_name.update({g["tg_id"]: g["name"] for g in data["groups"]})
        tv = f"{tax_id}:{tax.taxonomy_hash(subs)}"
        tvm = f"{tv}|{GKP_METHOD}"
        finder = CandidateFinder(store, by_id, s, f"{s['table_prefix']}_taxonomy")
        version = version_id or _version(conn, instrument, s["mkp_table"], scope_key or "ALL")
        mkps = {k: v for k, v in store.mkps(instrument, version).items()
                if _in_scope(provision_prefix(k), instrument, scope_key or "ALL")
                and (only_keys is None or k in only_keys)}
        if not mkps:
            raise RuntimeError(f"No approved AKP rows for {instrument}, {version}, {scope_key}")
        mkp_groups = store.mkp_groups(s["mkp_table"], version, list(mkps))
        doc_groups = (store.document_groups(s["mkp_table"], s["ref_document_table"], s["ref_link_table"], instrument)
                      if subordinate else [])
        log(f"\nGKP / DOCUMENT     : {instrument}")
        log(f"GKP / SCOPE        : {scope_key or 'ALL'} / {len(mkps)} MKPs / {version}")
        log(f"GKP / TAXONOMY     : {s['taxonomy_file']} - {len(data['groups'])} groups / {len(subs)} subgroups "
              f"({counts['taxonomy_embeddings']} embedded now)")

        def classify(atoms: List[dict], mkp: str, mkp_id: str, context: list) -> Dict[str, List[dict]]:
            """Phases CORE -> CORE_EXPANDED -> CREATE. Returns rows per element."""
            stage(f"GKP verify {len(atoms)} atom(s)")
            out = {a["element"]: [] for a in atoms}
            ready = [a for a in atoms if not a.get("blocked")]
            for a in atoms:
                if a.get("blocked"):
                    out[a["element"]] = [_row(a["base"], None, "REVIEW", "NONE", a["blocked"])]
            pairs = sum(len(a["candidates"]) for a in ready)
            tier = "COMPLEX" if pairs > s["complex_pairs"] else "SIMPLE"
            verifier = Verifier(call, prompt, route(s["models"][tier]), s["max_attempts"], log=log)
            ctx = {"document": instrument, "version": version, "source_mkp": mkp, "mkp_id": mkp_id, "context": context}

            def run_phase(trial, phase):
                for i in range(0, len(trial), s["max_atoms_per_request"]):
                    batch = trial[i:i + s["max_atoms_per_request"]]
                    payload_atoms = [{"element": a["element"], "type": a["type"], "concept": a.get("concept", ""),
                                      "family": a.get("family", ""), "results": a.get("results", ""),
                                      "local_results": a.get("local_results", ""),
                                      "candidates": [{k: c[k] for k in ("tsg_id", "tg_id", "group", "subgroup", "definition",
                                                                       "include", "exclude", "key_provisions", "concept_score",
                                                                       "atom_score", "sources")} for c in a["candidates"]]}
                                     for a in batch]
                    decisions = verifier.decide(payload_atoms, ctx, phase)
                    for a in batch:
                        for c in a["candidates"]:
                            d = decisions.get((a["element"], c["tsg_id"]))
                            if d and d["status"] in ("APPROVED", "REVIEW"):
                                out[a["element"]].append(_row(a["base"], by_id[c["tsg_id"]], d["status"], d["method"],
                                                              d["reason"] or "verdict missing; review required", c, d))
            run_phase([a for a in ready if a["candidates"]], "CORE")
            # wider embedding search inside every group for atoms with no approved / review membership
            empty = [a for a in ready if not out[a["element"]]]
            broad = []
            for a in empty:
                seen = {c["tsg_id"] for c in a["candidates"]}
                more = finder.find(a, core, s["broad_top_k_per_vector"], exclude=seen, full=False)
                if more:
                    broad.append(dict(a, candidates=more))
            run_phase(broad, "CORE_EXPANDED")
            left = [a for a in ready if not out[a["element"]]]
            if left and s["create_groups"]:
                for el, row in create_groups(left, mkp, context, tier).items():
                    out[el] = [row]
            for a in left:
                if not out[a["element"]]:
                    out[a["element"]] = [_row(a["base"], None, "UNCLASSIFIED", "NONE",
                                              "no sub-group approved and creating groups is switched off"
                                              if not s["create_groups"] else "GKP could not place or create a group")]
            return out

        def create_groups(left: List[dict], mkp: str, context: list, tier: str) -> Dict[str, dict]:
            """GKP CREATE: the proposer places each left-over atom in an existing TSG, or describes a new
            sub-group (under an existing TG or a new TG). Python reuses an existing TSG with the same meaning."""
            stage("GKP create group")
            m = route(s["models"][tier])["proposer"]
            payload = {"mode": "CREATE", "document": instrument, "version": version, "source_mkp": mkp, "context": context,
                       "atoms": [{"element": a["element"], "concept": a.get("concept"), "family": a.get("family"),
                                  "results": a.get("results"), "local_results": a.get("local_results")} for a in left]}
            try:
                raw = call(m["llm"], m["model"], prompt + "\n\nGST TAXONOMY:\n" + tax.compact_text(subs) +
                           CACHE_BREAK + "INPUT JSON:\n" + json.dumps(payload, ensure_ascii=False, default=str))
                decisions = parse_create(raw, left, set(by_id), core)
            except Exception as exc:
                from llm_runtime_errors import is_fatal, is_rate_limited
                if is_rate_limited(exc) or is_fatal(exc):
                    raise
                log(f"GKP CREATE             : skipped - {type(exc).__name__}: {str(exc)[:150]}")
                return {}
            out: Dict[str, dict] = {}
            made: Dict[str, str] = {}                 # same new name in one answer -> one group
            for d in decisions:
                a = next(x for x in left if x["element"] == d["element"])
                if d["action"] == "EXISTING":
                    out[a["element"]] = _row(a["base"], by_id[d["tsg_id"]], "APPROVED", "CREATE_EXISTING", d["reason"])
                    counts["create_existing"] += 1
                    continue
                key = re.sub(r"[^a-z0-9]+", " ", d["name"].lower()).strip()
                sid = made.get(key) or new_group(d, mkp)
                made[key] = sid
                out[a["element"]] = _row(a["base"], by_id[sid], "APPROVED",
                                         "GKP_CREATED" if sid not in base_ids else "CREATE_REUSED", d["reason"])
            return out

        def new_group(d: dict, mkp: str) -> str:
            """Create (or reuse) one sub-group; one worker at a time."""
            nonlocal subs, finder
            with _create_lock:
                norm = lambda t: re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()
                same_name = next((x["tsg_id"] for x in by_id.values() if norm(x["name"]) == norm(d["name"])), None)
                if same_name:                         # a group of this name exists (made earlier): reuse it
                    counts["create_reused"] += 1
                    return same_name
                cand = {"tsg_id": "NEW", "tg_id": d.get("tg_id") or "", "tg_name": d.get("tg_name") or "",
                        "name": d["name"], "definition": d["definition"],
                        "include": list(dict.fromkeys([d["name"]] + list(d["include"]))),
                        "exclude": d["exclude"], "key_provisions": []}
                vec = None
                try:
                    vec = embed_fn(tax.embed_text(dict(cand, tg_name=cand["tg_name"] or by_tg_name.get(cand["tg_id"], ""))))
                except Exception as exc:
                    log(f"GKP CREATE             : embedding failed ({type(exc).__name__}) - no reuse check")
                if vec:
                    near = store.nearest_subgroup(f"{s['table_prefix']}_taxonomy",
                                                  "[" + ",".join(str(float(x)) for x in vec) + "]")
                    if near and near[1] >= s["create_reuse_similarity"] and near[0] in by_id:
                        log(f"GKP CREATE             : '{d['name']}' is the same as {near[0]} ({near[1]:.2f}) - reused")
                        counts["create_reused"] += 1
                        return near[0]
                tg = d.get("tg_id") if d.get("tg_id") in core else ""
                sid = tax.add_subgroup(_path(s["taxonomy_file"]), tg, d["name"], d["definition"], d["include"],
                                       d["exclude"], d.get("tg_name", ""), d.get("tg_description", ""), mkp)
                _, file_subs = tax.load(_path(s["taxonomy_file"]))
                tax.sync(conn, get_db_schema(), file_subs, embed_fn, emb_model, f"{s['table_prefix']}_taxonomy")
                _, subs = tax.current(conn, get_db_schema(), f"{s['table_prefix']}_taxonomy", _path(s["taxonomy_file"]))
                by_id.clear()
                by_id.update({x["tsg_id"]: x for x in subs})
                core.update(x["tg_id"] for x in subs)
                finder = CandidateFinder(store, by_id, s, f"{s['table_prefix']}_taxonomy")   # its words too
                counts["groups_created"] += 1
                created.append(f"{sid} {d['name']} ({by_id[sid]['tg_id']} {by_id[sid]['tg_name']})")
                log(f"GKP CREATE             : new sub-group {sid} '{d['name']}' under {by_id[sid]['tg_id']}")
                return sid

        def is_amendment(rows):
            r = next((x for x in rows if x["type"] == "MKP"), rows[0])
            return get_field({"RESULTS": r["results"]}, "knowledge_type").upper() == "AMENDMENT"
        # provisions first, amendments last: an amendment takes the groups of the provision it amends
        ordered = sorted(mkps.items(), key=lambda kv: (is_amendment(kv[1]), kv[0]))
        for mkp, raw_rows in ordered:
            mkp_row = next((r for r in raw_rows if r["type"] == "MKP"), raw_rows[0])
            kt = get_field({"RESULTS": mkp_row["results"]}, "knowledge_type").upper()
            mkp_id = mkp_row.get("mkp_id") or ""
            atom_rows = [r for r in raw_rows if r["type"] in ("A", "A_REF")]
            counts["mkps"] += 1
            if not atom_rows and kt != "AMENDMENT":
                counts["skipped_non_operative"] += 1
                continue
            context = [{"type": r["type"], "element": r["element"], "results": str(r["results"])[:3000]}
                       for r in raw_rows if r["type"] not in ("A", "A_REF")]
            # CACHE: grouped before from this same AKP version -> reuse, no model call
            akp_now = store.akp_state(instrument, version, mkp) or {}
            state = store.latest_state(instrument, version, mkp)
            same_akp = bool(state and akp_now) and (
                (state["akp_version"] is not None and state["akp_version"] == akp_now.get("akp_version"))
                or (state["akp_version"] is None and state["inserted_at"] is not None
                    and akp_now.get("inserted_at") is not None and state["inserted_at"] >= akp_now["inserted_at"]))
            saved_tv = str((state or {}).get("taxonomy_version") or "")
            # grouped with another taxonomy (other TG / TSG ids) -> grouped again
            method_ok = saved_tv.split(":", 1)[0] == tax_id
            if (s["use_cache"] and same_akp and not state["review"] and method_ok
                    and (not state["unclassified"] or saved_tv.split("|", 1)[0] == tv)):
                counts["cached_mkps"] += 1
                log(f"MKP                    : {mkp} - GROUPED (CACHED, version {state['gkp_version']})")
                continue
            reason = ("use_cache=false" if not s["use_cache"] else "no GKP saved yet" if not state
                      else "REVIEW rows are checked again" if same_akp and state["review"]
                      else "grouped again with the new taxonomy"
                      if same_akp and not method_ok
                      else "the taxonomy changed for UNCLASSIFIED atoms" if same_akp
                      else f"AKP changed (AKP v{state['akp_version'] or '?'} -> v{akp_now.get('akp_version')})")
            reasons.append((mkp, reason))
            rows: List[dict] = []
            atoms: List[dict] = []
            inherited_keys = []
            akp_check: Dict[str, dict] = {}         # atoms whose AKP group goes to the verifiers (score low / audit)
            calib: List[list] = []
            if kt == "AMENDMENT":
                base = {"instrument": instrument, "version_id": version, "source_mkp": mkp, "mkp_id": mkp_id, "akp_id": None,
                        "akp_element": mkp_row["element"], "akp_type": "MKP", "concept": "Amendment record"}
                lk = next((r for r in raw_rows if r["type"] == "LK" and "AMENDS" in (r["results"] or "")), None)
                target = get_field({"RESULTS": lk["results"]}, "target") if lk else provision_prefix(mkp)
                if amend_tsg in by_id:
                    rows.append(_row(base, by_id[amend_tsg], "APPROVED", "AMENDMENT_RULE",
                                     f"amendment record of {target} (time fact)"))
                for m in store.approved_for_provision(target):
                    if m["tsg_id"] in by_id and m["tsg_id"] != amend_tsg:
                        rows.append(_row(base, by_id[m["tsg_id"]], "APPROVED", "AMENDMENT_TARGET",
                                         f"amends {target}, which belongs to this subgroup"))
                inherited_keys.append(("amend", target, sorted(r["tsg_id"] for r in rows)))
            for r in atom_rows:
                base = {"instrument": instrument, "version_id": version, "source_mkp": mkp, "mkp_id": mkp_id,
                        "akp_id": r["akp_id"], "akp_element": r["element"], "akp_type": r["type"], "concept": r["concept"] or ""}
                counts["atoms"] += 1
                if r["type"] == "A_REF":
                    canonical = r.get("same_akp_id") or ""
                    if not canonical:                    # older AKP format: same_ref "inst::ver::mkp::element"
                        parts = get_field({"RESULTS": r["results"]}, "same_ref").split("::")
                        canonical = f"{parts[-2]}::{parts[-1]}" if len(parts) >= 2 else ""
                    got = store.approved_for_akp(canonical) if canonical else []
                    if got:
                        for m in got:
                            if m["tsg_id"] in by_id:
                                rows.append(_row(base, by_id[m["tsg_id"]], "APPROVED", "SAME_INHERITED",
                                                 f"same meaning as {canonical}"))
                        inherited_keys.append(("same", canonical, sorted(m["tsg_id"] for m in got)))
                        continue
                    ref = store.canonical_atom(canonical) if canonical else None
                    if not ref:
                        rows.append(_row(base, None, "REVIEW", "NONE", f"SAME atom {canonical or '?'} not found or not approved"))
                        continue
                    data_row = dict(r, concept=ref["concept"], results=ref["results"], concept_vector=ref["concept_vector"],
                                    atom_vector=ref["atom_vector"])
                    facts = store.atom_facts(instrument, version, ref["source_mkp"], ref["element"])
                else:
                    data_row = r
                    given = [t for t in dict.fromkeys(TSG_RE.findall(get_field({"RESULTS": r["results"] or ""}, "groups")))
                             if t in by_id]
                    method = "AKP_GROUP"
                    if not given and subordinate:     # notification / circular: its MKP's or its document's groups
                        given = [t for t in mkp_groups.get(mkp, []) if t in by_id]
                        method = "MKP_GROUP"
                        if not given:
                            given = [t for t in doc_groups if t in by_id]
                            method = "DOCUMENT_GROUP"
                    if given and method != "AKP_GROUP":
                        for t in given:
                            rows.append(_row(base, by_id[t], "APPROVED", method,
                                             "group of its MKP" if method == "MKP_GROUP"
                                             else "group of this document (or of the provisions that cite it)"))
                        counts["mkp_group"] += 1
                        inherited_keys.append((method, r["element"], given))
                        continue
                    facts = store.atom_facts(instrument, version, mkp, r["element"])
                    if given:                     # AKP's group: checked by the Python score, the LLM only when unsure
                        probe = {"element": r["element"], "concept": r["concept"] or "", "results": r["results"] or "",
                                 "family": get_field({"RESULTS": r["results"] or ""}, "family"), "source_mkp": mkp,
                                 "facts": facts, "concept_vector": r.get("concept_vector"),
                                 "atom_vector": r.get("atom_vector"), "base": base}
                        sims, top = finder.closeness(probe, core, given, s["confirm_top_n"])
                        found = {c["tsg_id"]: c for c in finder.find(probe, core, s["top_k_per_vector"])}
                        checked = {t: gscore.clues(t, sims, top, mkp_groups.get(mkp, []),
                                                   (found.get(t) or {}).get("sources", [])) for t in given}
                        high = all(gscore.is_high(c, s["confirm_min_similarity"]) for c in checked.values())
                        audit = gscore.audited(r["akp_id"] or r["element"], s["audit_share"])
                        if high and not audit and not s["calibrate"]:
                            for t in given:
                                c = checked[t]
                                rows.append(_row(base, by_id[t], "APPROVED", gscore.CONFIRMED,
                                                 "AKP group confirmed by Python: " + gscore.why(c), found.get(t),
                                                 python_score=c["similarity"]))
                            counts["akp_group"] += 1
                            inherited_keys.append((gscore.CONFIRMED, r["element"], given))
                            continue
                        counts["akp_group_llm"] = counts.get("akp_group_llm", 0) + 1
                        akp_check[r["element"]] = {"given": given, "clues": checked,
                                                   "why": "calibration" if s["calibrate"] else "audit" if high and audit
                                                   else "Python not sure"}
                atom = {"element": r["element"], "type": r["type"], "concept": data_row["concept"] or "",
                        "results": data_row["results"] or "", "family": get_field({"RESULTS": data_row["results"] or ""}, "family"),
                        "local_results": r["results"] if r["type"] == "A_REF" else "", "source_mkp": mkp, "facts": facts,
                        "concept_vector": data_row.get("concept_vector"), "atom_vector": data_row.get("atom_vector"), "base": base}
                if not (atom["concept_vector"] or atom["atom_vector"]):
                    atom["blocked"] = "No AKP embeddings for this AKP yet (filled on the next AKP run)"
                    atom["candidates"] = []
                else:
                    atom["candidates"] = finder.find(atom, core, s["top_k_per_vector"])
                    for t in (akp_check.get(r["element"]) or {}).get("given", []):   # AKP's group is always a candidate
                        if t not in {c["tsg_id"] for c in atom["candidates"]}:
                            extra = finder.find(atom, core, s["broad_top_k_per_vector"], full=True)
                            hit = next((c for c in extra if c["tsg_id"] == t), None) or {
                                "tsg_id": t, "concept_score": None, "atom_score": None, "sources": [],
                                "tg_id": by_id[t]["tg_id"], "group": by_id[t]["tg_name"], "subgroup": by_id[t]["name"],
                                "definition": by_id[t]["definition"], "include": by_id[t]["include"],
                                "exclude": by_id[t]["exclude"], "key_provisions": by_id[t].get("key_provisions") or []}
                            atom["candidates"].append(dict(hit, sources=list(hit["sources"]) + ["AKP group"]))
                atoms.append(atom)
            fp = _hash({"v": CLASSIFIER_VERSION, "prompt": prompt, "models": s["models"], "inherited": inherited_keys,
                        # only stable candidate sources: fact / reference / section sources change as other MKPs
                        # get grouped and would make every run look new (use_cache=false re-checks with them)
                        "atoms": [(a["element"], a["type"], a["concept"], a["results"], a.get("blocked"),
                                   sorted((c["tsg_id"], c["definition"], tuple(c["include"]), tuple(c["exclude"]))
                                          for c in a["candidates"]
                                          if any(x.startswith(("concept", "atom", "term", "anchor")) for x in c["sources"])))
                                  for a in atoms]})
            log(f"\nMKP                    : {mkp} / {len(atoms)} atoms" + (" / amendment" if kt == "AMENDMENT" else ""))
            for a in atoms:
                srcs = "; ".join(f"{c['tsg_id']}[{', '.join(c['sources'][:3])}]" for c in a["candidates"][:5])
                log(f"  {a['element']:<20} {a['concept'][:28]:<28} {srcs[:150]}")
            call.unit = mkp
            if atoms:
                for el, rs in classify(atoms, mkp, mkp_id, context).items():
                    chk = akp_check.get(el)
                    for x in rs:                  # the verifiers on an AKP group: kept or changed (notes section 1)
                        if chk and x["status"] == "APPROVED" and x["method"] in ("AGREE", "JUDGE"):
                            kept = x["tsg_id"] in chk["given"]
                            c = chk["clues"].get(x["tsg_id"]) or max(chk["clues"].values(), key=lambda v: v["similarity"])
                            x["python_score"] = c["similarity"]
                            x["reason"] = (f"{chk['why']}; " + gscore.why(c) + "; " +
                                           f"{x['method'].lower()}: " + str(x["reason"] or ""))[:2000]
                            x["method"] = gscore.CONFIRMED_LLM if kept else gscore.CHANGED
                    if chk:                       # calibration data: did the LLM keep the AKP group?
                        kept = any(x["status"] == "APPROVED" and x["tsg_id"] in chk["given"] for x in rs)
                        for t, c in chk["clues"].items():
                            calib.append([c["similarity"], bool(c["closest"] and (c["mkp_agrees"] or c["anchor"]
                                                                                   or c["facts"])), kept])
                    rows.extend(rs)
                if calib:
                    store.calibration_put(instrument, mkp, calib)
            for r in rows:
                r["taxonomy_version"] = tvm
                r["input_sha256"] = fp
                r["akp_version"] = akp_now.get("akp_version")
            ver = store.same_as_latest(instrument, version, mkp, rows)
            if ver is not None:                      # same memberships as saved: keep that version
                store.touch_akp_version(instrument, version, mkp, akp_now.get("akp_version"), tvm)
                counts["unchanged"] += 1
            else:
                ver = store.save_version(instrument, version, mkp, rows)
            for r in rows:
                counts["approved_mappings"] += r["status"] == "APPROVED"
                counts["agree"] += r["status"] == "APPROVED" and r["method"] == "AGREE"
                counts["judge"] += r["status"] == "APPROVED" and r["method"] == "JUDGE"
                counts["same_inherited"] += r["method"] == "SAME_INHERITED"
                counts["amendment_rows"] += r["method"].startswith("AMENDMENT")
                counts["review"] += r["status"] == "REVIEW"
                counts["unclassified"] += r["status"] == "UNCLASSIFIED"
            for el in dict.fromkeys(r["akp_element"] for r in rows):
                mine = [r for r in rows if r["akp_element"] == el]
                ok = [f"{r['tsg_id']} {r['tsg_name']} ({r['method']})" for r in mine if r["status"] == "APPROVED"]
                log(f"  FINAL {el:<14}: " + ("; ".join(ok) if ok else mine[0]["status"] + " - " + mine[0]["reason"][:90]))
            log(f"SAVED                  : version {ver} / {sum(r['status'] == 'APPROVED' for r in rows)} approved, "
                  f"{sum(r['status'] != 'APPROVED' for r in rows)} review / unclassified")
        # coverage of the anchor provisions: once per document (the worker pool does it at the end, not per job)
        gaps = [g for g in store.coverage(instrument, version, subs) if g[6] == "GAP"] if s["coverage"] else []
        counts["coverage_gaps"] = len(gaps)
        for g in gaps[:20]:
            log(f"COVERAGE GAP           : {g[3]} has {g[4]} atom(s), none approved in {g[2]} {by_id[g[2]]['name']}")
        if s["export_excel"]:
            p = excel_mod.write(_path(permission["paths"]["output_dir"]) / s["excel_file"], store, by_id)
            log(f"GKP EXCEL              : {p}")
        tsg = store.tsg_counts(instrument, version, list(mkps))
        counts["tsg_counts"] = tsg
        counts["source_mkps"] = list(mkps)
        llm_mkps = counts["mkps"] - counts["cached_mkps"] - counts["skipped_non_operative"]
        say(f"  GKP : {counts['mkps']} MKP(s) - {counts['cached_mkps']} from database, {llm_mkps} grouped now "
            f"({counts['akp_group']} atom(s) from AKP groups, {counts['mkp_group']} from MKP / document groups) - "
            f"LLM calls {counts['llm_calls']}" + (f" - {counts['unclassified']} atom(s) UNCLASSIFIED" if counts["unclassified"] else "")
            + (f" - {counts['review']} to REVIEW" if counts["review"] else ""))
        for c in created:
            say(f"        NEW GROUP (created by GKP) : {c}")
        counts["created"] = created
        for key, reason in reasons:
            say(f"        LLM for {key}: {reason}")
        if tsg:
            say("        atoms per TSG : " + ", ".join(f"{k} {v['atoms']}" for k, v in tsg.items()))
        log("GKP COUNTS " + json.dumps({k: v for k, v in counts.items() if k not in ("tsg_counts", "source_mkps", "created")}))
        return counts
    finally:
        conn.close()


def sync_taxonomy(embed: Optional[Callable] = None) -> int:
    """Embed the taxonomy once before the worker pool starts (workers then skip it: taxonomy_synced)."""
    permission = _permission()
    s = validate_gkp_settings(permission.get("gkp", {}), permission)
    _, subs = tax.load(_path(s["taxonomy_file"]))
    from db_insert import get_connection, get_db_schema
    embed_fn, model = _embedder(embed)
    conn = get_connection()
    try:
        return tax.sync(conn, get_db_schema(), subs, embed_fn, model, f"{s['table_prefix']}_taxonomy")
    finally:
        conn.close()


def ensure_tables(settings_override: Optional[dict] = None) -> None:
    """Create / upgrade the GKP tables once, before the worker pool starts."""
    permission = _permission()
    s = validate_gkp_settings({**permission.get("gkp", {}), **(settings_override or {})}, permission)
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"]).ensure()
    finally:
        conn.close()


def export_excel(instruments: Optional[List[str]] = None, settings_override: Optional[dict] = None) -> Optional[Path]:
    """Once at the end of a pool run (workers do neither): the coverage check of the documents of the run, then
    gkp.xlsx."""
    permission = _permission()
    s = validate_gkp_settings({**permission.get("gkp", {}), **(settings_override or {})}, permission)
    _, subs = tax.load(_path(s["taxonomy_file"]))
    from akp_engine import _version
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"])
        store.ensure()
        for inst in instruments or []:
            try:
                gaps = [g for g in store.coverage(inst, _version(conn, inst, s["mkp_table"], "ALL"), subs) if g[6] == "GAP"]
                for g in gaps[:20]:
                    log(f"COVERAGE GAP           : {g[3]} has {g[4]} atom(s), none approved in {g[2]}")
            except Exception as exc:
                conn.rollback()
                log(f"COVERAGE {inst} : skipped ({str(exc)[:120]})")
        if not s["export_excel"]:
            return None
        return excel_mod.write(_path(permission["paths"]["output_dir"]) / s["excel_file"], store,
                               {x["tsg_id"]: x for x in subs})
    finally:
        conn.close()


def main_gkp():
    """Standalone: group existing AKPs for each enabled document."""
    permission = _permission()
    s = validate_gkp_settings(permission.get("gkp", {}), permission)
    if not s["enabled"]:
        log("GKP : SKIPPED")
        return []
    scope = s["scope_key"] or "ALL"
    if s["ask_user_input"]:
        scope = input("GKP scope (citation key / ALL): ").strip() or scope
    from kp_settings import selected_documents
    return [run_gkp_scope(d["code"], scope_key=scope) for d in selected_documents(permission)]


def calibration_report() -> List[dict]:
    """How often the verifiers kept the AKP's group, per similarity floor (atoms with the other clues ok).
    Set gkp.confirm_min_similarity to the lowest floor where they agree almost always (98-99%)."""
    permission = _permission()
    s = validate_gkp_settings(permission.get("gkp", {}), permission)
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"])
        store.ensure()
        pairs = store.calibration_rows()
    finally:
        conn.close()
    table = gscore.calibration([(sim, kept) for sim, ok, kept in pairs if ok])
    say(f"  GKP CALIBRATION : {len(pairs)} checked group(s), {sum(1 for p in pairs if p[1])} with the other clues ok"
        f" (now: confirm_min_similarity {s['confirm_min_similarity']}, calibrate {s['calibrate']})")
    for t in table:
        say(f"        similarity >= {t['from_score']:.2f} : {t['atoms']:>5} atom(s), LLM kept the AKP group "
            + (f"{t['llm_agrees']:.1%}" if t["llm_agrees"] is not None else "-"))
    return table


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DARP LAW ARC GKP (groups the saved AKPs of the selected documents)")
    ap.add_argument("--calibration", action="store_true", help="how often the LLM kept the AKP group, per similarity")
    if ap.parse_args().calibration:
        calibration_report()
    else:
        main_gkp()
