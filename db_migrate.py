"""One-time database upgrade to the IKP / MKP / AKP / GKP / governance master names (keeps every row).

Runs automatically the first time a connection is opened in a run (db_insert.get_connection and
ikp_engine.get_connection). It is safe to run again: every step checks first and does nothing when already done.

  tables     pass0_*            -> ikp_*                (pass0_index or ikp_index -> ikp; ikp_versions, ikp_chapters, ikp_nodes, ikp_llm_cache)
             pass_1_mkp*        -> mkp*                 (mkp, mkp_cache, mkp_llm_cache)
             pass_2_akp*        -> akp*                 (akp, akp_cache, akp_llm_cache)
             pass_2_fact / pass_2_atom_fact / pass_2_time_fact -> akp_fact / akp_atom_fact / akp_time_fact
             pass_3_gkp         -> gkp
             pass_3_g18_*       -> gkp_tg18_*
             pass_3_*           -> gkp_*                (gkp_cache, gkp_llm_cache, gkp_coverage, gkp_taxonomy)
             pass_4_law_pack*   -> governance_master*   (governance_master, governance_master_history)
  indexes    renamed with their table (old prefix -> new prefix)
  columns    group_id -> tg_id, subgroup_id -> tsg_id, group_name -> tg_name, subgroup_name -> tsg_name,
             approved_subgroup_id -> approved_tsg_id   (GKP and governance master tables)
  values     G01 -> TG01, G01_S03 -> TSG01_03, G18_P... -> TSG18_P...  (in those id columns)
"""
from __future__ import annotations

MIGRATION_SQL = r"""
DO $mig$
DECLARE
  sch text := %(schema)s;
  r record;
  newname text;
  c record;
BEGIN
  -- views are rebuilt by the governance master; drop them so tables and columns can be renamed
  EXECUTE format('DROP VIEW IF EXISTS %%I.v_molecule_elements CASCADE', sch);
  EXECUTE format('DROP VIEW IF EXISTS %%I.v_element_links CASCADE', sch);

  -- 1. tables (and their indexes)
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = sch AND (
           tablename LIKE 'pass0\_%%' OR tablename = 'ikp_index' OR tablename LIKE 'pass\_1\_mkp%%' OR tablename LIKE 'pass\_2\_%%'
           OR tablename LIKE 'pass\_3\_%%' OR tablename LIKE 'pass\_4\_law\_pack%%') LOOP
    newname := r.tablename;
    newname := regexp_replace(newname, '^pass0_index$', 'ikp');
    newname := regexp_replace(newname, '^ikp_index$', 'ikp');
    newname := regexp_replace(newname, '^pass0_', 'ikp_');
    newname := regexp_replace(newname, '^pass_1_mkp', 'mkp');
    newname := regexp_replace(newname, '^pass_2_akp', 'akp');
    newname := regexp_replace(newname, '^pass_2_(fact|atom_fact|time_fact)$', 'akp_\1');
    newname := regexp_replace(newname, '^pass_3_gkp$', 'gkp');
    newname := regexp_replace(newname, '^pass_3_g18_', 'gkp_tg18_');
    newname := regexp_replace(newname, '^pass_3_', 'gkp_');
    newname := regexp_replace(newname, '^pass_4_law_pack', 'governance_master');
    IF newname <> r.tablename AND to_regclass(format('%%I.%%I', sch, newname)) IS NULL THEN
      EXECUTE format('ALTER TABLE %%I.%%I RENAME TO %%I', sch, r.tablename, newname);
      FOR c IN SELECT indexname FROM pg_indexes WHERE schemaname = sch AND tablename = newname
               AND left(indexname, length(r.tablename)) = r.tablename LOOP
        IF to_regclass(format('%%I.%%I', sch, newname || substr(c.indexname, length(r.tablename) + 1))) IS NULL THEN
          EXECUTE format('ALTER INDEX %%I.%%I RENAME TO %%I', sch, c.indexname,
                         newname || substr(c.indexname, length(r.tablename) + 1));
        END IF;
      END LOOP;
      RAISE NOTICE 'renamed table %% -> %%', r.tablename, newname;
    END IF;
  END LOOP;

  -- 2. columns group / sub-group -> TG / TSG
  FOR c IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = sch AND (table_name LIKE 'gkp%%' OR table_name LIKE 'governance\_master%%')
             AND column_name IN ('group_id', 'subgroup_id', 'group_name', 'subgroup_name', 'approved_subgroup_id') LOOP
    newname := CASE c.column_name WHEN 'group_id' THEN 'tg_id' WHEN 'subgroup_id' THEN 'tsg_id'
                 WHEN 'group_name' THEN 'tg_name' WHEN 'subgroup_name' THEN 'tsg_name' ELSE 'approved_tsg_id' END;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = sch
                   AND table_name = c.table_name AND column_name = newname) THEN
      EXECUTE format('ALTER TABLE %%I.%%I RENAME COLUMN %%I TO %%I', sch, c.table_name, c.column_name, newname);
    END IF;
  END LOOP;

  -- 3. id values G01 -> TG01, G01_S03 -> TSG01_03, G18_P.. -> TSG18_P..
  FOR c IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = sch AND (table_name LIKE 'gkp%%' OR table_name LIKE 'governance\_master%%')
             AND column_name IN ('tg_id', 'tsg_id', 'approved_tsg_id', 'proposal_id') AND data_type = 'text' LOOP
    EXECUTE format($q$UPDATE %%I.%%I SET %%I = regexp_replace(regexp_replace(regexp_replace(%%I,
                     '^G(\d\d)_S(\d\d)$', 'TSG\1_\2'), '^G(\d\d)$', 'TG\1'), '^G18_', 'TSG18_')
                   WHERE %%I ~ '^G\d\d'$q$, sch, c.table_name, c.column_name, c.column_name, c.column_name);
  END LOOP;
END
$mig$;
"""

