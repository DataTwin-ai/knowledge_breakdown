# How LAW ARC works (IKP → worker pool [MKP → AKP → GKP] → governance master → fact catalog)

## 1. The run: `python main.py`

**Two settings files:**
- `permission.json` — what you change day to day, read only through `kp_settings.py`:

| Block | What it sets |
|---|---|
| `run`, `use_cache` | Which steps run (also `run.fact_catalog`), cache on / off per step |
| `read`, `documents` | Which kinds of law are read (acts / rules / notifications / circulars); `documents` switches one document on / off by code |
| `scope` | Ask the scope (`ask_user_input`) or use `default` (`"FULL"`, `["section 7", "rule 55(1)"]`) |
| `workers` | `count` (worker pool size, start with 4) and `max_calls_at_once` per provider (all workers together) |
| `features` | `download_missing_pdfs`, `cross_ref_record`, `web_scraping`, `push_amending_notifications`, `gkp_create_groups`, `prompt_caching` |
| `access` | Folders the scripts may read / write, `internet`, `database_delete`. A folder not listed is never touched; the run stops naming the key |
| `console` | `FLOW` (only the flow: workers, units, retries, errors, summary) or `DETAIL` (every engine line) |
| `governance_master`, `llm` | Which TSGs get a governance master; the LLM + model of every job (`llm.ref_link` = the model that finds official links for web scraping) |

- `Config/settings.json` — fixed settings: folders, prompt files, table names, limits, word lists, `pool`
  (`max_job_chars`, `requeue_limit`), `refs` (tables, official hosts), `fact_catalog`.
- **Table names (2026-11-19):** every table is named in `Config/settings.json`; no name is made in the code. Every
  table after MKP ends with `_new` (`akp.table` `akp_new`, `akp.cache_table`, `akp.llm_cache_table`, `akp.fact_table`,
  `akp.atom_fact_table`, `akp.time_fact_table`, `gkp.table` `gkp_new`, `gkp.cache_table`, `gkp.llm_cache_table`,
  `gkp.coverage_table`, `gkp.taxonomy_table`, `governance_master.table` / `history_table` / `tables`,
  `relationship.table`, `fact_catalog.table` / `index_table`). `kp_settings.check_tables` stops the run when a name is
  missing or one table has two names. IKP, MKP, `mkp_ref`, `ref_document` keep their tables; MKP reads
  `mkp.taxonomy_table` (`gkp_taxonomy`) because the taxonomy is part of its input hash. To rename: settings only.

**Law folder.** Put PDFs in `Law/Acts`, `Law/Rules`, `Law/Notifications`, `Law/Circulars`. The name, code, type and
fixed reference ID of each PDF are kept in `Config/documents.json`. An entry there with a `url` and no PDF is
downloaded at the start (`features.download_missing_pdfs` + `access.internet`).

**Order of work:**
1. **Check.** `config_check.check_permission()` stops the run before any step if a prompt, folder, taxonomy, spine,
   PDF or access right is missing, listing every problem. The first database connection also upgrades old table names
   and installs the delete triggers (`db_migrate.py`).
2. **Downloads** of listed PDFs that are missing.
3. **IKP** for every selected PDF (the whole PDF, whatever its kind).
4. **Plan.** The scope of every document is asked first (all questions before the work starts), then cut into jobs
   (`mkp_ss/scope.jobs_for`, section 8).
5. **Worker pool** (`kp_pool.py`): `workers.count` workers run MKP → AKP → GKP per job.
6. When every job is done: `akp.xlsx`, the coverage check and `gkp.xlsx` once, the **governance master** of every TSG
   that got laws, the **fact catalog**, the reference status of the processed documents, and the **summary**.
7. If MKP is switched off, AKP / GKP / governance master run on the MKPs already saved (one after another).

### Rules every step follows

- **Database first.** MKP, AKP and GKP look in the database before calling an LLM:
  - MKP: the saved MKP text is compared with the law text Python reads from the PDF. Same → reused. Missing or
    different → LLM. The screen says why, e.g. `MKP LLM reason : CGSTA17_S7_SS1_Ca#TEXT - text changed` with the
    old (v1) and new (v2) text.
  - AKP: reused when it was made from the same MKP version. GKP: reused when it was made from the same AKP version.
- **New version only when different.** A re-run that gives the same content keeps the version number. A different
  result is saved as the next version; older rows stay with `is_latest=false`. The scripts never delete; you may
  (section 11).
- **Python checks every model answer** before it is saved (word checks, structure, facts, engine grammar).
- **Short screen, full log.** With `console.show = FLOW` the screen shows only the flow; everything else is in
  `Output/logs/law_arc_<date>.log`, each line tagged with its worker (`[W2]`).
- **One error log per document.** `Output/<DOC>/error_log.csv` (time, document, unit, stage, worker, error, retries,
  status). A failing unit never stops the run; `python main.py --retry-failed` runs only the units whose last row is
  FAILED (a failed row for the WHOLE document - IKP / MKP_LOAD could not reach the database - is skipped: it is not
  a law, and running it again would run the whole document). When the database cannot be reached at all (network
  down), a worker waits up to `database.unreachable_wait_seconds` (trying every `unreachable_retry_seconds`) and the
  unit goes back in the queue, instead of the whole queue failing; the governance master builds once more after a
  dropped connection.

---

## 2. IKP: the book's structure (`ikp_engine.py`, `ikp_ss/`)

**Input:** one PDF from `Law/`. **Output:** a verified tree of every legal unit.

