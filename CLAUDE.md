# CLAUDE.md — DARP LAW ARC (GST law → knowledge points → Decision Graph Engine)

Read `README.md` and `docs/HOW_IT_WORKS.md` first. They describe the real flow, tables and prompts of this repo.
`docs/GOVERNANCE_MASTER.md` describes the governance master.

```
PDF ─► IKP ─► worker pool [ MKP ─► AKP (+ fact network) ─► GKP ] ─► GOVERNANCE MASTER ─► FACT CATALOG ─► engine
```

## Working rules (every session)

1. **Read before you write:** this file, the docs, `main.py`, the step engine and its `<step>_ss/` folder, the prompts in `Prompt/`, `permission.json`, `Config/settings.json` and `Config/gst_taxonomy.json`. Report the gaps and wait for approval before any schema change.
2. **Existing scripts and prompts are the baseline.** Change only what is required, or what fixes a real defect. No unwanted tables, columns or features. Ask first if you think something extra is needed.
3. **Layout:** one `<step>_engine.py` plus a `<step>_ss/` folder with `tests/` per step (ikp, mkp, akp, gkp, governance_master). Names use IKP / MKP / AKP / GKP / governance master, never "pass".
4. **`main.py` changes only with approval.** Show the new or changed function and where it goes.
5. **Every table name comes from `Config/settings.json`** (tables after MKP end with `_new`, 2026-11-19); the code never builds a table name. **No hidden defaults.** Day-to-day switches and the LLM + model per job are in `permission.json`; fixed paths, tables, prompts and thresholds are in `Config/settings.json` (both read by `kp_settings.py`). A missing key stops the run with its name.
6. **Prompts keep their flow:** the same header, row types and step order. Changing a prompt changes the raw LLM-answer cache keys of that step. Batch prompt edits, and log them at the top of the prompt file.
7. **Every model answer passes a Python gate before it is saved.** Write tests for parsers, gates, versioning and resume. Tests that need PostgreSQL must `pytest.skip` when the DB is down. Prefer tests that run without the DB.
8. **Versioning everywhere:** the scripts insert only, never `DELETE` or overwrite content. A new version only when the content is different; then version n+1 and `is_latest=false` on older rows. Readers always filter `is_latest`. Only the user deletes (pgAdmin or `kp_delete.py`, `access.database_delete`); the triggers in `db_migrate.py` then remove everything made from that MKP / IKP node.
9. **Database first:** MKP, AKP and GKP reuse what is saved when their input did not change, and log why the LLM was called when it did.
10. **Never invent** legal text, IDs, citations, facts, or the engine's rulebook format. When something is unknown, stop and ask.
11. **Never put secrets in code or tests.** Credentials live only in `Credentials/` (`.env`, `service_crendential.json`, `claude_crendential.json`).
12. **Worker pool safe.** MKP / AKP / GKP run in several threads (`kp_pool.py`): one database connection per worker, no module-level mutable state without a lock, Excel and coverage written once at the end, rate limits re-raised (`llm_runtime_errors.is_rate_limited`) so the pool can requeue the unit.
13. **One access check.** Folders, internet and deletes go through `kp_settings.allowed()` / `require()` (permission.json `access`). Console lines: `flow()` for the run's flow, `say()` / `log()` for engine detail.

## Prompts actually used

| Step | Prompt files (in `Prompt/`) |
|---|---|
| IKP | `law_arc_ikp_map_prompt.txt` (LLM assist, only for chapters that fail the gate) |
| MKP | `law_arc_mkp_prompt.txt`, `law_arc_mkp_judge_prompt.txt` |
| AKP | `law_arc_akp_prompt.txt`, `law_arc_akp_judge_prompt.txt`, `law_arc_akp_comparison_prompt.txt`, `law_arc_akp_same_prompt.txt`, `law_arc_akp_fact_prompt.txt` (fact check: is a new fact really new?), `law_arc_akp_time_prompt.txt` (time step: old / new law of an amended provision), `law_arc_akp_same_pair_prompt.txt` (SAME check of saved atom pairs: after every run, `--same-review`), `law_arc_akp_concept_prompt.txt` (one more try for a weak fact concept) |
| References | `law_arc_ref_part_prompt.txt` (which part of an outside document a law needs; only when Python finds none) |
| GKP | `law_arc_gkp_prompt.txt` (VERIFY, JUDGE, CREATE) |

## IDs and words that must not be mixed up

- **`G_`** is a logic group inside one MKP (AKP).
- **`TG01`… / `TSG01_01`…** are taxonomy groups (TG) and sub-groups (TSG): 15 TG / 81 TSG in `Config/gst_taxonomy.json` plus the ones GKP created (`created_by: GKP`); columns `tg_id` and `tsg_id`. MKP rows (`groups`, `mkp.group_names`) and AKP atoms (`groups` field, saved in `akp.group_names`) carry TSG ids too.
- **Reference IDs** (`ref_ss/ids.py`) are made by Python, never by the LLM: `N_CTR_04_2018`, `CIR_47_2018`, `ACT_31_2018`, or a document code (`CGSTR17`).
- **Abbreviated ids (`kp_ids.py`, made by Python only):** MKP `M_<acronym of the concept name>` (unique across all laws), every AKP element `<MKP id>_<TYPE>_<acronym>` (TYPE A, G, LE, PD, OR, CT, TC, LK; `akp.akp_id`, there is no element column), facts `F_<acronym of the fact name>` with no prefix; an LK row is named by its relation (`M_IOSFC_LK_AMENDS`). The models keep their own working ids; `akp_ss/ids.py` rewrites them just before saving.
- **SAME atom (`A_REF`)**: an atom with the same meaning as a saved atom of another law is saved as an `A_REF` row whose `akp_id` IS the saved atom's id (`M_LTPTOG_A_SIG`); the new law's groups / LE use it and the governance master writes it once. There is no `same_akp_id` column (2026-11-19).
- **Facts (`F_...`)** are raw inputs, never conclusions. One fact keeps one id across all laws (a registered fact keeps its id; the same name or a saved alias reuses it). `akp_atom_fact` is the fact network.
- **Two kinds of version:**
  - **Run version** (`version_no`, `mkp_version`, `akp_version`, `gkp_version`) is our own output.
  - **Law version** (`akp_time_fact.effective_from`, AMENDMENT MKPs) is when the law itself changed.

## Governance master

See `docs/GOVERNANCE_MASTER.md`.
- One governance master per TSG, in `Governance_Master/<TG>/` as `<TSG>_rulebook.yaml`, with its report in `Governance_Master/<TG>/report/<TSG>_report.xlsx` (outside `Output/`, no sub-folder per TSG; the engine CLI takes the file path). Call it a "governance master", never "rulebook". Every predicate carries `handler: python | light | heavy` (from the AKP atom).
- It uses no LLM and no engine. The governance master is checked in Python (`governance_master_ss/pack.py`).
- The main table is `governance_master`, with one row per element; history goes to `governance_master_history`.
- It runs once when the worker pool is done, with no user input, for every TSG that got laws in the run (spine-only TSGs too).
- Mapping decisions (fact aliases, tiers, spine coverage) live in `Config/governance_master_map.json`, which people review. Never guess a mapping in code.
