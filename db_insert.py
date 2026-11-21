"""PostgreSQL connection for every LAW ARC step (settings in Credentials/.env).

LEGAL_DB_HOST / LEGAL_DB_PORT may default (localhost / 5432); LEGAL_DB_NAME, LEGAL_DB_USER and LEGAL_DB_PASSWORD
must be set. The first connection of a run also upgrades old table names (db_migrate.py).
"""
from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv


def load_project_environment() -> None:
	from kp_settings import build_paths
	try:
		env_file = build_paths()["env_file"]
	except Exception:
		env_file = None
	if env_file is not None and env_file.exists():
		load_dotenv(env_file, override=False)
	else:
		load_dotenv(override=False)


def get_db_config() -> dict:
	def req(key: str) -> str:
		value = os.getenv(key)
		if not value:
			raise KeyError(f"Required environment variable '{key}' not set in Credentials/.env or the environment")
		return value
	return {
		"host": os.getenv("LEGAL_DB_HOST", "localhost"),
		"port": int(os.getenv("LEGAL_DB_PORT", "5432")),
		"dbname": req("LEGAL_DB_NAME"),
		"user": req("LEGAL_DB_USER"),
		"password": req("LEGAL_DB_PASSWORD"),
	}


def get_db_schema() -> str:
	# Credentials/.env first: a step may ask for the schema before its first connection (IKP did, and wrote to public)
	load_project_environment()
	return os.getenv("LEGAL_DB_SCHEMA", "public")


def keepalive_options() -> dict:
	"""TCP keepalive from Config/settings.json `database` (a worker's connection waits minutes for a model answer;
	without keepalive the network dropped it as idle: 'server closed the connection unexpectedly')."""
	import json
	from kp_settings import resolve
	db = json.loads(resolve("Config/settings.json").read_text(encoding="utf-8-sig")).get("database")
	if not isinstance(db, dict):
		raise KeyError("Config/settings.json database is missing")
	out = {"keepalives": 1}
	for key, opt in (("keepalive_idle_seconds", "keepalives_idle"), ("keepalive_interval_seconds", "keepalives_interval"),
	                 ("keepalive_count", "keepalives_count")):
		if key not in db:
			raise KeyError(f"Config/settings.json database.{key} is missing")
		out[opt] = int(db[key])
	return out


def is_alive(conn) -> bool:
	"""False when a PostgreSQL connection was closed or the server no longer answers (checked with SELECT 1)."""
	if conn is None:
		return True                     # nothing to check (a test run without a database)
	if not isinstance(conn, psycopg2.extensions.connection):
		return True
	if conn.closed:
		return False
	try:
		with conn.cursor() as cur:
			cur.execute("SELECT 1")
		conn.rollback()
		return True
	except Exception:
		return False


_LOST = ("server closed the connection", "connection already closed", "terminating connection",
         "could not receive data", "connection timed out", "ssl syscall")
_DOWN = ("network is unreachable", "connection refused", "could not translate host name", "no route to host",
         "timeout expired", "could not connect to server", "connection timed out")   # 2026-11-20: Windows 10060


def is_connection_lost(exc) -> bool:
	"""The error of a dropped connection (not a wrong SQL statement). A model call that failed because its cache
	could not be saved (RuntimeError '... failed: connection already closed') counts too."""
	if isinstance(exc, psycopg2.InterfaceError):
		return True
	text = str(exc).lower()
	if isinstance(exc, psycopg2.OperationalError):
		return any(t in text for t in _LOST)
	return isinstance(exc, RuntimeError) and any(t in text for t in _LOST)


def is_db_unreachable(exc) -> bool:
	"""The database cannot be reached at all (network down, laptop offline, server down) - bug list D17: on
	17 Nov at 19:31 'Network is unreachable' failed 100 queued laws in 2 minutes."""
	text = str(exc).lower()
	return isinstance(exc, (psycopg2.OperationalError, RuntimeError)) and any(t in text for t in _DOWN)


def unreachable_wait() -> tuple:
	"""(seconds to wait in all, seconds between tries) from Config/settings.json `database`."""
	import json
	from kp_settings import resolve
	db = json.loads(resolve("Config/settings.json").read_text(encoding="utf-8-sig")).get("database") or {}
	for key in ("unreachable_wait_seconds", "unreachable_retry_seconds"):
		if key not in db:
			raise KeyError(f"Config/settings.json database.{key} is missing")
	return int(db["unreachable_wait_seconds"]), max(1, int(db["unreachable_retry_seconds"]))


def wait_for_db(total: int, every: int, note=lambda _m: None, connect=None, sleep=None) -> bool:
	"""Try to connect every `every` seconds for up to `total` seconds. True as soon as the database answers."""
	import time
	connect = connect or get_connection
	sleep = sleep or time.sleep
	waited = 0
	while waited <= total:
		try:
			c = connect()
			try:
				c.close()
			except Exception:
				pass
			if waited:
				note(f"DATABASE  : reachable again after {waited} s")
			return True
		except Exception as exc:
			if not is_db_unreachable(exc):
				return False
			if waited == 0:
				note(f"DATABASE  : cannot be reached - waiting up to {total // 60} min (trying every {every} s)")
		sleep(every)
		waited += every
	note(f"DATABASE  : still not reachable after {total // 60} min")
	return False


def get_connection():
	load_project_environment()
	conn = psycopg2.connect(**get_db_config(), **keepalive_options())
	from db_migrate import migrate
	migrate(conn, get_db_schema())
	return conn