| Step | Module | What happens |
|---|---|---|
| Read | `layout.py` | Text is read with PyMuPDF. Running headers and footers, contents pages, footnotes, margin notes and footnote markers are separated. `[[PAGE n]]` markers are kept. |
| Split | `structure.py` | Python splits the document: chapter → section / rule → sub-section → clause → sub-clause → proviso / explanation / closing text / table rows / annexures / orders. |
| Enrich | `enrich.py`, `citations.py` | Status (inserted / substituted / omitted) and amendment dates are taken from the footnotes. Cross-references such as "section 16(2)(c)" and "sections 73 to 76" are resolved, and definitions are collected. |
| Cluster | `cluster.py` | One cluster (C_ / CM_ rows) per top-level operative provision. |
| Gate | `gates.py` | A quality check per chapter: coverage, sequence, duplicate keys, empty provisions and unresolved markers. |
| LLM help | `llm_assist.py` | **Only** for a chapter that fails the gate (`llm.mode=ON_FAIL`). The model tags line starts, and Python re-checks them. The LLM version is kept only if it scores better. |
| Save | `build.py`, `store.py` | Each chapter is committed as a checkpoint (resume point). When the document gate passes, the version becomes COMPLETE and `is_latest`. |
| Read back | `reader.py` | `IkpIndex`: `get`, `resolve("section 7(1)(a)")`, `context`, `read_from_pdf`. This is the only way MKP gets law text. |

**Switches** (`permission.json` → `run.ikp` / `use_cache.ikp`; `Config/settings.json` → `ikp`):

| Setting | Effect |
|---|---|
| `run.ikp=false` | Use the latest saved IKP. |
| `use_cache.ikp=true` | Reuse the same PDF if already COMPLETE, or resume an unfinished run. |
| `use_cache.ikp=false` | Build a new version. |
| `llm_mode` | `OFF`, `ON_FAIL` or `ALWAYS`. |
| `llm_chapters` | Re-check only the listed chapters with the LLM. |
| `fail_on_gate` | A document that fails is saved but not made latest. |
| `dry_run_fetch` | Every operative node must return non-empty text. |

**Tables:**

| Table | Holds |
|---|---|
| `ikp_versions` | One row per document version: `version_no`, PDF SHA-256, law-as-on date, status, `is_latest` |
| `ikp_chapters` | Per-chapter checkpoint |
| `ikp` | DOC / TYPE / ELEMENT / PAGE_NUMBER / RESULTS rows |
| `ikp_nodes` | One row per node: text, PDF blocks, anchors, parent, children, amendments, cross-references |
| `ikp_llm_cache` | Raw LLM answers |

**Also written:** `Output/ikp/<CODE>.ikp.jsonl` and `Output/ikp.xlsx`.

---

## 3. MKP: molecules (`mkp_engine.py`, `mkp_ss/`)

**Input:** the latest IKP. The PDF hash must match the IKP, otherwise MKP stops and asks you to re-run IKP. **Output:** Standard MKP + SP rows in `mkp`.

1. **Scope** (`scope.py`):
   - If `ask_user_input=true`, a menu: 1 Full document, 2 Chapter → Section/Rule → Sub-section → Clause (0 = the whole of this level), 3 Type a citation, 4 Type a IKP key.
   - Otherwise `permission.json` → `scope.default` is used, e.g. `"FULL"` or `["section 7", "rule 55(1)"]`.
   - The scope is split into batches: one per legal unit, or one for the smaller selected piece.
2. **Reference text** (`draft.py`): Python writes the exact reference text of every expected MKP from the IKP, including lead-ins, closing lines and footnotes.
3. **Models by complexity** (`scope.complexity`): `SIMPLE` or `COMPLEX` tier, which picks the LLM + model of each job from `permission.json` → `llm.mkp`.
4. **Generate:** generator_1 and generator_2 run in parallel with `law_arc_mkp_prompt.txt`.
5. **Pre-gate report** (`check.pregate`): Python compares both answers with the reference text and tells the judge exactly where each one differs.
6. **Judge:** the judge model, using the standard judge prompt `law_arc_mkp_judge_prompt.txt`, builds the FINAL table.
7. **Standard gate** (`check.standard_gate`):
   - Every expected provision has an MKP.
   - **Source fidelity:** every word comes from the source (`fidelity_check=BLOCK`).
   - The SP phrases rejoin exactly to the MKP text.
   - Modal, negation and qualifier words (`Config/settings.json` → `word_lists`) are all kept. This is the anti-hallucination word check: a model may not drop "shall", "not" or "subject to".

   On failure, the judge gets one retry with the exact errors. If it still fails, the unit is **split into its children**, each child is run on its own, and the failure is logged.
8. **Groups.** The TSG list (`gkp_ss/taxonomy.compact_text`, about 2,200 tokens) is sent with the generator and
   judge prompts; every MKP row carries `groups: [TSG.., ..]`. Python keeps only real TSG ids
   (`check.clean_groups`); HEADING / AMENDMENT / METADATA rows get none. Saved in `mkp.group_names`.
9. **References** (Python, `ref_ss/`): for every MKP, SELF references (same document: from the IKP cross references
   of the provision, its lead-ins and closing line) and CROSS references (notifications, circulars, amending Acts,
   "section 15 of the Act", "rule 27 of the CGST Rules"), each with one fixed ID made by Python
   (`N_CTR_04_2018`, `CIR_47_2018`, `ACT_31_2018`, `CGSTR17`). Saved in `mkp.self_refs` / `mkp.cross_refs`,
   `mkp_ref` (one row per reference) and `ref_document` (one row per outside document, with its status).
   The title is the document's FULL name (from the footnote's `instrument` that IKP read, e.g. "The Central Goods
   and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)", or a notification with its date).
   A footnote "Inserted / Substituted by …" is AMENDING (relation AMENDED_BY) and PENDING like any outside document.
   **Link while splitting** (`ref_ss/link.py`, `refs.link_on_split`): a NEW outside document gets its official link
   once - the `ref_link` model (Claude Haiku) with Claude's web search on `refs.allowed_hosts`; Python checks its
   link, the PDF links among the search results, and opens an official page that lists the document to find its
   PDF; only when all that finds nothing, the stronger `ref_link_retry` model (Claude Sonnet) searches once more.
   `link_status` OK (status LINK_FOUND) / BAD (LINK_BAD) / NOT_FOUND (PENDING). `ref_engine.py` uses the same search.
   `ref_engine.py` downloads the OK ones and finds another link for the others.
   **Status of each law:** `mkp_ref.target_status` is INSERTED when the exact clause the reference points to has an
   approved AKP in the AKP table (`akp_new`; per clause, not per document - an MKP alone is not enough), updated at
   the end of every run. View `refs.parts_view` (`v_reference_parts_new`): each document with its inserted clauses. `ref_document.status`
   is about the document's file only.
