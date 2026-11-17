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
