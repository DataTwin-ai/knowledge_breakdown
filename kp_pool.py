"""LAW ARC worker pool: many units at the same time, MKP -> AKP -> GKP each, no manual splitting.

    jobs   the chosen scope of every document, cut by mkp_ss/scope.jobs_for(): one job per section / rule /
           schedule paragraph / circular paragraph; a unit bigger than pool.max_job_chars is split at sub-sections,
           then clauses (its heading and lead-in become one small extra job). pool.max_job_chars = 0 splits every
           unit down to its smallest parts, so no worker waits behind one big section. Acts first, then Rules,
           notifications, circulars (law order inside each document).
    queue  every job waits in one queue. permission.json workers.count workers (W1, W2 ...) each take the next
           job as soon as they are free - whichever worker finishes first takes the next one.
    waves  base laws first (mkp_ss/waves.py, pool.waves): a worker takes a job of a later wave only when every
           job of the waves before it is finished, so the core facts exist before the other laws need them.
    job    MKP for the unit, then AKP for its MKPs, then GKP for its atoms (one database connection per worker).
    errors a unit that fails a step goes to Output/<DOC>/error_log.csv and the worker takes the next job.
           An MKP gate failure splits the unit into its parts (new jobs at the front of the queue).
           "Too many requests" puts the unit back at the end of the queue (pool.requeue_limit times).
    after  when the queue is empty: Excel files once, then the caller builds the governance masters once.

LLM calls of all workers together are limited per provider (workers.max_calls_at_once, llm_runtime.py).
"""
from __future__ import annotations

import threading
from collections import deque
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import kp_errors
import kp_usage
from kp_console import board_print, board_start, board_stop, board_tick, flow, log, set_worker, stage
from llm_runtime_errors import is_fatal, is_rate_limited


@dataclass
class Job:
    doc: dict                         # selected document (code, name, type, file)
    batch: object                     # mkp_ss.scope.Batch
    depth: int = 0                    # 1, 2, 3 = a part made after an MKP gate failure
    requeues: int = 0
    no: int = 0
    wave: int = 0                     # run wave (mkp_ss/waves.py): a wave starts when the waves before it are done
    requeued: bool = False            # this attempt was put back in the queue (not counted as finished)

    @property
    def code(self) -> str:
        return self.doc["code"]

    @property
    def name(self) -> str:
        return self.batch.root + (" (heading)" if getattr(self.batch, "only_keys", None) else "")


@dataclass
class DocTotals:
    jobs: int = 0
    done: int = 0
    failed: int = 0
    mkps: int = 0
    mkps_from_db: int = 0
    mkps_new: int = 0
    mkps_changed: int = 0
    atoms: int = 0
    unclassified: int = 0
    llm_mkp: int = 0
    llm_akp: int = 0
    llm_gkp: int = 0
    tsg: Dict[str, dict] = field(default_factory=dict)
    created: List[str] = field(default_factory=list)
    source_mkps: List[str] = field(default_factory=list)