10. **Save** (`store.commit`, one transaction per unit) with the MKP embedding. **Versioning:**
   - each MKP element gets `mkp_version` 1, 2, 3, …;
   - only an element whose text, type or phrases changed gets the next version; the earlier row gets `is_latest=false`;
   - nothing is deleted;
   - all readers use `is_latest`.

**Tables:**

| Table | Holds |
|---|---|
| `mkp` | `cluster`, `type`, `element`, `results`, `embedding`, `version_id` (PDF hash), `mkp_id`, `mkp_version`, `is_latest`, `run_id`, `inserted_at`, `source_version` (IKP version), `qc_status`, `doc_type`, `instrument_code` |
| `mkp_cache` | generator_1 / generator_2 / Standard rows per unit and input hash |
| `mkp_llm_cache` | Raw model answers (`unit_key` = the unit they belong to) |
| `mkp_ref` | MKP → referenced document / provision (`scope` SELF / CROSS, `relation`, `mention`, `target_status` INSERTED / PENDING of the exact law) |
| `ref_document` | One row per outside document: `ref_id`, kind, number, year, full title, link, `link_status` (OK / BAD / NOT_FOUND), `status` (PENDING, LINK_FOUND, LINK_BAD, VERIFIED, INSERTED, NOT_FOUND) of the file |

New `mkp` columns: `group_names`, `self_refs`, `cross_refs` (JSON lists).

**Also written:**
- `Output/mkp.xlsx`
- `Output/mkp_completed_provisions.txt`
- `Output/mkp_failed_provisions.txt` and `Output/mkp_failed_details.txt`

---

## 4. AKP: atoms and facts (`akp_engine.py`, `akp_ss/`)

**Input:** the latest approved Standard MKPs of the scope, with children before parents. **Output:** the element table per MKP, plus the fact network.

1. **Python-written MKPs (no LLM):**
   - HEADING and METADATA MKPs get MKP + SP + SUM rows.
   - AMENDMENT MKPs also become a **time fact** (`akp_ss/facts.time_fact`): kind (INSERTED / SUBSTITUTED / OMITTED), the instrument that made the change, the instrument date, the effective-from date and the replaced text. It is saved in `akp_time_fact` with an LK `AMENDS` link to the provision it changes.