_done = set()


def migrate(conn, schema: str = "public") -> None:
    """Rename old tables / columns / ids once per process. Never deletes data."""
    key = (id(conn), schema)
    if not (schema in _done or key in _done):
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL, {"schema": schema})
        conn.commit()
        _done.add(schema)
    try:
        install_cascade(conn, schema)
    except Exception:
        conn.rollback()           # the cascade is a convenience: a failure here must not stop the run


# ---------------------------------------------------------------- delete cascade (permission.json access.database_delete)
#
# Delete an MKP (in pgAdmin, psql or `python kp_delete.py`) and everything made from it goes too: its phrases,
# AKP atoms, facts links, time facts, GKP groups, governance master rows, cross references, kp_relation links and the LLM answers
# cached for it, so a re-run makes it again from the law. Delete an IKP node (latest) and its MKPs go, with all of
# the above. The tables are found by name at delete time, so a table that does not exist yet is simply skipped.

import re as _re

_cascade_done = set()


def _names(cfg: dict) -> dict:
    akp = cfg["akp"].get("table", "akp")
    base = akp.rsplit("_akp", 1)[0]
    gkp = cfg["gkp"].get("table_prefix", "gkp")
    refs = cfg.get("refs") or {}
    return {"mkp": cfg["mkp"].get("table", "mkp"), "akp": akp, "akp_cache": cfg["akp"].get("cache_table", "akp_cache"),
            "akp_llm": f"{akp}_llm_cache", "fact_edge": f"{base}_atom_fact", "time_fact": f"{base}_time_fact",
            "gkp": gkp, "gkp_cache": f"{gkp}_cache", "gkp_llm": f"{gkp}_llm_cache",
            "gm": cfg["governance_master"].get("table", "governance_master"),
            "ref_link": refs.get("link_table", "mkp_ref"), "ikp_nodes": f"{cfg['ikp'].get('table_prefix', 'ikp')}_nodes",
            "relation": (cfg.get("relationship") or {}).get("table", "kp_relation")}


