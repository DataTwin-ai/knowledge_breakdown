"""db_check.py - quick database check before a full run (no tables are created or changed).

    python db_check.py

Reads Credentials/.env (LEGAL_DB_HOST / PORT / NAME / USER / PASSWORD / SCHEMA), then checks, step by step,
what main.py needs: server reachable, login, database, schema, owner rights, CREATE rights, pgvector,
connection limit, key tables. Each failed step prints the issue and what to do. The password is never printed.
"""
import os
import socket
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / "Credentials" / ".env"
KEY_TABLES = ["ikp_versions", "mkp", "akp", "akp_cache", "akp_new", "akp_cache_new", "gkp_new"]
problems = []


def ok(msg):
    print(f"  OK    {msg}")


def bad(msg, fix=""):
    print(f"  FAIL  {msg}" + (f"\n        fix: {fix}" if fix else ""))
    problems.append(msg)


def warn(msg, fix=""):
    print(f"  WARN  {msg}" + (f"\n        fix: {fix}" if fix else ""))


def read_env():
    print("1. CREDENTIALS (Credentials/.env)")
    if not ENV_FILE.is_file():
        bad(f"{ENV_FILE} not found")
        return None
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("LEGAL_DB_") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("LEGAL_DB_HOST", "LEGAL_DB_PORT", "LEGAL_DB_NAME", "LEGAL_DB_USER", "LEGAL_DB_PASSWORD", "LEGAL_DB_SCHEMA"):
        if not env.get(k):
            bad(f"{k} missing or empty")
        elif k == "LEGAL_DB_PASSWORD":
            p = env[k]
            ok(f"{k} = {p[0]}{'*' * (len(p) - 2)}{p[-1]} ({len(p)} characters)")
        else:
            ok(f"{k} = {env[k]}")
    return env if not problems else None


def reach(host, port):
    print(f"\n2. SERVER {host}:{port}")
    try:
        socket.create_connection((host, port), timeout=8).close()
        ok("server reachable")
        return True
    except socket.timeout:
        bad("no answer (timed out)", "server down, or firewall / allowed-IP list blocks your current IP")
    except ConnectionRefusedError:
        bad("connection refused", "PostgreSQL not running on that port, or the port is closed")
    except OSError as e:
        bad(f"cannot reach: {e}", "check the host IP and your internet")
    return False


def login(env):
    print(f"\n3. LOGIN as {env['LEGAL_DB_USER']} to database {env['LEGAL_DB_NAME']}")
    try:
        import psycopg2
    except ImportError:
        bad("psycopg2 not installed", "pip install psycopg2-binary")
        return None
    try:
        conn = psycopg2.connect(host=env["LEGAL_DB_HOST"], port=int(env["LEGAL_DB_PORT"]),
                                dbname=env["LEGAL_DB_NAME"], user=env["LEGAL_DB_USER"],
                                password=env["LEGAL_DB_PASSWORD"], connect_timeout=10)
        conn.autocommit = True
        ok("login works")
        return conn
    except Exception as e:
        msg = str(e).strip().splitlines()[-1] if str(e).strip() else repr(e)
        low = msg.lower()
        if "password authentication failed" in low:
            bad(msg, "wrong password for this user (each user has its own password)")
        elif "does not exist" in low and "database" in low:
            bad(msg, "wrong LEGAL_DB_NAME - check the database name under the server in pgAdmin")
        elif "too many connections" in low:
            bad(msg, "close pgAdmin tabs / other runs, or ask the admin to raise the role's CONNECTION LIMIT")
        elif "no pg_hba.conf entry" in low:
            bad(msg, "the server does not allow your IP / user - ask the admin to add it in pg_hba.conf")
        else:
            bad(msg)
        return None


