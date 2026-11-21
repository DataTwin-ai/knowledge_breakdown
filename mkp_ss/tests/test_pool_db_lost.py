"""Worker pool: a dropped database connection (no database, no model).

Real case 2026-11-16: sections 12 and 13 waited minutes for the model, the network dropped the idle MKP connection
('server closed the connection unexpectedly'), and every later job of that worker failed with 'connection already
closed'. Now the connection sends keepalive signals, a lost connection puts the unit back in the queue, and the next
try opens a new connection.
"""
import sys
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import db_insert  # noqa: E402
from db_insert import is_connection_lost, keepalive_options  # noqa: E402


def test_keepalive_options_come_from_settings():
    o = keepalive_options()
    assert o["keepalives"] == 1
    assert {"keepalives_idle", "keepalives_interval", "keepalives_count"} <= set(o)


def test_keepalive_missing_key_names_it(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    f.write_text('{"database": {"keepalive_idle_seconds": 30}}', encoding="utf-8")
    import kp_settings
    monkeypatch.setattr(kp_settings, "resolve", lambda v: f)
    with pytest.raises(KeyError, match="database.keepalive_interval_seconds"):
        keepalive_options()


@pytest.mark.parametrize("exc, lost", [
    (psycopg2.OperationalError("server closed the connection unexpectedly"), True),
    (psycopg2.InterfaceError("connection already closed"), True),
    (psycopg2.OperationalError("SSL SYSCALL error: EOF detected"), True),
    (psycopg2.errors.SyntaxError("syntax error at or near"), False),
    (RuntimeError("comparison FALSE"), False),
])
def test_connection_lost_is_told_apart_from_sql_errors(exc, lost):
    assert is_connection_lost(exc) is lost


def test_is_alive_skips_objects_that_are_not_postgres():
    assert db_insert.is_alive(None) is True
    assert db_insert.is_alive(object()) is True


class _Pool:
    """Only what KpPool._lost and _drop_run use."""
    def __init__(self):
        import threading
        from kp_pool import Pool as KpPool
        self._lost = KpPool._lost.__get__(self)
        self._drop_run = KpPool._drop_run.__get__(self)
        self.requeue_limit, self.total_jobs, self.cv, self.added = 3, 5, threading.Condition(), []

    def add(self, jobs, front=False):
        self.added.extend(jobs)


class _Job:
    code, name, requeues, requeued = "CGSTA17", "CGSTA17_S12_SS5_Cb", 0, False


class _Tot:
    jobs = 5


def test_lost_connection_requeues_the_unit():
    p, job, tot = _Pool(), _Job(), _Tot()
    assert p._lost("W6", job, tot, psycopg2.InterfaceError("connection already closed"), {}, []) is True
    assert job.requeued and job.requeues == 1 and p.added == [job] and tot.jobs == 4


def test_other_errors_and_the_limit_do_not_requeue():
    p, job, tot = _Pool(), _Job(), _Tot()
    assert p._lost("W6", job, tot, RuntimeError("x"), {}, []) is False
    job.requeues = 3
    assert p._lost("W6", job, tot, psycopg2.InterfaceError("connection already closed"), {}, []) is False
    assert p.added == []


def test_drop_run_writes_excel_and_closes_the_connection():
    class Conn:
        closed = False
        def close(self):
            self.closed = True

    class Store:
        conn = Conn()

    class Run:
        store, flushed = Store(), False
        def flush_excel(self):
            Run.flushed = True

    runs, conns = {"CGSTA17": Run()}, [Store.conn]
    _Pool()._drop_run("CGSTA17", runs, conns)
    assert runs == {} and conns == [] and Run.flushed and Store.conn.closed


# ---- bug list D17: the database cannot be reached at all (17 Nov 19:31, 'Network is unreachable')

DOWN = psycopg2.OperationalError('connection to server at "1.2.3.4", port 5432 failed: Network is unreachable')


def test_unreachable_is_told_apart_from_a_dropped_connection():
    assert db_insert.is_db_unreachable(DOWN)
    assert not db_insert.is_db_unreachable(psycopg2.InterfaceError("connection already closed"))
    assert db_insert.is_connection_lost(RuntimeError("GEMINI gemini-2.5-pro failed: connection already closed"))


def test_wait_for_db_tries_until_the_database_answers():
    calls, slept, notes = [], [], []

    class C:
        def close(self):
            pass

    def connect():
        calls.append(1)
        if len(calls) < 3:
            raise DOWN
        return C()
    assert db_insert.wait_for_db(600, 30, notes.append, connect=connect, sleep=slept.append) is True
    assert slept == [30, 30] and any("reachable again after 60 s" in n for n in notes)


def test_wait_for_db_gives_up_after_the_limit():
    def connect():
        raise DOWN
    assert db_insert.wait_for_db(60, 30, connect=connect, sleep=lambda _s: None) is False


def test_unreachable_database_waits_then_requeues(monkeypatch):
    waited = []
    monkeypatch.setattr(db_insert, "wait_for_db", lambda total, every, note=None: waited.append((total, every)) or True)
    monkeypatch.setattr(db_insert, "unreachable_wait", lambda: (600, 30))
    p, job, tot = _Pool(), _Job(), _Tot()
    assert p._lost("W6", job, tot, DOWN, {}, []) is True
    assert waited == [(600, 30)] and p.added == [job]


def test_still_unreachable_after_the_wait_fails_as_before(monkeypatch):
    monkeypatch.setattr(db_insert, "wait_for_db", lambda total, every, note=None: False)
    monkeypatch.setattr(db_insert, "unreachable_wait", lambda: (600, 30))
    p, job, tot = _Pool(), _Job(), _Tot()
    assert p._lost("W6", job, tot, DOWN, {}, []) is False and p.added == []


def test_unreachable_settings_come_from_config():
    total, every = db_insert.unreachable_wait()
    assert total >= every >= 1


# ---- 2026-11-20: Windows 'Connection timed out (0x0000274C/10060)' skipped the whole references step

def test_windows_connection_timeout_counts_as_unreachable():
    exc = psycopg2.OperationalError('connection to server at "32.201.165.32", port 5432 failed: '
                                    'Connection timed out (0x0000274C/10060)')
    assert db_insert.is_db_unreachable(exc)


def test_references_step_waits_and_runs_once_more(monkeypatch):
    import main
    calls, flows = [], []
    DOWN10060 = psycopg2.OperationalError('connection to server failed: Connection timed out (0x0000274C/10060)')

    def refs(*a):
        calls.append(1)
        if len(calls) == 1:
            raise DOWN10060
    monkeypatch.setattr(main, "_references", refs)
    monkeypatch.setattr(main, "flow", flows.append)
    monkeypatch.setattr(db_insert, "unreachable_wait", lambda: (60, 30))
    monkeypatch.setattr(db_insert, "wait_for_db", lambda total, every, note=None: True)
    main._references_waiting({}, {}, {}, {}, {})
    assert len(calls) == 2 and any("waiting, then once more" in f for f in flows)


def test_references_step_other_errors_do_not_wait(monkeypatch):
    import main
    calls, flows = [], []
    monkeypatch.setattr(main, "_references", lambda *a: calls.append(1) or (_ for _ in ()).throw(KeyError("x")))
    monkeypatch.setattr(main, "flow", flows.append)
    main._references_waiting({}, {}, {}, {}, {})
    assert len(calls) == 1 and any("not done" in f for f in flows)
