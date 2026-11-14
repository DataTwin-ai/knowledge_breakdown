# DARP LAW ARC: GST law → knowledge points → Decision Graph Engine

LAW ARC turns GST law PDFs (Acts, Rules, Notifications, Circulars) into knowledge points, and keeps one
**governance master** per TSG (taxonomy sub-group) for the Decision Graph Engine, plus a **fact catalog** that says
which laws each fact can check.

```
PDF ─► IKP ─► worker pool [ MKP ─► AKP ─► GKP ] ─► GOVERNANCE MASTER ─► FACT CATALOG ─► Decision Graph Engine
     index     many units at the same time          one per TSG          fact -> laws
```

| Step | What it makes | Main table(s) |
|---|---|---|
| IKP | The index of the book: every chapter, section, sub-section, clause, proviso and explanation, with exact text, pages and PDF position | `ikp`, `ikp_nodes` |
| MKP | One molecule per provision (exact legal text + source phrases), its **groups** (TSG ids) and its **references** (same document / outside documents) | `mkp`, `mkp_ref`, `ref_document` |
| AKP | The molecule split into concept atoms, logic groups, law expressions, outcomes, catalysts, test cases and facts; every atom carries its groups | `akp`, `akp_fact`, `akp_atom_fact`, `akp_time_fact` |
| GKP | Every atom in the GST taxonomy (15 TG / 81 TSG). Groups given by MKP / AKP are taken as they are; the LLM is asked only for the rest; when nothing fits GKP **creates** a sub-group (never "unclassified") | `gkp`, `gkp_taxonomy` |
| GOVERNANCE MASTER | One `rulebook.yaml` per TSG (the engine's format): its laws + the laws they point to + the spine | `governance_master` (+ `_history`), `Governance_Master/<TG>/<TSG>/rulebook.yaml` |
| FACT CATALOG | Every fact, how it is obtained (document / derived / lookup / ask) and every law it can check | `fact_catalog`, `fact_index`, `Governance_Master/fact_catalog.yaml` |

## Quick start

1. **Credentials.** Keep your own `Credentials/` folder (not shipped): `.env` (`LEGAL_DB_NAME`, `LEGAL_DB_USER`,
   `LEGAL_DB_PASSWORD`, optional `LEGAL_DB_HOST` / `LEGAL_DB_PORT`), `service_crendential.json` (Gemini, Vertex
   embeddings), `claude_crendential.json` (Claude).
2. **Database.** PostgreSQL with `pgvector`. Tables are created by the steps. Old table names are renamed and the
   delete triggers installed automatically (`db_migrate.py`), keeping every row.
3. **Python packages:** `pymupdf`, `psycopg2-binary`, `python-dotenv`, `openpyxl`, `xlsxwriter`, `pandas`,
   `google-genai`, `anthropic`, `lark`, `pyyaml`, `pytest`.
4. **Law PDFs:** `Law/Acts`, `Law/Rules`, `Law/Notifications`, `Law/Circulars` - or a `url` in
   `Config/documents.json` and the PDF is downloaded at the start.
5. **Choose** in `permission.json`: `run`, `use_cache`, `read` / `documents` (which laws), `scope`, `workers`,
   `features`, `access`, `console`, `governance_master`, `llm`.
6. **Run:** `python main.py` (`--retry-failed` for the failed units only, `--documents CODE,CODE` for some documents)
7. **Test:** `python -m pytest -q` (database and PDF tests skip themselves when those are not available).

Other commands: `python kp_delete.py --unit CGSTA17_S7` (delete wrong MKPs and everything made from them),
`python ref_engine.py` (web scraping of cited notifications / circulars; off by default),
`python fact_catalog_engine.py --facts F_A,F_B` (which TSGs those facts can check).

## Documentation

| File | Read it for |
|---|---|
| `docs/HOW_IT_WORKS.md` | The full flow, worker pool, settings, cache and versions, tables and outputs |
| `docs/GOVERNANCE_MASTER.md` | Governance masters: how they are built and updated, the main table, tests |
| `docs/IKP_DETAILS.md` | IKP internals and results on the sample PDFs |
| `CLAUDE.md` | Working rules for Claude Code in this repo |