def cascade_sql(schema: str, n: dict) -> str:
    for v in list(n.values()) + [schema]:
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
            raise ValueError(f"unsafe table name {v!r}")
    q = lambda t: f'"{schema}"."{t}"'

    def step(table: str, where: str, guard_column: bool = False) -> str:
        where = where.replace("'", "''")
        body = f"EXECUTE 'DELETE FROM {q(table)} WHERE {where}' USING OLD.version_id, OLD.element;"
        if guard_column:     # a column added by newer code (unit_key, kp_relation.mkp): an older table is skipped
            body = f"BEGIN {body} EXCEPTION WHEN undefined_column THEN NULL; END;"
        return f"  IF to_regclass('{q(table)}') IS NOT NULL THEN {body} END IF;\n"

    mkp = n["mkp"]
    fn = f"kp_cascade_{mkp}"
    return (
        f"CREATE OR REPLACE FUNCTION \"{schema}\".\"{fn}\"() RETURNS trigger LANGUAGE plpgsql AS $kp$\nBEGIN\n"
        + step(mkp, "version_id = $1 AND element = $2 AND type <> 'MKP'")
        + step(n["akp"], "version_id = $1 AND source_mkp = $2")
        + step(n["akp_cache"], "version_id = $1 AND source_mkp = $2")
        + step(n["akp_llm"], "unit_key = $2", True)
        + step(n["fact_edge"], "version_id = $1 AND source_mkp = $2")
        + step(n["time_fact"], "version_id = $1 AND amendment_mkp = $2")
        + step(n["gkp"], "version_id = $1 AND source_mkp = $2")
        + step(n["gkp_cache"], "version_id = $1 AND source_mkp = $2")
        + step(n["gkp_llm"], "unit_key = $2", True)
        + step(n["gm"], "source_mkp = $2 AND $1 IS NOT NULL")
        + step(n["ref_link"], "version_id = $1 AND mkp_element = $2")
        + step(n.get("relation", "kp_relation"), "version_id = $1 AND mkp = $2", True)
        + step(f"{mkp}_llm_cache", "$2 LIKE unit_key || '%' AND $1 IS NOT NULL", True)
        + step(f"{mkp}_cache", "version_id = $1 AND $2 LIKE scope_key || '%'")
        + "  RETURN OLD;\nEND\n$kp$;\n"
        f"DROP TRIGGER IF EXISTS kp_cascade ON {q(mkp)};\n"
        f"CREATE TRIGGER kp_cascade AFTER DELETE ON {q(mkp)} FOR EACH ROW WHEN (OLD.type = 'MKP') "
        f"EXECUTE FUNCTION \"{schema}\".\"{fn}\"();\n"
        f"CREATE OR REPLACE FUNCTION \"{schema}\".\"kp_cascade_{n['ikp_nodes']}\"() RETURNS trigger LANGUAGE plpgsql AS $kp$\n"
        f"BEGIN\n  IF OLD.is_latest AND to_regclass('{q(mkp)}') IS NOT NULL THEN\n"
        f"    EXECUTE 'DELETE FROM {q(mkp)} WHERE instrument_code = $1 AND split_part(element, ''#'', 1) = $2' "
        f"USING OLD.doc, OLD.element;\n  END IF;\n  RETURN OLD;\nEND\n$kp$;\n"
        f"DROP TRIGGER IF EXISTS kp_cascade ON {q(n['ikp_nodes'])};\n"
        f"CREATE TRIGGER kp_cascade AFTER DELETE ON {q(n['ikp_nodes'])} FOR EACH ROW "
        f"EXECUTE FUNCTION \"{schema}\".\"kp_cascade_{n['ikp_nodes']}\"();\n")


def install_cascade(conn, schema: str = "public", names: dict = None) -> bool:
    """Create the delete triggers once the MKP and IKP tables exist (tried again on the next connection if not)."""
    if names is None:
        try:
            import kp_settings
            names = _names(kp_settings.load())
        except Exception:
            return False
    key = (schema, names["mkp"])
    if key in _cascade_done:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s), to_regclass(%s)",
                    (f'"{schema}"."{names["mkp"]}"', f'"{schema}"."{names["ikp_nodes"]}"'))
        have_mkp, have_ikp = cur.fetchone()
    if not have_mkp or not have_ikp:
        return False
    with conn.cursor() as cur:
        cur.execute(cascade_sql(schema, names))
    conn.commit()
    _cascade_done.add(key)
    return True


# ---------------------------------------------------------------- table setup once while the worker pool runs
#
# CREATE / ALTER TABLE takes strong locks. If several workers run it while others read, PostgreSQL can deadlock.
# So the pool sets the tables up once in the main thread (kp_pool.Pool.start) and the workers skip it.

import threading as _threading

POOL_READY = False                     # True while the worker pool runs (tables already set up)
_ensured = set()
_ensure_lock = _threading.Lock()


def run_ensure(key, fn) -> None:
    """Call a store's table setup, unless the pool is running and it was already done for this table."""
    if POOL_READY and key in _ensured:
        return
    fn()
    with _ensure_lock:
        _ensured.add(key)