2. **Operative MKPs:**
   1. generator_1 and generator_2 run with `law_arc_akp_prompt.txt`. The input carries the MKP, its SP phrases, the cluster, the MKP's groups (`mkp_groups`), the taxonomy and a **fact registry slice**: the facts already used in this section, then the nearest by meaning, then the most used. Every A atom carries `groups: [..]` (kept from the MKP, or another TSG when the atom is about something else).
   2. The judge (`law_arc_akp_judge_prompt.txt`), returns one Standard table: `MKP|TYPE|ELEMENT|CONCEPT|RESULTS`, with rows MKP, LE, OR, G, PD, A, CT, FACTS, LK, SP, LW, TC and SUM.
   3. **Python checks** (`guard.py`):
      - `legal_words`: every negation stays next to the word it negates, so "without consideration" turned into "for consideration" is caught as **NEGATION FLIPPED**. It also checks that qualifiers (notwithstanding, subject to, only, within …) are kept, that shall / may are not changed, and that no number is lost or invented.
      - `structure`: IDs, LE and G logic (LEs use A_ / G_ / PD_ only, never F_), groups of two or more members, no orphan atoms, full-sentence statements, **every operative atom names its fact(s)**, every fact declared, and SP coverage.
      - `registry.check`: a fact type must match the type already registered.
      - `refs`: an atom may not cite a provision the source does not cite.
      - `engine_ready`: `order` is LOW or HIGH; a LOW rule uses the engine grammar, only declared facts and only their options; a HIGH atom has its question and facts.

      Structure / fact / engine errors go back to the judge (`judge_repairs`); if they remain, the attempt is FALSE
      (no model can excuse them). Word / number / reference errors are *findings*: they go to the comparison.
   4. **Comparison** (`law_arc_akp_comparison_prompt.txt`), run by the `comparison` model whether or not Python had findings: rebuild the MKP from the atoms and check it means the same as the source. It also answers every Python finding REAL or PYTHON_WRONG (e.g. "fifty lakh" = 5000000); TRUE stands only when none is REAL. If FALSE, the next attempt (`max_attempts`, 3) gets "ERRORS FROM LAST TIME" and its rejected table (AKP prompt section RETRY).
   5. **When TRUE:**
      - Python adds the reference links.
      - The atoms are embedded (concept + family, and statement).
      - The **SAME check** (bug list A, 2026-11-18): every new atom gets up to 5 saved atoms of OTHER laws by RAG (statement similarity > 0.90, or 0.80-0.90 with the same facts and rule, or concept >= 0.92 with statement >= 0.70). An exact copy (same statement, facts and rule) is SAME by Python; the rest go to the SAME model (`law_arc_akp_same_prompt.txt`, SAME check only), which names the saved atom by its `akp_id`. SAME = the same condition on the same subject testing the same input, even when the two laws' outcomes differ. A SAME atom is saved as an `A_REF` row whose `akp_id` (and `same_akp_id`) is the **saved atom's id** (e.g. `M_LTPTOG_A_SIG`): this law's groups / LE use that id, the new id is never made, the row keeps this law's wording only as a record, its fact links stay. Every SAME answer that is not applied is logged (`SAME NOT APPLIED : ... - reason`).
      - **Old AKP cache (2026-11-19, `akp.reuse_old_cache_table`, '' = off, only with use_cache true):** an MKP with no
        AKP in the new tables but an approved answer for the same MKP input in the old cache table (`akp_cache`) uses
        those atoms - no generator / judge / comparison call; an old A_REF becomes a plain atom again, then only the
        fact check, the SAME check and the check at save run, and the result is saved in `akp_new` + `akp_cache_new`
        (log `REUSED old AKP cache`). Nothing is copied straight from the old cache. No old answer -> AKP from the start.
        Old fact ids are NOT kept (they belong to the old fact table: old F_PRS "Person Relation Status" is another fact
        than new F_PRS "Person Registration Status"): every old fact gets a temporary id and is matched again by name
        and meaning; a fact the old atom read without declaring it comes from `akp.reuse_old_fact_table` (`akp_fact`).
        Log `OLD FACTS`, `OLD FACT MISSING`.
      - **Same atom = same facts (2026-11-19):** an `A_REF` tests the SAVED atom's facts and rule (log `SAME FACTS`);
        a fact only that A_REF used is not registered (log `FACT DROPPED`). One atom id never points to two facts.
      - **Fact shape (2026-11-19):** a fact is an attribute with values, not a yes/no fact per value. Python
        (`guard.flag_facts`, `FACT SHAPE`) sends back alternatives of one choice (ANY group / OR in an LE) that each
        test their own new BOOL fact of the same entity: make ONE CODE fact with options.
      - **No `same_akp_id` column (2026-11-19):** an `A_REF` row's `akp_id` is the saved atom's id; readers take the
        reused id from `akp_id` of `A_REF` rows.
      - **At save** (bug list A5, like the facts): inside the save lock the worker checks again the atoms other workers saved after its SAME check (laws running at the same time) - same RAG + Python + SAME model, only those atoms; log `SAME AT SAVE`.
      - **After the workers** (`main.py`, `akp.same_sweep_after_run`): the atoms saved in the run are checked again against every saved atom (laws that ran at the same time never saw each other) - same rule, pairs prompt `law_arc_akp_same_pair_prompt.txt`; a SAME pair gives the later law a new AKP version using the first-saved id, then GKP runs again for that law. For atoms saved before the fix: `python akp_engine.py --same-review` writes `Output/same_review.xlsx` (one row per close pair, the AI's answer, an APPROVE column - nothing changes), a person marks YES, then `python akp_engine.py --same-apply` maps those rows.
      - The **governance master** writes a SAME atom ONCE, under the saved id, and every law that uses it points to it.
      - **Same facts (bug list B):** facts with no name / concept embedding get them at the start of AKP (an embedding
        that fails is logged); a NEW fact whose concept is missing, longer than 3 words or only its name gets one
        more try (`law_arc_akp_concept_prompt.txt`); after the workers, the facts first registered by the run's laws
        are checked against every registered fact (`akp.fact_sweep_after_run`, the fact-check model) and a SAME pair
        is merged into the first-registered fact (like `--fact-merge`). The second check at save time stays.
      - Python keeps only real TSG ids in each atom's `groups` field (`clean_atom_groups`).
3. **Save** (one transaction):
   - `akp`: next `akp_version`; older rows get `is_latest=false` and are marked SUPERSEDED.
   - `akp_fact`: new facts. For a known fact, new options and evidence are merged in.
   - `akp_atom_fact`: atom → fact edges, carrying the atom's rule.

**The fact network starts here.**
- One fact keeps **one id everywhere**, e.g. `F_SUPPLY_CONSIDERATION` (PAID / AGREED / NONE).
- Atoms of different provisions that test the same fact are linked through it.
- `derived_from` records computed facts, such as age from date of birth.
- `evidence` lists the documents that prove a fact.
- `possible_duplicate_of` flags a new fact whose meaning is ≥ 0.93 similar to an existing one.

**Tables:**

| Table | Holds |
|---|---|
| `akp` | `instrument`, `version_id`, `source_mkp`, `mkp_id`, `akp_id` (the short id of every row, no element column), `ordinal`, `type`, `concept`, `results`, `concept_embedding`, `atom_embedding`, `validation_status`, `akp_version`, `is_latest`, `inserted_at`, `mkp_version`, `tier`, `same_akp_id`, `group_names` (the atom's TSG ids, like `mkp.group_names`; the confirmed TG / TSG is in `gkp`) |
| `akp_cache` | Cached results per MKP and input hash |
| `akp_llm_cache` | Raw model answers |
| `akp_fact` | One row per canonical fact: name, entity, type, options, nl, `aliases`, `source_kind`, `derivation`, `concept` (2-3 words) with `concept_embedding`, `embedding` (name + nl) |
| `akp_atom_fact` | Atom → fact edges |
| `akp_time_fact` | Amendments as time facts |

**Also written:** `Output/akp.xlsx` and `Output/akp_failed_mkps.txt`.

---

## 5. GKP: grouping into TSGs (`gkp_engine.py`, `gkp_ss/`)

**Input:** the latest approved AKP rows of the scope, plus `Config/gst_taxonomy.json` (15 TG / 81 TSG, each with
definition, include, exclude ("not here → goes to"), key provisions and anchors; and the groups GKP created).
**Output:** one row per (atom, sub-group) membership.

1. **Groups first, no LLM:**
   - an atom with valid TSG ids in its `groups` field (given by the MKP and AKP judges) → APPROVED, method `AKP_GROUP`;
   - notification / circular: an atom without its own groups takes its MKP's groups (`MKP_GROUP`), else the groups of
     the document as a whole - its other MKPs, or the Act / Rules MKPs that cite it (`DOCUMENT_GROUP`);
   - `A_REF` atoms inherit the groups of the atom they are SAME as;
   - AMENDMENT MKPs go to `TSG14_02` (Amendments & effective dates) plus the groups of the provision they amend;
   - HEADING MKPs are skipped.
2. **The rest** (no groups yet, or older AKP rows): candidates (`candidates.py`: concept / atom embeddings, include
   terms, the most specific anchor, TSGs of atoms sharing a fact, referenced provisions, section neighbours, similar
   atoms of other documents) → verifier_1 + verifier_2 in parallel → judge only when they disagree → wider search.
3. **GKP CREATE** (`features.gkp_create_groups`), only for atoms still without a group: the proposer
   (`law_arc_gkp_prompt.txt`, MODE CREATE) places each atom in an existing TSG after all, or describes a new narrow
   sub-group under an existing TG, or (rarely) a new TG. Python checks the answer (`verify.parse_create`: no
   "Other / Miscellaneous", evidence from the atom's own text), reuses a sub-group with the same name or the same
   meaning (embedding ≥ `gkp.create_reuse_similarity`, 0.85) instead of making a near copy, and writes a new one to the
   taxonomy file with `created_by: GKP` (one worker at a time). Nothing is left UNCLASSIFIED.
4. **Save:** the next `gkp_version` per MKP, with `is_latest`, `inserted_at`, TG and TSG ids and names, `status`,
   `method` (AKP_GROUP, MKP_GROUP, DOCUMENT_GROUP, AGREE, JUDGE, SAME_INHERITED, AMENDMENT_*, GKP_CREATED,
   CREATE_EXISTING, CREATE_REUSED), `reason` and scores. Grouped with another taxonomy → grouped again.
5. **Coverage check** (once at the end of the run): an anchor provision whose atoms did not land in the TSG that names
   it is reported as a GAP.

**Tables:** `gkp` (memberships), `gkp_taxonomy` (TSG embeddings), `gkp_coverage`, `gkp_cache`, `gkp_llm_cache`.
**Also written:** `Output/gkp.xlsx` (sheet "Groups Created by GKP" lists every created sub-group).

---

## 6. Governance master: one per TSG (`governance_master_engine.py`, `governance_master_ss/`)

A governance master holds everything the Decision Graph Engine needs for one TSG: its laws, the laws they point to
(schedules, rules, definitions), and the spine ("is it a supply?", "goods or services?").

- It runs once when the worker pool is done, for every TSG the atoms of the run were grouped in. Every TSG with laws
  gets one, even when its laws are all in the spine. The yaml keeps the engine's format; the fact flow is the fact
  catalog next to it (section 10).
- It is checked in Python (ids, rules, steps, LOW/HIGH order). No LLM, no engine.
- Same content → the file is not rewritten. Different → the file and the main-table rows are replaced and the change
  is recorded in history.

| Output | What it is |
|---|---|
| `Governance_Master/<TG>/<TSG>_rulebook.yaml` | The governance master; the engine reads this file (+ `<TG>/report/<TSG>_report.xlsx`) |
| `<TG>/report/<TSG>_report.xlsx` | The report of each governance master, in its TG's report folder |
| `Governance_Master/index.xlsx` | Every governance master |
| `governance_master` | **Main table**: one row per element, filter by `tsg_id` |
| `governance_master_history` | One row per change |

Full details are in `docs/GOVERNANCE_MASTER.md`.

## 7. What one molecule contains (manager view)

There are two PostgreSQL **views**: always current, no extra tables, no storage. The governance master step refreshes them on every run. To refresh them at any time:

```
python governance_master_engine.py --views
```

**`v_molecule_elements`**: everything inside a molecule, one row per element (the manager's `M1 | A | A1` table).

| molecule | mkp_id | element_type | element_id | concept |
|---|---|---|---|---|
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | LE | LE_CIPFS | |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | G | G_PAY_FORM | |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | A | A_PAY_NONMONEY | Non Money Payment |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | F | F_PAYMENT_FORM | Form of payment |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | TSG | TSG04_04 | Non-money consideration (barter, exchange) |
| CGSTA17_S2_C31_SCa#TEXT | | GOVERNANCE_MASTER | TSG04_04 | Non-money consideration (barter, exchange) |

The element types are:
- MKP, LE, OR, G, PD, A, A_REF, CT, LK and TC, from AKP;
- F: the facts its atoms read;
- TSG: the TSGs from GKP;
- GOVERNANCE_MASTER: the governance masters that use it.

There are also `detail`, `version` and `inserted_at` columns.

**`v_element_links`**: how the elements connect inside the molecule, one row per link (parent → child):

| relation | parent → child | example |
|---|---|---|
| HAS | MKP → LE / OR / CT / TC | the molecule has law expression LE_CIPFS |
| USES | LE → A / G / PD in its formula | LE_CIPFS uses G_PAY_FORM |
| MEMBER | G → its members | G_PAY_FORM → A_PAY_NONMONEY |
| FEEDS | LE → the product it issues | LE_CIPFS → PD_CONSIDERATION_INCLUDED |
| READS | A → F | A_PAY_NONMONEY reads F_PAYMENT_FORM |
| GROUPED_IN | A → TSG | A_PAY_NONMONEY is in TSG04_04 |
| IN_GOVERNANCE_MASTER | MKP → governance master | used by governance master TSG04_04 |

Example queries:

```sql
SELECT element_type, element_id, concept FROM v_molecule_elements
WHERE molecule = 'CGSTA17_S2_C31_SCa#TEXT' ORDER BY element_type, element_id;

SELECT parent_id, relation, child_id FROM v_element_links WHERE molecule = 'CGSTA17_S2_C31_SCa#TEXT';
```

The governance master Excel report also has a **Molecules** sheet with the same table for every law in the governance master, including each element's id inside the governance master.


---

## 8. Worker pool (`kp_pool.py`, `mkp_ss/scope.jobs_for`)

**Jobs.** One job per section / rule / schedule paragraph / circular paragraph of the chosen scope, in law order;
documents in the order Acts → Rules → notifications → circulars. A unit with more than `pool.max_job_chars`
characters of law text (24,000, about 6,000 tokens) is split at its sub-sections, then clauses - never inside a clause;
its own heading and lead-in become one small extra job ("heading"), so every MKP of the unit is framed exactly once.
Choosing one clause gives one job and one worker; the whole PDF gives every unit.

**Workers.** `workers.count` threads (W1, W2 …), each with its own database connection. A free worker takes the next
job - whichever worker finishes first. A job = MKP for the unit → AKP for its MKPs → GKP for its atoms.

**When something goes wrong:**

| What | What happens |
|---|---|
| MKP gate fails | The unit is split into its parts; the parts go to the **front** of the queue (row `SPLIT` in the error log) |
| "Too many requests" | `llm_runtime.py` waits longer each time (up to a minute, `MAX_RATE_LIMIT_RETRIES`); still limited → the unit goes to the **end** of the queue (`pool.requeue_limit` times). Answers already received are cached, so nothing is paid twice |
| Database connection lost | Every connection sends TCP keepalive signals (`Config/settings.json` `database`) so a worker waiting minutes for a model is not dropped as idle; MKP ends its open read before AKP starts. If a connection is still lost (`server closed the connection`, `connection already closed`), the unit goes back in the queue (`pool.requeue_limit` times) and the next try opens a new connection (`kp_pool.py`, `db_insert.is_alive`) |
| Any other error | One row in `Output/<DOC>/error_log.csv`; the worker takes the next job |
| AKP comparison FALSE | The other MKPs of the unit are saved; the unit is logged (stage AKP) |

**Speed.** LLM calls of all workers together are limited per provider (`workers.max_calls_at_once`). The fixed part of
every MKP / AKP / GKP prompt (instructions + taxonomy) comes first and is cached by Claude (`features.prompt_caching`);
Gemini sees the same beginning and caches it by itself. Fact saving and taxonomy changes are one worker at a time.

**Screen (FLOW):**

```
PLAN      : CGST Act (CGSTA17) - scope Full document - 320 job(s) (1 big unit(s) split into parts)
WORKERS   : 4 started - 320 job(s) in the queue
[W1] start  CGSTA17 CGSTA17_S7 (heading)   Section 7 - Scope of supply - heading and lead-in
[W2] start  CGSTA17 CGSTA17_S7_SS1         Sub Section (1) ...
[W2] retry  CGSTA17 CGSTA17_S7_SS1 - too many requests, back in the queue (1/3)
[W1] done   CGSTA17 CGSTA17_S7 (heading)   atoms 0  6s
[W3] FAILED CGSTA17 CGSTA17_S7_SS3 at MKP - ... (error log; next job goes on)
PROGRESS  : 41/320 jobs | 4 working | 1 failed | about 34 min left
```

## 9. Cross references and web scraping (`ref_ss/`, `ref_engine.py`)

MKP records the references (section 3). When Section 7 is validated, `RefStore.for_mkp(version, element)` gives its
references with the status of each document; an INSERTED document's MKPs / AKPs are in the database under its code.

`python ref_engine.py` (only with `features.web_scraping = true` and `access.internet = true`; `--limit N`, `--run`):
for every PENDING notification / circular - the saved link, or the official link from the LLM (`llm.ref_link`) →
download (HTTP 200, a real PDF, an official host from `refs.allowed_hosts`) → check that its number and year are in
the first pages → `Law/Notifications/<ID>.pdf` + `Config/documents.json` (code = its fixed ID) → VERIFIED. A wrong
file is refused (LINK_BAD) and the LLM is asked once more; never found → NOT_FOUND (add it by hand). With `--run` the
new documents go through `main.py --documents ...` and become INSERTED.

**Automatic after the workers (bug list C, 2026-11-18)** - `main.py._references`, helpers in `ref_ss/auto.py`,
before the SAME checks and the governance master; the manual `ref_engine.py` stays:
- **SELF** (same PDF, e.g. 7(1)(a) -> Schedule III): the direct targets of the run's laws with no MKP yet run like a
  user scope, MKP -> AKP -> GKP (`refs.self_depth` 1 = only what these laws name, at most `refs.self_limit` per run).
  The target keeps its own TSG; the citing law's governance master pulls its checks in by the same ids.
- **CROSS** (outside document; `refs.auto_fetch`, `features.web_scraping`, `access.internet`): at most
  `refs.auto_fetch_limit` documents are downloaded and checked (`ref_engine.run(ref_ids=...)`; a document already
  VERIFIED / INSERTED is reused), IKP'd, and the EXACT part each citing law needs is found - Python first (an amending
  text names the provision it changes: "In section 2 ... in clause (17), for sub-clause (h) ..."), the
  `law_arc_ref_part_prompt.txt` model only when Python finds nothing (keys of that document only); a small document
  cited as a whole (at most `refs.whole_document_max_parts` parts) runs whole. The part is saved in
  `mkp_ref.source_key` and runs MKP -> AKP with **no GKP** (no TSG of its own). A part already inserted (approved
  AKP) is **not run again**: the citing law only gets its link. A reference run never uses `scope.default`.
- **Which atom (2026-11-19):** at AKP save Python writes `outside_refs: [ref ids]` on the exact atom that names the
  document (its number "14/2017" in the atom's words; with no number, the MKP's only outside reference goes to the
  atom that says notified / circular). Log `REFERENCE : <atom> holds <ref>`, or `REFERENCE NOT ON ATOM`.
- **Governance master:** it follows `mkp_ref.source_key` like a self reference: the part comes in **full**
  (included_as `REFERENCE <ref id> <part>`) and the marked atom is answered by the part's steps (`_wire_reference`).
  The same part used by law a and law b is the same ids in both yaml files. Design: project note `reference_design.md`.
- **Status** (`mkp_ref.target_status`): PENDING -> QUEUED -> INSERTED, or FAILED (kept FAILED until inserted;
  `--retry-failed` runs the failed units). A QUEUED row left by a stopped run is picked again by the next run.
- The download check refuses an amending Act whose number is not in the title on page 1 (the principal Act as
  amended names it only in footnotes); titles lose a leading "Inserted by" / "Substituted vide".

## 10. Fact catalog: the fact flow (`fact_catalog_engine.py`, `fact_catalog_ss/`)

Built after the governance masters (`run.fact_catalog`), no LLM. For every fact of `akp_fact`: what it is, **how it is
obtained** (`DOCUMENT` on the document, `DERIVED` by Python from other facts, `LOOKUP` from a table, `ASK` the user /
ERP - `Config/fact_kinds.json`, reviewed by people) and **every law that uses it** (atom → MKP → TSG, and whether the
check is Python (LOW) or the judge (HIGH)).

| Output | What it is |
|---|---|
| `Governance_Master/fact_catalog.yaml` | The catalog |
| `fact_catalog` | One row per fact |
| `fact_index` | One row per fact → TSG → law (atom) |

`python fact_catalog_engine.py --facts F_INVOICE_DATE,F_SUPPLIER_STATE,...` says, per TSG, READY (every fact there,
or derivable), PARTIAL (with the missing facts by kind) or NOT_RUN. This picks **which** governance masters run for
a document; inside each one the engine still walks the steps in the law's order.

## 11. Deleting wrong knowledge points (`kp_delete.py`, `db_migrate.py`)

`kp_delete.py` also (bug list F23): lists the other laws whose SAME atom (`A_REF`) was an atom of a deleted law (run
them again, or the next run's SAME check maps them), removes the outside-document part links that pointed into the
deleted law and sets those references back to PENDING. `--same-review` and the SAME check after every run list
SAME atoms whose saved atom was reworded / removed in a new version (F24).

Delete an MKP row (pgAdmin, psql, or `python kp_delete.py --mkp <element>` / `--unit <key prefix>` / `--dry-run`;
needs `access.database_delete`) and database triggers delete everything made from it: its phrases, AKP atoms, fact
links, time facts, GKP groups, governance master rows, references and the LLM answers cached for its unit. Delete the
latest IKP node and its MKPs go too. Run `main.py` again for that scope: only that unit calls the LLM.

## 12. A new PDF of a document done before (`mkp_ss/carry.py`)

The MKP version of a document is its PDF (`version_id` = `V_` + the start of the PDF hash), so an updated act is a new
version. When MKP starts on it, `carry_forward()` runs once:

1. IKP has already read the new PDF as a new IKP version.
2. The IKP nodes of the old and new PDF are compared (status, heading, own text, amendments).
3. A law whose node did not change is copied to the new version with no LLM: MKP + phrases, BATCH / SUM of its
   cluster, AKP atoms, fact links, time facts, GKP groups, references.
4. A changed or new law is not copied: MKP / AKP / GKP make it again when its part runs.
5. The old version's rows get `is_latest = false` (kept, not deleted), so every reader (governance master, fact
   catalog, views) sees only the new PDF, and a law removed from the new PDF drops out.

The screen shows `NEW PDF : CGSTA17 V_old -> V_new: N law(s) unchanged, copied without LLM; M changed; K not in the new
PDF (dropped)`. Run the whole document once, then the governance master with `tsgs: "ALL"`, so every TSG yaml is rebuilt
from the new PDF (a TSG whose only laws were dropped is not "touched" by the run).

## 13. Relationship table and Fact Network page (`relationship_builder.py`, `relationship_ss/`)

main.py runs it at the end of every pipeline run (`relationship.run_after_pipeline`); you can also run it yourself. No LLM.

```
python relationship_builder.py                     new / changed MKPs only, then the page
python relationship_builder.py --full              every MKP again
python relationship_builder.py --only CGSTA17_S10  only MKPs whose id starts with this
python relationship_builder.py --page-only         only rewrite the page
```

It reads the latest rows of `akp`, `akp_atom_fact`, `akp_fact`, `gkp` and `governance_master` and writes one table,
`kp_relation`, one row per link.

| column | example | meaning |
|---|---|---|
| rel_id | 10452 | row number |
| mkp | CGSTA17_S7_SS1_Ca#TEXT | the MKP the row belongs to |
| instrument | CGSTA17 | the document |
| from_type, from_id, from_name | GROUP, CGSTA17_S7_SS1_Ca#TEXT::G_TIMING, ANY of A_MADE, A_AGREED | one end |
| relation | NEEDS | the link (below) |
| to_type, to_id, to_name | FACT, F_SUPPLY_STATUS, Status of supply | the other end |
| link | DIRECT / DERIVED | as the law is built, or worked out by Python (NEEDS) |
| via | A_MADE > G_TIMING | DERIVED rows only: the path to the fact |
| version, akp_version, version_id, fingerprint | 2, 1 | versioning per MKP |
| is_latest, inserted_at | true | old rows are kept with is_latest = false |

DIRECT relations: ATOM READS FACT; ATOM / GROUP / PD MEMBER_OF GROUP; ATOM / GROUP / PD USED_BY LE; LE PRODUCES PD;
OR OTHERWISE_OF LE; CT TRIGGERED_BY ATOM / GROUP / LE / FACT; TC TESTS LE; LE / PD / OR / CT / TC PART_OF MKP;
ATOM / GROUP / LE GROUPED_IN TSG; TSG BELONGS_TO TG; ATOM SAME_AS ATOM (another MKP); MKP USED_IN GOVERNANCE_MASTER;
MKP AMENDS PROVISION.

DERIVED: GROUP / LE / PD / OR / CT / TC / MKP / TSG / GOVERNANCE_MASTER NEEDS FACT - every element that depends on a
fact gets its own row, so "what needs this fact" is one query on `relation IN ('READS', 'NEEDS')`.

Ids: facts, TSGs, TGs, governance masters, provisions and MKPs keep their own id; atom, group, LE, PD, OR, CT and TC
ids are `<MKP>::<element>`, the same as `akp.akp_id`.

**Only new or changed MKPs are read.** Each MKP's fingerprint (its AKP version and rows, fact links, GKP groups,
governance masters and the builder version) is compared with the one saved on its rows. Unchanged MKPs are skipped;
a changed one gets its old rows set `is_latest = false` and new rows as the next version; an MKP no longer in the
database has its rows set `is_latest = false`. Deleting an MKP (`kp_delete.py`) also deletes its rows (cascade). A
`kp_relation` made by the first builder (columns molecule / from_label / to_label) is upgraded in place on the next
run and every MKP is rebuilt once.

**The page** `Output/Relationship/fact_network.html` is drawn only from the latest `kp_relation` rows: pick a fact
(most shared first), a TSG, or search an id; columns Fact → Atom → Group → LE → PD / OR / CT / TC → MKP → TSG /
governance master; type buttons hide a type; below are the rows and the SQL for the selection.

```sql
-- everything that needs a fact, every type
SELECT from_type, from_id, from_name, relation, via, mkp FROM kp_relation
WHERE to_id = 'F_SUPPLY_STATUS' AND relation IN ('READS', 'NEEDS') AND is_latest ORDER BY from_type, mkp;
-- the facts one law needs
SELECT DISTINCT to_id, to_name FROM kp_relation
WHERE from_id = 'CGSTA17_S7_SS1_Ca#TEXT::LE_SIAFOS' AND relation = 'NEEDS' AND is_latest;
-- how one MKP is built
SELECT from_type, from_id, relation, to_type, to_id FROM kp_relation
WHERE mkp = 'CGSTA17_S7_SS1_Ca#TEXT' AND link = 'DIRECT' AND is_latest;
```

## 14. The 2026-11-15 rebuild (what changed, where it lives)

Decided in the project notes (`claude/akp_rebuild_notes.md`); everything is re-run in the central database
(`Credentials/.env` LEGAL_DB_NAME / LEGAL_DB_SCHEMA - IKP, MKP, AKP, GKP all use LEGAL_DB_SCHEMA when
`ikp.schema` / `mkp.schema` are empty).

| Area | What | Where |
|---|---|---|
| Ids | MKP `M_<acronym of the concept name>`, unique across all laws (a clash gets a second letter, then a number); every AKP element `<MKP id>_<TYPE>_<acronym>`; facts `F_<acronym>` with no prefix. The models keep their working ids; Python rewrites every id just before saving. `akp.akp_id` is the id of every row (no element column). | `kp_ids.py`, `akp_ss/ids.py`, `mkp_ss/store.py` |
| Fact flow | layer 1: the fact list given to the model (same unit, provisions it refers to, its TSGs, nearest, most used); layer 2 (before the model): same name / saved alias -> the registered id, only when the type, entity and options fit (a same name with another type gets its own numbered id); layer 3: every new fact vs ALL registered facts, found by name + nl AND by concept; a fact with the same name but other option words goes to the model, which maps the options (`option_map`: AGREED_TO_BE_PAID -> PAYABLE, or NEW for a real new value) and Python rewrites the rules (SAME / DERIVED / NEW, Python gate on type, entity, options; a legal conclusion goes back to the models); layer 4: `python akp_engine.py --fact-review` (merge list, sheet "Fact review" of akp.xlsx) and `--fact-merge OLD=KEEP` (new AKP versions). `akp_fact` has aliases, source_kind, derivation, concept. The fact-flow numbers (reuse rate, new facts per MKP, facts used by one atom, possible duplicates) are printed after every run. SAME atoms are found by statement AND concept (`same_concept_min_similarity`, `same_concept_statement_floor`). LK rows are named by their relation (`M_IOSFC_LK_AMENDS`). | `akp_ss/facts.py`, `akp_ss/factcheck.py`, `akp_ss/review.py`, `law_arc_akp_fact_prompt.txt` |
| SAME | candidates above 0.90, plus 0.80-0.90 when they read the same facts with the same rule, plus by concept; exact copies SAME by Python, the rest by the SAME model (named by akp_id); a SAME atom = A_REF row with the saved atom's id (groups / LE use it); every unapplied answer logged; SAME check of the run's atoms after the workers; --same-review / --same-apply for saved atoms; the governance master writes a shared atom once; two atoms of one law with the same facts and rule are merged. | `akp_engine.py` (`_same_band`, `_python_same`, `_apply_same`, `same_sweep`), `akp_ss/same_review.py`, `akp_ss/ids.py`, `governance_master_ss/build.py` |
| Handler | every atom: `handler: python / light / heavy` (AKP decides while splitting, Python gate, the comparison checks it); written on every governance-master predicate. | `akp_ss/guard.py`, `governance_master_ss/build.py` |
| Law in force | `time: law_date: F_DOI` (Date Of Invoice, `Config/governance_master_map.json` law_date) in every governance master; the AKP time step makes old-text atoms of a provision changed after `akp.gst_start` and adds `valid_from` / `valid_to` + ANY groups; time facts keep effective_from and in_force_from. | `akp_ss/timestep.py`, `law_arc_akp_time_prompt.txt` |
| GKP | AKP's group is checked by a Python score (embedding, among the closest, MKP agrees / anchor / facts) - the verifiers only when Python is not sure, plus an audit share; `python_score` on every row; `calibrate` once, then `python gkp_engine.py --calibration`. MKP, AKP and GKP read the taxonomy from the `gkp_taxonomy` table (re-read when GKP creates a group). `gkp_tg18_proposals` removed. | `gkp_ss/score.py`, `gkp_ss/taxonomy.py` (`current`) |
| Worker pool | `pool.max_job_chars = 0`: every unit split down to its clauses; run waves (`pool.waves`): base laws first, a wave starts when the earlier waves are done. | `kp_pool.py`, `mkp_ss/waves.py` |
| Governance master | `Governance_Master/<TG>/<TSG>_rulebook.yaml` + `<TG>/report/<TSG>_report.xlsx` (no sub-folder per TSG; one report folder per TG). | `governance_master_engine.py` |
| Scraper | amending documents are fetched too (`Law/Amendments`), each document once; Python number / year check, then the LLM compares the PDF with the laws that cite it; an outside document takes the groups of the laws that cite it. | `ref_engine.py`, `ref_ss/` |
| Observer | `relationship_builder.py` runs at the end of every pipeline run (`relationship.run_after_pipeline`); the page shows facts most used first, the chain of any fact or element both ways, and filters for TSG, document, handler and "new since the last run", with each atom's time window; `relationship.share_copy_to` copies it to a folder the team opens. | `relationship_ss/`, `relationship_builder.py` |
