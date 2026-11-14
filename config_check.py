"""Start-up check: stops the run before any step when something it needs is missing, naming every problem.

permission.json is read by kp_settings.load() (it already checks run / use_cache / read / llm roles); this checks the
files the switched-on steps need: prompts, taxonomy, spine, mapping, credentials folder and the selected PDFs.
"""
from __future__ import annotations

from typing import List

from kp_settings import allowed, build_paths, resolve, selected_documents

STEP_PROMPTS = {"ikp": ["ikp_prompt"], "mkp": ["mkp_prompt", "mkp_judge_prompt"],
                "akp": ["akp_prompt", "akp_judge_prompt", "akp_comparison_prompt", "akp_same_prompt"],
                "gkp": ["gkp_prompt"], "governance_master": []}


class ConfigError(KeyError):
    pass


def check_permission(cfg: dict) -> List[str]:
    """Return [] when everything needed exists; otherwise raise ConfigError naming each problem."""
    problems: List[str] = []
    paths = build_paths(cfg)
    if not paths["prompt_dir"].is_dir():
        problems.append(f"prompt folder not found: {paths['prompt_dir']}")
    if not paths["credentials_dir"].is_dir():
        problems.append(f"credentials folder not found: {paths['credentials_dir']}")
    for step, keys in STEP_PROMPTS.items():
        if not cfg[step]["enabled"]:
            continue
        for k in keys:
            if not paths[k].is_file():
                problems.append(f"{step}: prompt file not found: {paths[k]}")
    if any(cfg[s]["enabled"] for s in ("akp", "gkp", "governance_master")):
        if not resolve(cfg["gkp"]["taxonomy_file"]).is_file():
            problems.append(f"taxonomy file not found: {cfg['gkp']['taxonomy_file']}")
    if cfg["governance_master"]["enabled"]:
        for k in ("map_file", "spine_rulebook"):
            if not resolve(cfg["governance_master"][k]).is_file():
                problems.append(f"governance master {k} not found: {cfg['governance_master'][k]}")
    # permission.json access: every folder this run reads / writes must be allowed there
    for label, path in (("law folder", paths["law_dir"]), ("prompt folder", paths["prompt_dir"]),
                        ("credentials folder", paths["credentials_dir"]),
                        ("taxonomy", resolve(cfg["gkp"]["taxonomy_file"]))):
        if not allowed(cfg, "READ", path):
            problems.append(f"permission.json access.read_folders does not allow the {label} ({path})")
    writes = [("output folder", paths["output_dir"])]
    if cfg["governance_master"]["enabled"]:
        writes.append(("governance master folder", resolve(cfg["governance_master"]["output_dir"])))
    if cfg["features"]["gkp_create_groups"] and cfg["gkp"]["enabled"]:
        writes.append(("taxonomy file (GKP creates groups there)", resolve(cfg["gkp"]["taxonomy_file"])))
    for label, path in writes:
        if not allowed(cfg, "WRITE", path):
            problems.append(f"permission.json access.write_folders does not allow the {label} ({path})")
    docs = selected_documents(cfg)
    if any(cfg[s]["enabled"] for s in ("ikp", "mkp")) and not docs:
        folders = ", ".join(f"Law/{v['folder']}" for v in cfg["law_folders"].values())
        problems.append(f"no PDF selected: put PDFs in {folders} and set that kind to true under \"read\" in permission.json")
    can_download = cfg["features"]["download_missing_pdfs"] and cfg["access"]["internet"]
    for d in docs:
        if (paths["law_dir"] / d["file"]).is_file():
            continue
        if d.get("url") and can_download:
            if not allowed(cfg, "WRITE", paths["law_dir"]):
                problems.append(f"{d['code']}: PDF to download, but access.write_folders does not allow Law")
            continue
        problems.append(f"PDF missing: {paths['law_dir'] / d['file']}"
                        + (" (it has a url: switch on features.download_missing_pdfs and access.internet)"
                           if d.get("url") else ""))
    if problems:
        raise ConfigError("LAW ARC cannot start:\n  - " + "\n  - ".join(problems))
    return []
