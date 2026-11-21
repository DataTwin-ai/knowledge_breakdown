"""DARP LAW ARC - RELATIONSHIP BUILDER: how facts, atoms, groups, laws, TSGs and governance masters connect.

    python relationship_builder.py                       new / changed MKPs only, then the page
    python relationship_builder.py --full                every MKP again
    python relationship_builder.py --only CGSTA17_S10    only MKPs whose id starts with this
    python relationship_builder.py --page-only           only rewrite the page from kp_relation

main.py runs it at the end of every pipeline run (relationship.run_after_pipeline), so the observer page always shows
the latest graph; run it yourself too, whenever you want. It reads the latest rows of the database - akp
(A, G, LE, OR, CT, PD, LK, A_REF), akp_atom_fact (atom -> fact), akp_fact (fact names), gkp (TSG of each element) and
governance_master (which TSGs use an MKP) - and writes:
    PostgreSQL  kp_relation   one row per link (relationship_ss/build.py lists them): DIRECT rows as the law is built,
                              DERIVED "X NEEDS FACT" rows for every type that depends on a fact. Versioned per MKP.
                              Each MKP's fingerprint is compared first, so a second run reads only new / changed MKPs.
    Output/Relationship/fact_network.html   the Fact Network page, drawn ONLY from kp_relation (a picture of the table).
No LLM. Settings: Config/settings.json "relationship".
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional

from kp_console import flow
from relationship_ss.build import links
from relationship_ss.page import write as write_page
from relationship_ss.store import RelationStore

BASE_DIR = Path(__file__).resolve().parent
CHUNK = 200                                # MKPs read and written per transaction


def _names(cfg: dict) -> dict:
    from db_migrate import _names as tables
    n = tables(cfg)
    fact = cfg["akp"]["fact_table"]
    return {"akp": n["akp"], "fact_edge": n["fact_edge"], "fact": fact, "gkp": n["gkp"], "gm": n["gm"]}


def run(full: bool = False, only: str = "", page_only: bool = False, conn=None, cfg: Optional[dict] = None) -> dict:
    import kp_settings
    cfg = cfg or kp_settings.load()
    s = cfg["relationship"]
    page = Path(s["page"]) if Path(s["page"]).is_absolute() else BASE_DIR / s["page"]
    kp_settings.require(cfg, "WRITE", page)
    own = conn is None
    if own:
        from db_insert import get_connection
        conn = get_connection()
    from db_insert import get_db_schema
    started = time.time()
    out = {"new": 0, "changed": 0, "same": 0, "removed": 0, "rows": 0, "page": ""}
    try:
        store = RelationStore(conn, get_db_schema(), s["table"], _names(cfg))
        store.ensure()
        run_started = store.now()             # links (re)built from here on are "new since the last run" on the page
        if not page_only:
            current, saved = store.current(only), store.saved(only)
            todo = sorted(m for m in current if full or saved.get(m) != current[m][0])
            out["new"] = sum(1 for m in todo if m not in saved)
            out["changed"] = len(todo) - out["new"]
            out["same"] = len(current) - len(todo)
            flow(f"RELATION   : {len(current)} MKP(s) in the database - {out['new']} new, {out['changed']} "
                 f"{'rebuilt' if full else 'changed'}, {out['same']} unchanged (not read)")
            for i in range(0, len(todo), CHUNK):
                part = todo[i:i + CHUNK]
                src = store.source(part)
                built = {}
                for m in part:
                    v = src[m]
                    rows = links(m, v["akp"], v["edges"], v["gkp"], v["gm"], v["names"], v["same"], v.get("mkp_id", ""))
                    fp, vid, av = current[m]
                    built[m] = (rows, fp, vid, av)
                    out["rows"] += len(rows)
                store.save(built)
                flow(f"RELATION   : {min(i + CHUNK, len(todo))}/{len(todo)} MKP(s) written")
            gone = set(saved) - set(current)
            if gone:
                store.retire(gone)
                out["removed"] = len(gone)
                flow(f"RELATION   : {len(gone)} MKP(s) no longer in the database - their links set is_latest = false")
        latest = store.latest_rows(run_started)
        out["page"] = str(write_page(latest, page))
        flow(f"RELATION   : {len(latest)} latest row(s) in {s['table']} - {time.time() - started:.1f}s")
        flow(f"PAGE       : {out['page']}")
        share = str(s.get("share_copy_to") or "").strip()
        if share:                             # the team opens it from a shared folder (notes section 14)
            import shutil
            dest = Path(share)
            dest = dest / page.name if dest.suffix.lower() != ".html" else dest
            try:
                kp_settings.require(cfg, "WRITE", dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(out["page"], dest)
                out["shared"] = str(dest)
                flow(f"PAGE       : copied for the team to {dest}")
            except Exception as exc:
                flow(f"PAGE       : not copied to {dest} ({str(exc)[:120]})")
        return out
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DARP LAW ARC relationship builder (kp_relation + Fact Network page)")
    ap.add_argument("--full", action="store_true", help="build every MKP again, not only new / changed ones")
    ap.add_argument("--only", default="", help="only MKPs whose id starts with this, e.g. CGSTA17_S10")
    ap.add_argument("--page-only", action="store_true", help="only rewrite the page from kp_relation")
    a = ap.parse_args()
    run(a.full, a.only, a.page_only)
