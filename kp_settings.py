"""LAW ARC settings: permission.json (your switches) + Config/settings.json (fixed settings) + the Law folder.

permission.json   run / use_cache per step, which kinds of law to read, scope, governance master choice, and the
                  LLM + model of every role ({"llm": "GEMINI" | "CLAUDE", "model": "..."}).
Config/settings.json  paths, table names, file names, thresholds, word lists (rarely changed).
Law/Acts, Law/Rules, Law/Notifications, Law/Circulars
                  every PDF in a folder whose kind is "read": true in permission.json is processed. Its name, code
                  and type come from Config/documents.json (keyed by file name); a new PDF is added there
                  automatically, with a code made from its file name.

permission.json also holds: workers (pool size, LLM calls at once per provider), features (downloads, cross
references, web scraping, GKP-created groups, prompt caching), access (folders the scripts may read / write, internet,
database delete) and console (FLOW or DETAIL). allowed(cfg, ...) is the one check every script uses before it reads or
writes a folder, goes on the internet or deletes rows.

load() returns one dict with the sections the engines read: paths, files, documents, ikp, mkp, akp, gkp,
governance_master, vector, workers, features, access, console, fact_catalog.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
PERMISSION_FILE = BASE_DIR / "permission.json"
SETTINGS_FILE = BASE_DIR / "Config" / "settings.json"
STEPS = ("ikp", "mkp", "akp", "gkp", "governance_master")
FEATURES = ("download_missing_pdfs", "cross_ref_record", "web_scraping", "push_amending_notifications",
            "gkp_create_groups", "prompt_caching")
LLMS = ("GEMINI", "CLAUDE")
ROLES = {"mkp": ("generator_1", "generator_2", "judge"),
         "akp": ("generator_1", "generator_2", "judge", "comparison", "same"),
         "gkp": ("verifier_1", "verifier_2", "judge", "proposer")}
TIERS = ("SIMPLE", "COMPLEX")

_cache: Optional[dict] = None


class SettingsError(ValueError):
    pass


def _read(path: Path) -> dict:
    if not path.is_file():
        raise SettingsError(f"File not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SettingsError(f"{path.name} must contain a JSON object")
    return data


def resolve(value: str) -> Path:
    p = Path(str(value).strip())
    return p if p.is_absolute() else BASE_DIR / p


def _role(value, where: str) -> dict:
    if not isinstance(value, dict) or not value.get("llm") or not value.get("model"):
        raise SettingsError(f'permission.json {where} must be {{"llm": "GEMINI" or "CLAUDE", "model": "..."}}')
    llm = str(value["llm"]).strip().upper()
    if llm not in LLMS:
        raise SettingsError(f"permission.json {where}.llm must be GEMINI or CLAUDE (got {value['llm']})")
    return {"llm": llm, "model": str(value["model"]).strip()}


def _models(perm: dict, step: str) -> dict:
    block = (perm.get("llm") or {}).get(step) or {}
    out = {}
    for tier in TIERS:
        t = block.get(tier) or {}
        out[tier] = {role: _role(t.get(role), f"llm.{step}.{tier}.{role}") for role in ROLES[step]}
    return out


def _workers(perm: dict) -> dict:
    w = perm.get("workers") or {}
    n = w.get("count", 1)
    if type(n) is not int or not 1 <= n <= 32:
        raise SettingsError("permission.json workers.count must be a whole number from 1 to 32")
    at_once = {}
    for llm in LLMS:
        v = (w.get("max_calls_at_once") or {}).get(llm, n)
        if type(v) is not int or v < 1:
            raise SettingsError(f"permission.json workers.max_calls_at_once.{llm} must be a whole number of 1 or more")
        at_once[llm] = v
    return {"count": n, "max_calls_at_once": at_once}


def _features(perm: dict) -> dict:
    f = perm.get("features") or {}
    bad = [k for k in FEATURES if type(f.get(k)) is not bool]
    if bad:
        raise SettingsError(f"permission.json features: set {', '.join(bad)} to true or false")
    return {k: f[k] for k in FEATURES}


def _access(perm: dict) -> dict:
    a = perm.get("access") or {}
    for k in ("read_folders", "write_folders"):
        if not isinstance(a.get(k), list):
            raise SettingsError(f"permission.json access.{k} must be a list of folder names")
    for k in ("internet", "database_delete"):
        if type(a.get(k)) is not bool:
            raise SettingsError(f"permission.json access.{k} must be true or false")
    return {"read_folders": [str(x).strip("/\\") for x in a["read_folders"]],
            "write_folders": [str(x).strip("/\\") for x in a["write_folders"]],
            "internet": a["internet"], "database_delete": a["database_delete"]}


class AccessDenied(PermissionError):
    pass


def _top(path) -> str:
    p = Path(path)
    try:
        p = p.resolve().relative_to(BASE_DIR)
    except ValueError:
        return str(p)
    return p.parts[0] if p.parts else ""


def allowed(cfg: dict, action: str, target=None) -> bool:
    """The one access check: action READ / WRITE (target = a path), INTERNET or DELETE."""
    a = cfg["access"]
    if action == "INTERNET":
        return a["internet"]
    if action == "DELETE":
        return a["database_delete"]
    folder = _top(target)
    return folder in (a["read_folders"] if action == "READ" else a["write_folders"])


def require(cfg: dict, action: str, target=None) -> None:
    """Like allowed(), but stops with the permission.json key to change."""
    if allowed(cfg, action, target):
        return
    key = {"INTERNET": "access.internet", "DELETE": "access.database_delete",
           "READ": "access.read_folders", "WRITE": "access.write_folders"}[action]
    what = f" ({_top(target)})" if target is not None else ""
    raise AccessDenied(f"permission.json {key} does not allow {action.lower()}{what}")


# ---------------------------------------------------------------- documents from the Law folder

def _derive(file_name: str, doc_type: str) -> dict:
    """Name and code for a PDF that is not in Config/documents.json yet (from its file name)."""
    stem = Path(file_name).stem
    words = re.sub(r"[_\-]+", " ", stem).strip()
    nums = re.findall(r"\d+", stem)
    if doc_type == "NOTIFICATION":
        n, y = (nums + ["0", "0"])[:2]
        rest = re.sub(r"^\s*\d+\s*\d+\s*", "", words).strip()
        rest = re.sub(r"\bRate\b", "(Rate)", rest)
        return {"name": f"Notification {int(n)}/{y}" + (f"-{rest}" if rest else ""), "code": f"N{int(n):02d}_{y}"}
    if doc_type == "CIRCULAR":
        return {"name": "Circular " + "/".join(nums[:3]) + (" - " + re.sub(r"(?i)^\s*circular[\s\d]*", "", words).strip()
                                                             if re.sub(r"(?i)^\s*circular[\s\d]*", "", words).strip() else ""),
                "code": "C" + "_".join(nums[:3])}
    year = next((y[-2:] for y in nums if len(y) == 4), "")
    head = re.split(r"(?i)\b(act|rules?)\b", words)[0].strip()
    letters = "".join(w if w.isupper() else w[0].upper() for w in head.split() if w)
    part = re.search(r"(?i)\bpart\s*([A-Z])\b", words)
    if doc_type == "RULES":
        return {"name": f"{head} Rules".strip() + (f" Part {part.group(1).upper()}" if part else ""), "code": f"{letters}R{year}" + (part.group(1).upper() if part and part.group(1).upper() != "A" else "")}
    return {"name": f"{head} Act".strip(), "code": f"{letters}A{year}"}


def guess_type(file_name: str) -> str:
    """ACT / RULES / NOTIFICATION / CIRCULAR from the file name; "" when the name does not say."""
    n = file_name.lower()
    if "circular" in n:
        return "CIRCULAR"
    if "notification" in n or re.search(r"(central|integrated|union territory|state)[ _-]*tax", n):
        return "NOTIFICATION"
    if re.search(r"(^|[^a-z])rules?([^a-z]|$)", n):
        return "RULES"
    if re.search(r"(^|[^a-z])act([^a-z]|$)", n):
        return "ACT"
    return ""


def discover_documents(perm: dict, settings: dict, write_registry: bool = True) -> Dict[str, dict]:
    """{code: {name, code, file, type, read}} for every PDF under the Law kind folders. A document fetched only
    because a law cites it (documents.json reference_only, 2026-11-22) is not read as a main document - only the
    part each citing law needs runs - unless permission.json documents.<code> is true."""
    law_dir = resolve(settings["paths"]["law_dir"])
    reg_path = resolve(settings["files"]["documents_registry"])
    registry = _read(reg_path) if reg_path.is_file() else {}
    wanted = {k: bool(v) for k, v in (perm.get("read") or {}).items()}
    per_doc = {str(k).upper(): v for k, v in (perm.get("documents") or {}).items() if not str(k).startswith("_")}
    bad = [k for k, v in per_doc.items() if type(v) is not bool]
    if bad:
        raise SettingsError(f"permission.json documents: set {', '.join(bad)} to true or false")
    unknown = set(wanted) - set(settings["law_folders"])
    if unknown:
        raise SettingsError(f"permission.json read: unknown kind(s) {sorted(unknown)}; use {sorted(settings['law_folders'])}")
    docs: Dict[str, dict] = {}
    added = False
    # PDFs left directly in Law/ (old layout): move the ones whose kind is known into their sub-folder
    by_type: Dict[str, str] = {}
    for spec in settings["law_folders"].values():       # first folder of a type (Law/Acts, not Law/Amendments)
        by_type.setdefault(spec["type"], spec["folder"])
    if law_dir.is_dir():
        for pdf in sorted(law_dir.glob("*.pdf")):
            info = registry.get(pdf.name)
            folder = by_type.get(str((info or {}).get("type", "") or guess_type(pdf.name)).upper())
            if folder:
                (law_dir / folder).mkdir(exist_ok=True)
                if not (law_dir / folder / pdf.name).exists():
                    pdf.rename(law_dir / folder / pdf.name)
                    print(f"LAW FOLDER : moved {pdf.name} -> Law/{folder}/")
            else:
                print(f"LAW FOLDER : {pdf.name} is directly in Law/ - move it into Law/Acts, Law/Rules, "
                      "Law/Notifications or Law/Circulars so it can be read")
    for kind, spec in settings["law_folders"].items():
        folder = law_dir / spec["folder"]
        if not folder.is_dir():
            continue
        for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower()):
            info = registry.get(pdf.name)
            if not isinstance(info, dict):
                info = {**_derive(pdf.name, spec["type"]), "type": spec["type"]}
                registry[pdf.name] = info
                added = True
            code = str(info["code"]).strip().upper()
            if code in docs:
                raise SettingsError(f"Two PDFs have the code {code}: {docs[code]['file']} and {spec['folder']}/{pdf.name} - "
                                    f"give one a different code in {reg_path.name}")
            if not info.get("ref_id"):
                from ref_ss.ids import ref_id_for_document
                info["ref_id"] = ref_id_for_document(info["name"], str(info.get("type") or spec["type"]), code)
                added = True
            docs[code] = {"name": info["name"], "code": code, "file": f"{spec['folder']}/{pdf.name}",
                          "type": str(info.get("type") or spec["type"]).upper(), "url": str(info.get("url") or ""),
                          "ref_id": info["ref_id"], "missing": False,
                          "read": per_doc.get(code, False if info.get("reference_only") else wanted.get(kind, False))}
    # registry entries whose PDF is not in the folder yet: downloaded at start when they have a url
    folders = {spec["type"]: (kind, spec["folder"]) for kind, spec in settings["law_folders"].items()}
    for file_name, info in registry.items():
        if file_name.startswith("_") or not isinstance(info, dict):
            continue
        code = str(info.get("code") or "").strip().upper()
        kind_folder = folders.get(str(info.get("type") or "").upper())
        if not code or code in docs or not kind_folder or not str(info.get("url") or "").strip():
            continue                  # a PDF you removed (no url to fetch it again) is simply not read
        kind, folder = kind_folder
        docs[code] = {"name": info.get("name", code), "code": code, "file": f"{folder}/{file_name}",
                      "type": str(info["type"]).upper(), "url": str(info.get("url") or ""),
                      "ref_id": info.get("ref_id") or "", "missing": True,
                      "read": per_doc.get(code, False if info.get("reference_only") else wanted.get(kind, False))}
    if added and write_registry:
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return docs


# ---------------------------------------------------------------- one merged config

def load(fresh: bool = False, permission: Optional[dict] = None, settings: Optional[dict] = None) -> dict:
    global _cache
    if _cache is not None and not fresh and permission is None and settings is None:
        return copy.deepcopy(_cache)
    perm = permission if permission is not None else _read(PERMISSION_FILE)
    st = settings if settings is not None else _read(SETTINGS_FILE)
    run, cache = perm.get("run") or {}, perm.get("use_cache") or {}
    for name, block in (("run", run), ("use_cache", cache)):
        bad = [s for s in STEPS if type(block.get(s)) is not bool]
        if bad:
            raise SettingsError(f"permission.json {name}: set {', '.join(bad)} to true or false")
    scope = perm.get("scope") or {}
    gm = perm.get("governance_master") or {}
    llm = perm.get("llm") or {}
    cfg = {"paths": dict(st["paths"]), "files": dict(st["files"]), "law_folders": st["law_folders"]}
    ikp_role = _role(llm.get("ikp"), "llm.ikp")
    cfg["ikp"] = {**st["ikp"], "enabled": run["ikp"], "use_cache": cache["ikp"],
                  "llm": {"mode": st["ikp"].get("llm_mode", "ON_FAIL"), "provider": ikp_role["llm"],
                          "model": ikp_role["model"], "chapters": st["ikp"].get("llm_chapters", [])}}
    cfg["mkp"] = {**st["mkp"], "enabled": run["mkp"], "use_cache": cache["mkp"],
                  "ask_user_input": bool(scope.get("ask_user_input", True)), "scope": scope.get("default", "FULL"),
                  "models": _models(perm, "mkp")}
    cfg["akp"] = {**st["akp"], "enabled": run["akp"], "use_cache": cache["akp"], "models": _models(perm, "akp")}
    cfg["gkp"] = {**st["gkp"], "enabled": run["gkp"], "use_cache": cache["gkp"], "models": _models(perm, "gkp")}
    cfg["governance_master"] = {**st["governance_master"], "enabled": run["governance_master"],
                                "use_cache": cache["governance_master"], "tsgs": gm.get("tsgs", "TOUCHED"),
                                "law_as_on": str(gm.get("law_as_on", "") or "")}
    cfg["workers"] = _workers(perm)
    cfg["features"] = _features(perm)
    cfg["access"] = _access(perm)
    show = str((perm.get("console") or {}).get("show", "FLOW")).upper()
    if show not in ("FLOW", "DETAIL"):
        raise SettingsError("permission.json console.show must be FLOW or DETAIL")
    cfg["console"] = {"show": show}
    if type(run.get("fact_catalog", False)) is not bool:
        raise SettingsError("permission.json run.fact_catalog must be true or false")
    cfg["fact_catalog"] = {**(st.get("fact_catalog") or {}), "enabled": bool(run.get("fact_catalog", False))}
    if not isinstance(st.get("relationship"), dict) or not all(k in st["relationship"] for k in ("table", "page")):
        raise SettingsError('Config/settings.json "relationship" needs table and page')
    cfg["relationship"] = dict(st["relationship"])
    cfg["pool"] = dict(st.get("pool") or {})
    cfg["refs"] = dict(st.get("refs") or {})
    if llm.get("ref_link"):          # web scraping (ref_engine.py): the model that finds a document's official link
        cfg["refs"]["link_finder"] = _role(llm["ref_link"], "llm.ref_link")
    if llm.get("ref_link_retry"):    # the stronger model that searches once more when ref_link found nothing
        cfg["refs"]["link_retry"] = _role(llm["ref_link_retry"], "llm.ref_link_retry")
    cfg["llm_fallback"] = _fallback(llm.get("fallback"))
    emb = llm.get("embedding") or {}
    cfg["vector"] = {**st["vector"], "embedding_model": str(emb.get("model") or "text-embedding-005")}
    cfg["documents"] = discover_documents(perm, st)
    check_tables(cfg)
    if permission is None and settings is None:
        _cache = cfg
    return copy.deepcopy(cfg)


TABLE_KEYS = {          # every table after MKP has its own name in Config/settings.json (2026-11-19: all end in _new)
    "mkp": ("table", "taxonomy_table"),
    "akp": ("mkp_table", "table", "cache_table", "llm_cache_table", "fact_table", "atom_fact_table", "time_fact_table",
            "gkp_table"),
    "gkp": ("akp_table", "mkp_table", "table", "cache_table", "llm_cache_table", "coverage_table", "taxonomy_table",
            "atom_fact_table"),
    "governance_master": ("table", "history_table"),
    "relationship": ("table",),
    "fact_catalog": ("table", "index_table"),
}
SAME_TABLE = (         # the same table named in two places must have the same name
    (("akp", "table"), ("gkp", "akp_table")), (("akp", "gkp_table"), ("gkp", "table")),
    (("akp", "atom_fact_table"), ("gkp", "atom_fact_table")),
    (("mkp", "table"), ("akp", "mkp_table")), (("mkp", "table"), ("gkp", "mkp_table")),
    (("akp", "table"), ("governance_master", "tables", "akp")), (("akp", "fact_table"), ("governance_master", "tables", "fact")),
    (("akp", "atom_fact_table"), ("governance_master", "tables", "atom_fact")),
    (("akp", "time_fact_table"), ("governance_master", "tables", "time_fact")),
    (("gkp", "table"), ("governance_master", "tables", "gkp")), (("mkp", "table"), ("governance_master", "tables", "mkp")),
    (("refs", "link_table"), ("governance_master", "tables", "ref_link")),
)


def check_tables(cfg: dict) -> None:
    """Every table name is read from Config/settings.json - no name is made in the code. A missing name, or one table
    named differently in two sections, stops the run with the key."""
    for sec, keys in TABLE_KEYS.items():
        for k in keys:
            if not str((cfg.get(sec) or {}).get(k) or "").strip():
                raise SettingsError(f"Config/settings.json {sec}.{k} (table name) is missing")
    for k in ("akp", "fact", "atom_fact", "gkp", "time_fact", "mkp", "ref_link"):
        if not str(((cfg.get("governance_master") or {}).get("tables") or {}).get(k) or "").strip():
            raise SettingsError(f"Config/settings.json governance_master.tables.{k} (table name) is missing")
    for a, b in SAME_TABLE:
        va, vb = (_dig(cfg, a), _dig(cfg, b))
        if va != vb:
            raise SettingsError(f"Config/settings.json {'.'.join(a)} = {va!r} but {'.'.join(b)} = {vb!r} - "
                                "the same table must have the same name")


def _dig(cfg: dict, path: tuple):
    cur = cfg
    for p in path:
        cur = (cur or {}).get(p)
    return cur


def _fallback(block) -> dict:
    """permission.json llm.fallback: when Claude has no credit / the key expired, its calls go to these Gemini models.
    second_opinion: the other Gemini model, used when both models of a pair (generator_1 + generator_2,
    verifier_1 + verifier_2) would otherwise be the same."""
    if not isinstance(block, dict) or "enabled" not in block:
        raise SettingsError('permission.json llm.fallback is missing: {"enabled": true, "CLAUDE": {"<claude model>": '
                            '"<gemini model>"}, "second_opinion": {"<gemini model>": "<other gemini model>"}}')
    out = {"enabled": bool(block["enabled"]), "CLAUDE": {}, "second_opinion": {}}
    for key in ("CLAUDE", "second_opinion"):
        part = block.get(key)
        if out["enabled"] and (not isinstance(part, dict) or not part):
            raise SettingsError(f"permission.json llm.fallback.{key} must list the models")
        out[key] = {str(k).strip(): str(v).strip() for k, v in (part or {}).items() if not str(k).startswith("_")}
    return out


def build_paths(cfg: Optional[dict] = None) -> dict:
    """Absolute paths used by every step (folders, credentials, prompts)."""
    cfg = cfg or load()
    p, f = cfg["paths"], cfg["files"]
    out = {k: resolve(v) for k, v in p.items()}
    cred = out["credentials_dir"]
    out.update({"env_file": cred / f["env_file"], "gemini_credentials": cred / f["gemini_credentials"],
                "claude_credentials": cred / f["claude_credentials"]})
    for k, v in f.items():
        if k.endswith("_prompt"):
            out[k] = out["prompt_dir"] / v
    out["output_dir"].mkdir(parents=True, exist_ok=True)
    return out


def selected_documents(cfg: Optional[dict] = None) -> List[dict]:
    cfg = cfg or load()
    order = {"ACT": 0, "RULES": 1, "NOTIFICATION": 2, "CIRCULAR": 3}
    docs = [dict(d, document_key=code) for code, d in cfg["documents"].items() if d.get("read")]
    return sorted(docs, key=lambda d: order.get(d["type"], 9))   # Acts, Rules, notifications, circulars