def q(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def details(conn, env):
    schema, user = env["LEGAL_DB_SCHEMA"], env["LEGAL_DB_USER"]
    cur = conn.cursor()

    print("\n4. WHO / WHERE")
    db, me, superuser = q(cur, "SELECT current_database(), current_user, "
                               "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")[0]
    ok(f"database {db}, user {me}, superuser {superuser}")
    dbs = [r[0] for r in q(cur, "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
    ok(f"databases on this server: {', '.join(dbs)}")

    print(f"\n5. SCHEMA {schema}")
    row = q(cur, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (schema,))
    if not row:
        warn(f"schema {schema} does not exist in database {db} - a run starts EMPTY here (no cache, no old data)",
             "if your data is in another database, set LEGAL_DB_NAME to it")
    else:
        ok(f"schema exists, owner {row[0][0]}")
        n = q(cur, "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s", (schema,))[0][0]
        (ok if n else warn)(f"{n} table(s) / view(s) in it")
        present = {r[0] for r in q(cur, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))}
        missing = [t for t in KEY_TABLES if t not in present]
        if n:
            (warn if missing else ok)("key tables " + ("missing: " + ", ".join(missing) if missing else "all present"))
        for name in ("ikp_versions", "akp_new"):
            if name in present:
                c = q(cur, f'SELECT count(*) FROM "{schema}"."{name}"')[0][0]
                ok(f"{name}: {c} row(s)")
        # owner rights: main.py drops / recreates views and triggers at start-up
        others = q(cur, """SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c
                           JOIN pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname = %s AND c.relkind IN ('r','v','m','S')
                             AND NOT pg_has_role(current_user, c.relowner, 'USAGE')""", (schema,))
        if others and not superuser:
            bad(f"tables / views owned by {', '.join(r[0] for r in others)} - {me} cannot rebuild them "
                f"('must be owner of view ...')", f"the owner runs: GRANT {others[0][0]} TO {me};")
        else:
            ok("owner rights on all tables / views")

    print("\n6. RIGHTS")
    if superuser or q(cur, "SELECT has_database_privilege(current_database(), 'CREATE')")[0][0]:
        ok(f"may create schemas in {db}")
    else:
        bad(f"no CREATE right on database {db} ('permission denied for database {db}')",
            f"the database owner runs: GRANT CREATE ON DATABASE {db} TO {me};")

    print("\n7. PGVECTOR")
    ext = q(cur, "SELECT extnamespace::regnamespace::text, extversion FROM pg_extension WHERE extname = 'vector'")
    if not ext:
        bad(f"pgvector not installed in database {db}", "a superuser runs: CREATE EXTENSION vector;")
    else:
        ok(f"installed in schema {ext[0][0]} (version {ext[0][1]})")
        path = q(cur, "SHOW search_path")[0][0]
        try:
            cur.execute("SELECT '[1,2]'::vector")
            ok(f"type vector usable (search_path: {path})")
        except Exception:
            bad(f'type "vector" does not exist for {me} (search_path: {path}, vector is in {ext[0][0]})',
                f"a superuser runs: ALTER EXTENSION vector SET SCHEMA public;  or  "
                f"ALTER ROLE {me} IN DATABASE {db} SET search_path = \"$user\", public, {ext[0][0]};")

    print("\n8. CONNECTIONS")
    limit = q(cur, "SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user")[0][0]
    used = q(cur, "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user")[0][0]
    workers = None
    try:
        import json
        workers = json.loads((ENV_FILE.parent.parent / "permission.json").read_text(encoding="utf-8"))["workers"]["count"]
    except Exception:
        pass
    need = (workers or 0) + 3
    if limit == -1:
        ok(f"no connection limit ({used} open now)")
    elif workers and limit - used + 1 < need:
        bad(f"limit {limit}, {used} open now, a run needs about {need} (workers {workers} + 3)",
            f"lower permission.json workers.count, close pgAdmin tabs, or: ALTER ROLE {me} CONNECTION LIMIT 30;")
    else:
        ok(f"limit {limit}, {used} open now" + (f", a run needs about {need}" if workers else ""))
    conn.close()


def main():
    print("DB CHECK\n" + "=" * 60)
    env = read_env()
    if env and reach(env["LEGAL_DB_HOST"], int(env["LEGAL_DB_PORT"])):
        conn = login(env)
        if conn:
            try:
                details(conn, env)
            except Exception as e:
                bad(f"check stopped: {str(e).strip()}")
    print("\n" + "=" * 60)
    if problems:
        print(f"RESULT: {len(problems)} problem(s) - fix them before python main.py")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("RESULT: OK - main.py can run with these credentials")


if __name__ == "__main__":
    main()