class Pool:
    def __init__(self, cfg: dict, paths: dict, run: Dict[str, bool], workers: int,
                 settings: dict, fake_llm: Optional[Callable] = None, embed: Optional[Callable] = None,
                 connect: Optional[Callable] = None, akp_fake: Optional[Callable] = None,
                 gkp_fake: Optional[Callable] = None, akp_override: Optional[dict] = None,
                 gkp_override: Optional[dict] = None):
        self.cfg, self.paths, self.run_steps, self.n = cfg, paths, run, max(1, workers)
        self.mkp_settings = settings
        self.fake_llm, self.embed, self.akp_fake, self.gkp_fake = fake_llm, embed, akp_fake, gkp_fake
        self.akp_override = {"export_excel": False, **(akp_override or {})}
        self.gkp_override = {"export_excel": False, "taxonomy_synced": True, "coverage": False, **(gkp_override or {})}
        self.connect = connect
        pool_cfg = cfg.get("pool") or {}
        self.requeue_limit = int(pool_cfg.get("requeue_limit", 3))
        self.progress_every = float(pool_cfg.get("progress_every_seconds", 60))
        self.items: deque = deque()
        self.cv = threading.Condition()
        self.pending = 0                      # jobs added and not finished yet
        self.closing = False
        self.lock = threading.Lock()
        self.totals: Dict[str, DocTotals] = {}
        self.busy = 0
        self.finished = 0
        self.total_jobs = 0
        self.started = time.time()
        self.last_progress = time.time()
        self.counter = 0
        self.stopped = ""                     # why the run was stopped (no credit, wrong key ...)
        self.usage_rows: List[dict] = []      # one row per finished job (Output/usage.xlsx, sheet Jobs)
        from mkp_ss.waves import WavePlan
        self.waves = WavePlan(pool_cfg)       # base laws first (notes section 16)
        self.wave_pending: Dict[int, int] = {}
        self.wave_now = None
        kp_usage.reset()

    # ---------------------------------------------------------------- queue
    def add(self, jobs: List[Job], front: bool = False) -> None:
        """Queue jobs (front=True: the parts of a failed unit run before the rest)."""
        with self.cv:
            for j in jobs:
                self.counter += 1
                j.no = self.counter
                if not j.wave:
                    j.wave = self.waves.wave_of(j.batch.root, j.doc.get("type", ""))
                self.wave_pending[j.wave] = self.wave_pending.get(j.wave, 0) + 1
                self.total_jobs += 1
                self.totals.setdefault(j.code, DocTotals()).jobs += 1
            if front:
                self.items.extendleft(reversed(jobs))
            else:
                self.items.extend(jobs)
            self.pending += len(jobs)
            self.cv.notify_all()

    def _next(self) -> Optional[Job]:
        """The next job for a free worker; None when every job is finished. Only jobs of the lowest wave that still
        has work are handed out: a later wave waits until the earlier waves are done."""
        with self.cv:
            while True:
                if self.items:
                    now = min(w for w, n in self.wave_pending.items() if n > 0)
                    for i, j in enumerate(self.items):
                        if j.wave == now:
                            del self.items[i]
                            if self.wave_now != now:
                                self.wave_now = now
                                left = sum(1 for x in self.items if x.wave == now) + 1
                                flow(f"WAVE {now}    : {self.waves.name(now)} - {left} job(s)")
                            return j
                elif self.closing:
                    return None
                self.cv.wait()

    def _finish(self, counted: bool = True, wave: int = 0) -> None:
        with self.cv:
            if wave in self.wave_pending:
                self.wave_pending[wave] -= 1
            self.pending -= 1
            self.finished += 1 if counted else 0
            if self.pending <= 0:
                self.closing = True
            self.cv.notify_all()

    # ---------------------------------------------------------------- run
    def start(self) -> Dict[str, DocTotals]:
        import db_migrate
        self._prepare_tables()                # once, here: workers never create / alter tables (no deadlock)
        flow(f"WORKERS   : {self.n} started - {self.total_jobs} job(s) in the queue")
        with self.cv:
            if self.pending <= 0:
                self.closing = True
        db_migrate.POOL_READY = True
        names = [f"W{i + 1}" for i in range(self.n)]
        board_start(names, self._header)
        ticking = threading.Event()
        ticker = threading.Thread(target=self._tick, args=(ticking,), daemon=True)
        ticker.start()
        try:
            threads = [threading.Thread(target=self._worker, args=(w,), daemon=True) for w in names]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            ticking.set()
            ticker.join(timeout=3)
            board_stop()
            db_migrate.POOL_READY = False
        self._progress(force=True)
        if self.stopped:
            flow(f"STOPPED   : {self.stopped}")
            flow("            Fix it, then run  python main.py  again - finished units are kept, the rest go on.")
        return self.totals

    def _header(self) -> str:
        """The board's first line: bar, %, jobs, failed, time left."""
        with self.cv:
            done, total, busy = self.finished, max(self.total_jobs, 1), self.busy
        failed = sum(t.failed for t in self.totals.values())
        pct = int(100 * done / total)
        bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
        took = time.time() - self.started
        left = f" | about {took / done * (total - done) / 60:.0f} min left" if done and total > done else ""
        try:
            import llm_runtime
            caps = " | calls at once " + ", ".join(f"{p} {c}" for p, c in llm_runtime.calls_at_once().items())
        except Exception:
            caps = ""
        return f"PROGRESS  [{bar}] {pct:3d}%  {done}/{total} jobs | {busy} working | {failed} failed{left}{caps}"

    def _tick(self, stop: threading.Event) -> None:
        n = 0
        while not stop.wait(1.0):
            n += 1
            board_tick()                      # redraw in place every second
            if n % 15 == 0:
                board_print()                 # windows that cannot redraw: the board every 15 s

    def _prepare_tables(self) -> None:
        if self.run_steps.get("akp", True):
            from akp_engine import ensure_tables
            ensure_tables(self.akp_override)
        if self.run_steps.get("gkp", True):
            from gkp_engine import ensure_tables
            ensure_tables(self.gkp_override)

    def _stop(self, why: str) -> None:
        """No retry can help (no credit, wrong key): drop the waiting jobs, let running ones end."""
        with self.cv:
            if self.stopped:
                return
            self.stopped = why
            left = len(self.items)
            self.pending -= left
            for j in self.items:
                self.wave_pending[j.wave] -= 1
            self.items.clear()
            if self.pending <= 0:
                self.closing = True
            self.cv.notify_all()
        flow(f"STOPPING  : {why} - {left} waiting job(s) not started")

    def _worker(self, name: str) -> None:
        set_worker(name)
        runs: Dict[str, object] = {}          # one MkpRun (own connection) per document
        conns = []
        try:
            while True:
                stage("idle")
                job = self._next()
                if job is None:
                    stage("finished")
                    return
                with self.lock:
                    self.busy += 1
                job.requeued = False
                kp_usage.job_start()
                job.t0, job.status, job.atoms, job.groups = time.time(), "FAILED", 0, ""
                job.stage, job.error, job.mkps = "", "", 0
                try:
                    self._do(name, job, runs, conns)
                except Exception as exc:        # never let one unit stop a worker
                    self._failed(name, job, "RUN", exc)
                finally:
                    use = kp_usage.job_end()
                    if not job.requeued:
                        with self.lock:
                            self.usage_rows.append({"document": job.code, "unit": job.name, "worker": name,
                                                    "status": job.status, "seconds": time.time() - job.t0,
                                                    "usage": use, "atoms": job.atoms, "groups": job.groups,
                                                    "label": job.batch.label, "key": job.batch.root,
                                                    "stage": job.stage, "error": job.error, "mkps": job.mkps,
                                                    "document_name": job.doc.get("name", job.code),
                                                    "document_type": job.doc.get("type", "")})
                    for c in conns:             # a failed SQL statement must not poison the next job
                        try:
                            c.rollback()
                        except Exception:
                            pass
                    with self.lock:
                        self.busy -= 1
                    self._finish(counted=not job.requeued, wave=job.wave)
        finally:
            for r in runs.values():
                try:
                    r.flush_excel()
                except Exception:
                    pass
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass
            set_worker("")

    def _mkp_run(self, job: Job, runs: dict, conns: list):
        if job.code not in runs:
            import ikp_engine
            from mkp_engine import MkpRun
            conn = self.connect() if self.connect else ikp_engine.get_connection(self.paths)
            conns.append(conn)
            runs[job.code] = MkpRun(job.doc, self.cfg, self.paths, self.mkp_settings, conn, self.fake_llm, self.embed)
        return runs[job.code]

    def _do(self, name: str, job: Job, runs: dict, conns: list) -> None:
        from mkp_ss.check import GateError
        from mkp_ss.scope import Batch, label_of, selectable_children
        t0 = time.time()
        b = job.batch
        stage("starting", job=f"{job.code} {job.name.replace(job.code + '_', '')}")
        flow(f"[{name}] start  {job.code} {job.name:<34} {b.label[:60]}")
        tot = self.totals[job.code]
        run = None
        # ---- MKP
        if self.run_steps.get("mkp", True):
            kp_usage.set_step("MKP")
            run = self._mkp_run(job, runs, conns)
            try:
                r = run.run_unit(b)
            except Exception as exc:
                if is_rate_limited(exc) and job.requeues < self.requeue_limit:
                    self._requeue(name, job, tot)
                    return
                kids = selectable_children(run.idx, b.root) if not getattr(b, "only_keys", None) else []
                where = "MKP_GATE" if isinstance(exc, GateError) else "MKP"
                if kids and job.depth < 3 and isinstance(exc, GateError):
                    kp_errors.record(self.paths["output_dir"], job.code, b.root, where, exc, name, job.requeues,
                                     "SPLIT")
                    job.status, job.stage = "SPLIT", where
                    job.error = (f"MKP check failed - split into {len(kids)} part(s), each runs as its own job: "
                                 + " ".join(str(exc).split())[:300])
                    flow(f"[{name}] split  {job.code} {job.name} - MKP check failed, {len(kids)} part(s) queued first")
                    self.add([Job(job.doc, Batch(k, label_of(run.idx, k), k, True, False, b.cluster_id,
                                                 b.business_question), job.depth + 1, wave=job.wave) for k in kids],
                             front=True)
                    with self.lock:
                        tot.done += 1
                    return
                self._failed(name, job, where, exc)
                return
            tot.llm_mkp += r.get("llm_calls", 0)
            if r["status"] == "EMPTY":
                self._done(name, job, t0, "nothing to frame (heading / omitted text)")
                return
            with self.lock:
                tot.mkps += r.get("mkps", 0)
                job.mkps = r.get("mkps", 0)
                tot.mkps_from_db += r.get("cached", 0)
                tot.mkps_new += r.get("new", 0)
                tot.mkps_changed += r.get("changed", 0)
            for note in r.get("notes", []):
                log(f"NOTE : {note}")
        only = set(b.only_keys) if getattr(b, "only_keys", None) else None
        pdf = Path(self.paths["law_dir"]) / job.doc["file"]
        atoms, akp_failed = 0, []
        # ---- AKP
        if self.run_steps.get("akp", True):
            stage("AKP")
            kp_usage.set_step("AKP")
            from akp_engine import run_akp_scope
            try:
                session = run_akp_scope(pdf, job.code, scope_key=b.root, fake_llm=self.akp_fake, embed=self.embed,
                                        settings_override=self.akp_override, only_keys=only)
            except RuntimeError as exc:
                if "no approved Standard MKPs" in str(exc) or "No Standard MKPs found" in str(exc):
                    self._done(name, job, t0, "no operative MKP")
                    return
                if is_rate_limited(exc) and job.requeues < self.requeue_limit:
                    self._requeue(name, job, tot)
                    return
                self._failed(name, job, "AKP", exc)
                return
            c = getattr(session, "counts", {}) or {}
            atoms = c.get("atoms", 0)
            akp_failed = list(getattr(session, "failures", []) or [])
            with self.lock:
                tot.atoms += atoms
                tot.llm_akp += c.get("llm_calls", 0)
        # ---- GKP
        groups = ""
        if self.run_steps.get("gkp", True):
            stage("GKP")
            kp_usage.set_step("GKP")
            from gkp_engine import run_gkp_scope
            try:
                g = run_gkp_scope(job.code, scope_key=b.root, fake_llm=self.gkp_fake, embed=self.embed,
                                  settings_override=self.gkp_override, only_keys=only) or {}
            except RuntimeError as exc:
                if "No approved AKP rows" in str(exc):
                    g = {}
                elif is_rate_limited(exc) and job.requeues < self.requeue_limit:
                    self._requeue(name, job, tot)
                    return
                else:
                    self._failed(name, job, "GKP", exc)
                    return
            with self.lock:
                tot.llm_gkp += g.get("llm_calls", 0)
                tot.unclassified += g.get("unclassified", 0)
                for m in g.get("source_mkps", []):
                    if m not in tot.source_mkps:
                        tot.source_mkps.append(m)
                for tsg, v in (g.get("tsg_counts") or {}).items():
                    cur = tot.tsg.setdefault(tsg, {"name": v["name"], "atoms": 0})
                    cur["atoms"] += v["atoms"]
                for x in g.get("created", []):
                    if x not in tot.created:
                        tot.created.append(x)
            groups = ", ".join(sorted(g.get("tsg_counts") or {}))[:60]
            job.atoms, job.groups = atoms, groups
        if akp_failed:
            self._failed(name, job, "AKP", f"comparison FALSE for {', '.join(akp_failed)} (other MKPs of the unit saved)")
            return
        kp_errors.done(self.paths["output_dir"], job.code, b.root, name)
        self._done(name, job, t0, f"atoms {atoms}" + (f" | {groups}" if groups else ""))

    def _requeue(self, name: str, job: Job, tot: DocTotals) -> None:
        """Too many requests: the unit goes to the end of the queue (counted once)."""
        job.requeues += 1
        job.requeued = True
        flow(f"[{name}] retry  {job.code} {job.name} - too many requests, back in the queue "
             f"({job.requeues}/{self.requeue_limit})")
        with self.cv:
            self.total_jobs -= 1
            tot.jobs -= 1
        self.add([job])

    def _done(self, name: str, job: Job, t0: float, what: str) -> None:
        with self.lock:
            self.totals[job.code].done += 1
        job.status = "DONE"
        flow(f"[{name}] done   {job.code} {job.name:<34} {what}  {time.time() - t0:.0f}s | {kp_usage.current_text()}")

    def _failed(self, name: str, job: Job, stage: str, exc) -> None:
        kp_errors.record(self.paths["output_dir"], job.code, job.batch.root, stage, exc, name, job.requeues)
        job.status, job.stage = "FAILED", stage
        job.error = " ".join(f"{type(exc).__name__}: {exc}".split())[:500] if isinstance(exc, BaseException) else \
            " ".join(str(exc).split())[:500]
        if isinstance(exc, BaseException) and is_fatal(exc):
            text = str(exc)
            why = ("LLM credit balance too low - top up the account" if "credit" in text.lower()
                   else "LLM key or billing problem - check Credentials/")
            self._stop(f"{why} ({' '.join(text.split())[:120]})")
        with self.lock:
            self.totals[job.code].failed += 1
        text = " ".join(str(exc).split())[:140]
        flow(f"[{name}] FAILED {job.code} {job.name} at {stage} - {text} (error log; next job goes on)")

    def _progress(self, force: bool = False) -> None:
        """The final progress line (while running, the board shows it live)."""
        took = time.time() - self.started
        flow(self._header().replace("PROGRESS ", "FINISHED ") + f" | {took / 60:.1f} min")

