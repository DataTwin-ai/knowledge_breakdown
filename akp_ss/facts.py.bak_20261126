"""AKP / the fact registry - the backbone of the fact network.

A FACT is an observable input about a real case, with its evidence (e.g. F_PERSON_DATE_OF_BIRTH, evidence Aadhaar;
F_SUPPLY_CONSIDERATION = PAID / NONE, evidence invoice). Atoms TEST facts. Atoms of different laws that test
the same fact are connected through it - so the same fact must keep ONE id everywhere.

akp_fact        one row per canonical fact: id, name, entity, type, unit, options, evidence, derived_from, embedding,
                aliases (other names of the same fact), source_kind (DOCUMENT / WORKED_OUT / LOOKUP / ASK), derivation
akp_atom_fact   edge atom -> fact (with the atom's rule), per AKP version, is_latest
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Tuple

from psycopg2 import sql

from transport import get_field

from .parse import field_list, rfield

DATA_TYPES = {"BOOL", "BOOLEAN", "CODE", "ENUM", "AMOUNT", "MONEY", "NUMBER", "INTEGER", "PERCENT", "RATE", "DATE",
              "PERIOD", "DURATION", "TEXT", "STRING", "LIST", "ID", "QUANTITY"}
SOURCE_KINDS = {"DOCUMENT", "WORKED_OUT", "LOOKUP", "ASK"}    # where a fact's value comes from (notes section 2)
CANON_TYPE = {"BOOLEAN": "BOOL", "ENUM": "CODE", "MONEY": "AMOUNT", "INTEGER": "NUMBER", "RATE": "PERCENT",
              "STRING": "TEXT", "DURATION": "PERIOD"}


def canon_type(t: str) -> str:
    t = (t or "").strip().upper()
    return CANON_TYPE.get(t, t)


def _vec(v) -> Optional[str]:
    return "[" + ",".join(str(float(x)) for x in v) + "]" if v else None


_SMALL = {"of", "the", "a", "an", "for", "to", "by", "in", "on", "and", "or", "is", "at"}


def _concept(row: dict) -> str:
    """The 2-3 word concept of a FACTS row (its CONCEPT column), without small words ("Form Of Supply" ->
    "Form Supply"); '' when the model wrote none (NA)."""
    c = " ".join(str(row.get("CONCEPT") or "").replace('"', "").split())
    if c.upper() in ("", "NA", "N/A", "NONE"):
        return ""
    return " ".join(w for w in c.split() if w.lower() not in _SMALL)[:80] or c[:80]


def concept_warnings(rows: List[dict]) -> List[str]:
    """FACTS rows whose concept will not help the search: missing, more than 3 words, or the name repeated."""
    from transport import get_field
    out = []
    for r in rows:
        if r.get("TYPE") != "FACTS":
            continue
        c, name = _concept(r), get_field(r, "name")
        if not c:
            out.append(f"{r['ELEMENT']}: no concept")
        elif len(c.split()) > 3:
            out.append(f"{r['ELEMENT']}: concept '{c}' has more than 3 words")
        elif c.lower() == " ".join(w for w in str(name).split() if w.lower() not in _SMALL).lower():
            out.append(f"{r['ELEMENT']}: concept '{c}' only repeats the name")
    return out



def retry_concepts(rows: List[dict], known: Dict[str, dict], ask: Callable[[str], str], prompt: str,
                   note: Callable[[str], None] = lambda _m: None) -> int:
    """Bug list B11: a NEW fact whose concept is missing, longer than 3 words or only its name gets one more try
    (one small call for all of them). A concept that is still weak is kept as it was. Returns the concepts fixed."""
    from transport import get_field
    weak = {w.split(":", 1)[0]: w.split(":", 1)[1].strip() for w in concept_warnings(rows)}
    todo = [r for r in rows if r.get("TYPE") == "FACTS" and r["ELEMENT"] in weak and r["ELEMENT"] not in known]
    if not todo:
        return 0
    req = {"facts": [{"fact": r["ELEMENT"], "name": get_field(r, "name"), "type": rfield(r, "type"),
                      "options": field_list(r, "options"), "nl": get_field(r, "nl"), "concept_now": _concept(r),
                      "problem": weak[r["ELEMENT"]]} for r in todo]}
    try:
        raw = ask(prompt + "\nINPUT:\n" + json.dumps(req, ensure_ascii=False))
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
        got = {str(c.get("fact")): str(c.get("concept") or "") for c in
               (json.loads(text[text.find("{"): text.rfind("}") + 1]).get("concepts") or [])}
    except Exception as exc:
        from llm_runtime_errors import is_rate_limited
        if is_rate_limited(exc):
            raise
        note(f"FACT CONCEPT           : retry not done - {type(exc).__name__}: {str(exc)[:120]}")
        return 0
    fixed = 0
    for r in todo:
        old = r.get("CONCEPT")
        r["CONCEPT"] = got.get(r["ELEMENT"], "") or old
        if concept_warnings([r]):
            r["CONCEPT"] = old                          # still weak: keep what the model first wrote
            continue
        note(f"FACT CONCEPT           : {r['ELEMENT']} concept '{_concept({'CONCEPT': old})}' -> '{_concept(r)}'")
        fixed += 1
    return fixed


def _note_embed(fact_id: str, what: str, exc: Exception) -> None:
    """An embedding that could not be made is logged, not hidden (bug list B7)."""
    try:
        from kp_console import log
        log(f"FACT EMBEDDING         : {fact_id} {what} embedding not made - {type(exc).__name__}: {str(exc)[:120]}")
    except Exception:
        pass

class FactRegistry:
    def __init__(self, conn, schema: str, fact_table: str, edge_table: str):
        """Table names from Config/settings.json akp.fact_table / akp.atom_fact_table - none is made here."""
        self.conn, self.schema = conn, schema
        self.fact_t, self.edge_t = fact_table, edge_table

    @classmethod
    def from_settings(cls, conn, schema: str, s: dict) -> "FactRegistry":
        return cls(conn, schema, s["fact_table"], s["atom_fact_table"])

    def t(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def ensure(self) -> None:
        """Create / upgrade the tables (once while the worker pool runs: db_migrate.run_ensure)."""
        from db_migrate import run_ensure
        run_ensure(("FACT", self.schema, self.fact_t), self._ensure)

    def _ensure(self):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                fact_id text PRIMARY KEY, name text NOT NULL, entity text, data_type text, unit text,
                options jsonb NOT NULL DEFAULT '[]', evidence jsonb NOT NULL DEFAULT '[]', derived_from jsonb NOT NULL DEFAULT '[]',
                nl text, first_instrument text, first_source_mkp text, possible_duplicate_of text,
                embedding vector, inserted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
                        .format(self.t(self.fact_t)))
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                instrument text NOT NULL, version_id text NOT NULL, source_mkp text NOT NULL, akp_version integer NOT NULL,
                atom text NOT NULL, fact_id text NOT NULL, rule text, is_latest boolean NOT NULL DEFAULT true,
                inserted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, akp_version, atom, fact_id))""").format(self.t(self.edge_t)))
            # fact flow (notes section 8): other names of the same fact, where its value comes from, its formula
            # + the fact's 2-3 word concept and its own embedding: a new fact is compared by name + nl AND by concept
            for col, typ in (("aliases", "jsonb NOT NULL DEFAULT '[]'"), ("source_kind", "text"), ("derivation", "text"),
                             ("concept", "text"), ("concept_embedding", "vector")):
                cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + typ).format(
                    self.t(self.fact_t), sql.Identifier(col)))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (fact_id) WHERE is_latest").format(
                sql.Identifier(f"{self.edge_t}_fact_idx"), self.t(self.edge_t)))
        self.conn.commit()

    # ---------------- reads
    def all_ids(self) -> Dict[str, dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT fact_id, name, data_type, aliases, entity, options FROM {}")
                        .format(self.t(self.fact_t)))
            # entity + options: Python's SAME gate also runs on a match by name (fact flow layer 2)
            return {f: {"name": n, "type": t, "aliases": list(al or []) if isinstance(al, list) else [],
                        "entity": e or "", "options": list(o or []) if isinstance(o, list) else []}
                    for f, n, t, al, e, o in cur.fetchall()}

    def slice(self, unit_prefix: str, vector=None, limit: int = 30, tsgs: Optional[List[str]] = None,
              gkp_table: str = "", ref_keys: Optional[List[str]] = None) -> List[dict]:
        """Fact flow layer 1 (notes section 8) - the facts the models should reuse: those already used in the same
        unit, then by the provisions this MKP refers to, then by atoms of the MKP's TSGs, then the nearest by
        meaning, then the most used."""
        cols = "f.fact_id, f.name, f.entity, f.data_type, f.unit, f.options, f.evidence, f.concept"
        out, seen = [], set()

        def add(rows):
            for r in rows:
                if r[0] not in seen and len(out) < limit:
                    seen.add(r[0])
                    out.append({"fact_id": r[0], "name": r[1], "entity": r[2], "type": r[3], "unit": r[4],
                                "options": r[5], "evidence": r[6], "concept": r[7] or ""})
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(f"SELECT DISTINCT {cols} FROM {{}} f JOIN {{}} e ON e.fact_id=f.fact_id "
                                "WHERE e.is_latest AND e.source_mkp LIKE %s").format(self.t(self.fact_t), self.t(self.edge_t)),
                        (unit_prefix + "%",))
            add(cur.fetchall())
            for ref in ref_keys or []:          # 'as defined in section 2(52)', 'Schedule II' ...
                cur.execute(sql.SQL(f"SELECT DISTINCT {cols} FROM {{}} f JOIN {{}} e ON e.fact_id=f.fact_id "
                                    "WHERE e.is_latest AND (e.source_mkp LIKE %s OR e.source_mkp LIKE %s)")
                            .format(self.t(self.fact_t), self.t(self.edge_t)), (ref + "#%", ref + "\\_%"))
                add(cur.fetchall())
            if tsgs and gkp_table:
                try:
                    cur.execute(sql.SQL(f"SELECT {cols} FROM {{}} f JOIN {{}} e ON e.fact_id=f.fact_id AND e.is_latest "
                                        "JOIN {} g ON g.instrument=e.instrument AND g.version_id=e.version_id "
                                        "AND g.source_mkp=e.source_mkp AND g.akp_element=e.atom AND g.is_latest "
                                        "AND g.status='APPROVED' WHERE g.tsg_id = ANY(%s) "
                                        "GROUP BY f.fact_id ORDER BY count(*) DESC, f.fact_id LIMIT %s")
                                .format(self.t(self.fact_t), self.t(self.edge_t), self.t(gkp_table)), (list(tsgs), limit))
                    add(cur.fetchall())
                except Exception:              # GKP has not run yet
                    self.conn.rollback()
            if vector:
                try:
                    cur.execute(sql.SQL(f"SELECT {cols} FROM {{}} f WHERE embedding IS NOT NULL AND vector_dims(embedding)=%s "
                                        "ORDER BY embedding <=> %s::vector LIMIT %s").format(self.t(self.fact_t)),
                                (len(vector), _vec(vector), limit))
                    add(cur.fetchall())
                except Exception:
                    self.conn.rollback()
            cur.execute(sql.SQL(f"SELECT {cols} FROM {{}} f LEFT JOIN {{}} e ON e.fact_id=f.fact_id AND e.is_latest "
                                "GROUP BY f.fact_id ORDER BY count(e.atom) DESC, f.fact_id LIMIT %s")
                        .format(self.t(self.fact_t), self.t(self.edge_t)), (limit,))
            add(cur.fetchall())
        return out

    def backfill(self, embed: Callable, note: Callable[[str], None] = lambda _m: None, limit: int = 2000) -> int:
        """Bug list B7: a fact with no name embedding or no concept embedding (saved before the concept, or its
        embedding failed) gets them now - the fact check searches by both. Returns the facts filled."""
        if not embed:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT fact_id, name, nl, concept, embedding IS NULL, concept_embedding IS NULL
                FROM {} WHERE embedding IS NULL OR (COALESCE(concept, '') <> '' AND concept_embedding IS NULL)
                LIMIT %s""").format(self.t(self.fact_t)), (limit,))
            rows = cur.fetchall()
        done = 0
        for fid, name, nl, concept, no_vec, no_cvec in rows:
            try:
                v = embed(f"{name} - {nl or ''}".strip(" -")) if no_vec else None
                cv = embed(concept) if no_cvec and concept else None
            except Exception as exc:
                note(f"FACT EMBEDDING         : {fid} not embedded - {type(exc).__name__}: {str(exc)[:120]}")
                break
            with self.conn.cursor() as cur:
                cur.execute(sql.SQL("""UPDATE {} SET embedding = COALESCE(embedding, %s::vector),
                    concept_embedding = COALESCE(concept_embedding, %s::vector) WHERE fact_id=%s""")
                            .format(self.t(self.fact_t)), (_vec(v), _vec(cv), fid))
            done += 1
        self.conn.commit()
        return done

    def nearest(self, vector, k: int, min_similarity: float, concept_vector=None) -> List[dict]:
        """Fact flow layer 3: the registered facts nearest in meaning to a new fact (all facts, not only a slice) -
        by name + nl (embedding) AND by concept (concept_embedding), like atoms. similarity = the higher of both."""
        found: Dict[str, dict] = {}
        for col, vec in (("embedding", vector), ("concept_embedding", concept_vector)):
            if not vec:
                continue
            with self.conn.cursor() as cur:
                try:
                    cur.execute(sql.SQL("""SELECT fact_id, name, entity, data_type, unit, options, nl, aliases, concept,
                        1 - ({c} <=> %s::vector) FROM {t} WHERE {c} IS NOT NULL AND vector_dims({c})=%s
                        ORDER BY {c} <=> %s::vector LIMIT %s""").format(c=sql.Identifier(col), t=self.t(self.fact_t)),
                                (_vec(vec), len(vec), _vec(vec), k))
                    rows = cur.fetchall()
                except Exception:
                    self.conn.rollback()
                    continue
            key = "similarity" if col == "embedding" else "concept_similarity"
            for r in rows:
                if r[9] is None:
                    continue
                item = found.setdefault(r[0], {"fact_id": r[0], "name": r[1], "entity": r[2], "type": r[3],
                                               "unit": r[4], "options": r[5], "nl": r[6], "aliases": r[7],
                                               "concept": r[8] or ""})
                item[key] = round(float(r[9]), 4)
        out = []
        for item in found.values():
            best = max(item.get("similarity", 0.0), item.get("concept_similarity", 0.0))
            if best >= min_similarity:
                item["similarity"] = best
                out.append(item)
        return sorted(out, key=lambda x: (-x["similarity"], x["fact_id"]))[:k]

    def export_rows(self):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT f.fact_id, f.name, f.entity, f.data_type, f.unit, f.options::text, f.evidence::text,
                count(e.atom), f.possible_duplicate_of, f.first_source_mkp, COALESCE(f.concept, '') FROM {} f LEFT JOIN {} e
                ON e.fact_id=f.fact_id AND e.is_latest GROUP BY f.fact_id ORDER BY f.fact_id""")
                        .format(self.t(self.fact_t), self.t(self.edge_t)))
            return cur.fetchall()

    # ---------------- check the Standard table against the registry
    def check(self, rows: List[dict]) -> List[str]:
        known = self.all_ids()
        errors = []
        for r in rows:
            if r["TYPE"] != "FACTS":
                continue
            fid, t = r["ELEMENT"], canon_type(rfield(r, "type"))
            if t and t not in {canon_type(x) for x in DATA_TYPES}:
                errors.append(f"{fid}: type '{rfield(r, 'type')}' - use one of BOOL, CODE, AMOUNT, NUMBER, PERCENT, DATE, PERIOD, TEXT, LIST, ID, QUANTITY")
            if fid in known and known[fid]["type"] and t and known[fid]["type"] != t:
                errors.append(f"{fid}: already registered as {known[fid]['type']} ('{known[fid]['name']}') - "
                              f"reuse it with that type or use a different fact id")
        return errors

    # ---------------- save (inside the caller's transaction)
    def save(self, cur, rows: List[dict], instrument: str, version: str, source_mkp: str, akp_version: int,
             embed: Optional[Callable] = None) -> Tuple[int, int]:
        known = self.all_ids()
        new = 0
        for r in rows:
            if r["TYPE"] != "FACTS":
                continue
            fid = r["ELEMENT"]
            options = field_list(r, "options")
            evidence = field_list(r, "evidence") or ([get_field(r, "evidence")] if get_field(r, "evidence") else [])
            derived = field_list(r, "derived_from")
            if fid in known:
                # the same fact under another name in this law: that name is kept as an alias (fact flow)
                other = get_field(r, "name")
                alias = [other] if other and other.strip().lower() != str(known[fid].get("name") or "").strip().lower() else []
                concept = _concept(r)
                if concept:                      # a fact saved before it had a concept gets this law's concept
                    cv = None
                    if embed:
                        try:
                            cv = embed(concept)
                        except Exception as exc:
                            cv = None
                            _note_embed(fid, "concept", exc)
                    cur.execute(sql.SQL("""UPDATE {} SET concept=%s, concept_embedding=%s::vector
                        WHERE fact_id=%s AND COALESCE(concept, '') = ''""").format(self.t(self.fact_t)),
                                (concept, _vec(cv), fid))
                cur.execute(sql.SQL("""UPDATE {} SET
                    options = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]') FROM jsonb_array_elements(options || %s::jsonb) x),
                    evidence = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]') FROM jsonb_array_elements(evidence || %s::jsonb) x),
                    aliases = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]') FROM jsonb_array_elements(aliases || %s::jsonb) x),
                    updated_at = now() WHERE fact_id=%s""").format(self.t(self.fact_t)),
                            (json.dumps(options), json.dumps(evidence), json.dumps(alias), fid))
                continue
            name = get_field(r, "name") or fid
            nl = get_field(r, "nl")
            concept = _concept(r)
            vector, cvector, dup = None, None, None
            if embed:
                try:
                    vector = embed(f"{name} - {nl}".strip(" -"))
                except Exception as exc:              # logged (bug list B7); the catch-up step fills it next run
                    vector = None
                    _note_embed(fid, "name", exc)
                try:
                    cvector = embed(concept) if concept else None
                except Exception as exc:
                    cvector = None
                    _note_embed(fid, "concept", exc)
            if vector:
                cur.execute(sql.SQL("SELECT fact_id, 1 - (embedding <=> %s::vector) FROM {} WHERE embedding IS NOT NULL "
                                    "AND vector_dims(embedding)=%s ORDER BY embedding <=> %s::vector LIMIT 1")
                            .format(self.t(self.fact_t)), (_vec(vector), len(vector), _vec(vector)))
                hit = cur.fetchone()
                if hit and hit[1] is not None and float(hit[1]) >= 0.93:
                    dup = hit[0]
            entity = get_field(r, "entity") or (fid.split("_")[1] if fid.count("_") >= 2 else "")
            kind = (rfield(r, "source_kind") or "").upper().replace(" ", "_") or None
            cur.execute(sql.SQL("""INSERT INTO {} (fact_id, name, entity, data_type, unit, options, evidence, derived_from, nl,
                first_instrument, first_source_mkp, possible_duplicate_of, embedding, source_kind, derivation,
                concept, concept_embedding)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s::vector)
                ON CONFLICT (fact_id) DO NOTHING""")
                        .format(self.t(self.fact_t)),
                        (fid, name, entity, canon_type(rfield(r, "type")), get_field(r, "unit") or None, json.dumps(options),
                         json.dumps(evidence), json.dumps(derived), nl or None, instrument, source_mkp, dup, _vec(vector),
                         kind if kind in SOURCE_KINDS else None, get_field(r, "derivation") or None,
                         concept or None, _vec(cvector)))
            new += 1
            known[fid] = {"name": name, "type": canon_type(rfield(r, "type"))}
        # edges atom -> fact; earlier AKP versions of this MKP are no longer the latest
        cur.execute(sql.SQL("UPDATE {} SET is_latest=false WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND is_latest")
                    .format(self.t(self.edge_t)), (instrument, version, source_mkp))
        edges = 0
        for r in rows:
            if r["TYPE"] not in ("A", "A_REF"):        # an A_REF (SAME) atom still tests its facts
                continue
            for fid in dict.fromkeys(r.get("_facts") or field_list(r, "facts")):
                cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, akp_version, atom, fact_id, rule)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""").format(self.t(self.edge_t)),
                            (instrument, version, source_mkp, akp_version, r["ELEMENT"], fid, get_field(r, "rule") or None))
                edges += 1
        return new, edges


# ---------------------------------------------------------------- non-operative MKPs (no LLM)

AMEND_KIND = [(r"\bsub\w*?tuted\b|\bsubs\.", "SUBSTITUTED"), (r"\binserted\b|\bins\.", "INSERTED"),
              (r"\bomitted\b|\bomit\b", "OMITTED"), (r"\badded\b", "INSERTED"), (r"\bre-?numbered\b", "RENUMBERED"),
              (r"^\s*ibid\b", "IBID")]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
DATE = (r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})"
        r"|(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(january|february|march|april|may|june|july|august|september|october|november|december),?\s*(\d{4})")


def _iso(text: str) -> Optional[str]:
    m = re.search(DATE, text or "", re.I)
    if not m:
        return None
    if m.group(1):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), MONTHS[m.group(5).lower()], int(m.group(6))
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def time_fact(mkp_row: dict, instrument: str, source_mkp: str) -> dict:
    """An AMENDMENT MKP becomes a time fact on the provision it amends (not an atom).
    Footnote styles handled: 'Inserted vide Notf no. 35/2021-CT dt. 24.09.2021 w.e.f. 01.01.2022',
    'Inserted by The ... (Amendment) Act, 2018 (No. 31 of 2018) - Brought into force w.e.f. 01st February, 2019',
    'Substituted vide Notf No. 16/2019-CT dt.29.03.2019 wef 01.04.2019 for ―old wording‖', 'Omitted ...', 'Ibid.'"""
    text = get_field(mkp_row, "text")
    note = (text.split("states:", 1)[1] if "states:" in text else text).strip()
    kind = next((k for pat, k in AMEND_KIND if re.search(pat, note, re.I)), "AMENDED")
    by = (re.search(r"(?:\bvide|\bby)\s*((?:Notf|Notification|Order|Circular)\.?\s*no\.?\s*[\w/.-]+(?:\s*[–-]\s*CT)?)", note, re.I)
          or re.search(r"(?:\bvide|\bby)\s+((?:The\s+)?[A-Z][^–;]*?\bAct\b,?\s*\d{4}(?:\s*\(No\.\s*\d+\s+of\s+\d{4}\))?)", note))
    dated = re.search(r"\b(?:dt|dated)\.?\s*(" + DATE + ")", note, re.I)
    wef = re.search(r"(?:w\.?\s*e\.?\s*f\.?|with effect from)\s*(?:the\s+)?(" + DATE + ")", note, re.I)
    valid = get_field(mkp_row, "valid_from")
    valid = valid if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid or "") else None
    # effective_from: the w.e.f. date the footnote gives (a change can apply from an earlier date than the day it
    # was brought into force, e.g. s.7(3) w.e.f. 01.07.2017 by the 2018 Amendment Act); in_force_from: the day the
    # amending law was brought into force (MKP valid_from) when that is a different date (notes section 6)
    effective = (_iso(wef.group(1)) if wef else None) or valid
    in_force = valid if valid and valid != effective else None
    replaced = re.search(r"\bfor\s+(?:the\s+(?:words?|figures?|letters?|brackets?)[^―\"“‘']{0,40}?)?"
                         r"[―\"“‘']\s*(.+?)\s*[‖\"”’'](?=[\s,.;)]|$)", note, re.S)
    citation_key = get_field(mkp_row, "citation_key") or source_mkp.split("#", 1)[0]
    return {"time_fact_id": "T_" + source_mkp.replace("#", "_"), "target": citation_key, "kind": kind,
            "amended_by": re.sub(r"\s+", " ", by.group(1)).strip(" .") if by else None,
            "instrument_date": _iso(dated.group(1)) if dated else None,
            "effective_from": effective, "in_force_from": in_force,
            "replaced_text": replaced.group(1).strip() if replaced else None, "note": note}


def skip_rows(bundle: dict, knowledge_type: str, tfact: Optional[dict] = None) -> List[dict]:
    """MKP + SP (+ LK for amendments) + SUM rows for HEADING / AMENDMENT / METADATA MKPs - written by Python."""
    mkp_id, row = bundle["mkp_id"], bundle["mkp_row"]

    def q(t):
        return '"' + str(t or "").replace(";", ",").replace("|", "/").replace('"', '\\"') + '"'
    reason = {"HEADING": "Heading - names the subject, no testable condition",
              "AMENDMENT": "Amendment record - kept as a time fact on the amended provision",
              "METADATA": "Instrument metadata - no testable condition"}.get(knowledge_type, "Non-operative")
    rows = [{"MKP": mkp_id, "TYPE": "MKP", "ELEMENT": mkp_id, "CONCEPT": "NA",
             "RESULTS": (f"citation: {q(get_field(row, 'citation'))}; citation_key: {get_field(row, 'citation_key')}; "
                         f"part: {get_field(row, 'part')}; knowledge_type: {knowledge_type}; frame: {knowledge_type}; "
                         f"skip: {q(reason)}; nl: {q(get_field(row, 'text'))}")}]
    if tfact:
        # the link to the time fact: an abbreviated id like every element (TF = time fact); the time fact keeps its
        # citation key (T_CGSTA17_S7_SS1_Cb_AMD1) in the RESULTS, which joins it to akp_time_fact
        rows.append({"MKP": mkp_id, "TYPE": "LK", "ELEMENT": f"{mkp_id}_LK_AMENDS", "CONCEPT": "NA",
                     "RESULTS": (f"relation: AMENDS; target: {tfact['target']}; status: TIME_FACT; time_fact: {tfact['time_fact_id']}; "
                                 f"kind: {tfact['kind']}; effective_from: {tfact['effective_from'] or 'NA'}; "
                                 f"instrument_date: {tfact['instrument_date'] or 'NA'}; "
                                 f"amended_by: {q(tfact['amended_by'] or 'NA')}")})
    for sp in bundle["sp_rows"]:
        rows.append({"MKP": mkp_id, "TYPE": "SP", "ELEMENT": get_field(sp, "sp") or "SP1", "CONCEPT": "NA",
                     "RESULTS": f"role: FRAME; text: {q(get_field(sp, 'text'))}; elements: [{mkp_id}]"})
    rows.append({"MKP": mkp_id, "TYPE": "SUM", "ELEMENT": mkp_id, "CONCEPT": "NA",
                 "RESULTS": f"atoms: 0; facts: 0; skip: {q(reason)}"})
    return rows
