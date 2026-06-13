"""Database operations — termind v2.0 core.

Local-first, security-first DB access. SQLite is fully supported with ZERO dependencies
(Python's stdlib `sqlite3`), which is the right default for a private, isolated agent.
Postgres / MySQL / MongoDB are first-class too, but their drivers are third-party — so termind
installs them into its ISOLATED workspace venv (never system-wide) and, if a driver isn't there
yet, it hands you the exact install step instead of pretending to connect.

Safety model (every engine):
  • reads (SELECT/EXPLAIN/PRAGMA) run freely;
  • writes + schema changes (INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE…) are DESTRUCTIVE —
    termind shows an EXPLAIN plan and the EXACT affected-row count (run in a savepoint and rolled
    back) and refuses to execute until the caller passes explicit consent.
Every query is meant to be sealed into the audit ledger by the caller.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3

# engine → the pip package that provides its driver (sqlite needs none — it's stdlib)
DRIVERS = {"postgres": "psycopg2", "mysql": "pymysql", "mongodb": "pymongo"}
ENGINES = ("sqlite",) + tuple(DRIVERS)

# a statement that changes data or schema → needs preview + consent before it runs
_DESTRUCTIVE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|replace|create|merge|grant|revoke)\b", re.I)


class DriverMissing(Exception):
    """Raised when an engine's third-party driver isn't installed in the workspace venv."""

    def __init__(self, engine: str):
        self.engine = engine
        self.module = DRIVERS.get(engine, "?")
        super().__init__(f"{engine} driver '{self.module}' is not installed")


def engines_available() -> dict:
    """{engine: usable_now?} — sqlite is always True; others depend on the installed driver."""
    out = {"sqlite": True}
    for eng, mod in DRIVERS.items():
        out[eng] = importlib.util.find_spec(mod.split(".")[0]) is not None
    return out


def parse_dsn(spec: str):
    """('sqlite', path) | ('postgres'|'mysql'|'mongodb', dsn). A bare path/':memory:' = sqlite."""
    spec = (spec or "").strip()
    m = re.match(r"^(\w+)://(.*)$", spec)
    if m:
        scheme, rest = m.group(1).lower(), m.group(2)
        if scheme in ("sqlite", "file"):
            return "sqlite", rest or ":memory:"
        if scheme in ("postgres", "postgresql"):
            return "postgres", spec
        if scheme == "mysql":
            return "mysql", spec
        if scheme in ("mongodb", "mongodb+srv"):
            return "mongodb", spec
    # no scheme → treat as a sqlite file path (or in-memory)
    return "sqlite", spec or ":memory:"


def is_destructive(sql: str) -> bool:
    return bool(_DESTRUCTIVE.search(sql or ""))


class Database:
    """One connection to one database. sqlite uses the stdlib; others lazy-import their driver."""

    def __init__(self, name: str, spec: str):
        self.name = name
        self.spec = spec
        self.engine, self.target = parse_dsn(spec)
        self._con = None

    # ── connection ───────────────────────────────────────────────────────────
    def connect(self):
        if self._con is not None:
            return self._con
        if self.engine == "sqlite":
            self._con = sqlite3.connect(self.target)
        else:
            mod = DRIVERS[self.engine]
            if importlib.util.find_spec(mod.split(".")[0]) is None:
                raise DriverMissing(self.engine)
            self._con = self._connect_driver(mod)
        return self._con

    def _connect_driver(self, mod: str):
        if self.engine == "postgres":
            import psycopg2
            return psycopg2.connect(self.spec)
        if self.engine == "mysql":
            import pymysql
            return pymysql.connect(**_mysql_params(self.spec))
        if self.engine == "mongodb":
            import pymongo
            return pymongo.MongoClient(self.spec)
        raise DriverMissing(self.engine)  # unreachable

    def close(self):
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    # ── introspection ────────────────────────────────────────────────────────
    def tables(self) -> list:
        con = self.connect()
        if self.engine == "sqlite":
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            return [r[0] for r in rows]
        if self.engine == "mongodb":
            return sorted(con.get_default_database().list_collection_names())
        cur = con.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema','sys','mysql') "
                    "ORDER BY table_name")
        return [r[0] for r in cur.fetchall()]

    def schema(self, table: str = None) -> dict:
        """{table: [(column, type), …]} for one table or all (sqlite/SQL engines)."""
        con = self.connect()
        names = [table] if table else self.tables()
        out = {}
        if self.engine == "sqlite":
            for t in names:
                cols = con.execute(f"PRAGMA table_info({t})").fetchall()
                out[t] = [(c[1], c[2] or "?") for c in cols]
            return out
        if self.engine == "mongodb":
            db = con.get_default_database()
            for t in names:
                doc = db[t].find_one() or {}
                out[t] = [(k, type(v).__name__) for k, v in doc.items()]
            return out
        cur = con.cursor()
        for t in names:
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = %s ORDER BY ordinal_position", (t,))
            out[t] = [(c[0], c[1]) for c in cur.fetchall()]
        return out

    # ── reads ────────────────────────────────────────────────────────────────
    def run(self, sql: str, max_rows: int = 200) -> dict:
        """Execute a read and return {columns, rows, truncated}. (Caller should gate writes.)"""
        con = self.connect()
        cur = con.cursor()
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]],
                "truncated": truncated}

    def verify(self, sql: str) -> dict:
        """Syntax-check WITHOUT running it (sqlite compiles via EXPLAIN). {ok, error}."""
        if self.engine != "sqlite":
            return {"ok": True, "error": None, "note": "compile-check is exact only for sqlite"}
        try:
            self.connect().execute("EXPLAIN " + sql)   # compiles to bytecode, does not execute
            return {"ok": True, "error": None}
        except sqlite3.Error as e:
            return {"ok": False, "error": str(e)}

    def preview(self, sql: str) -> dict:
        """Impact preview BEFORE a destructive op: query plan + EXACT affected rows (run in a
        savepoint, then rolled back so nothing changes). sqlite is exact; others are advisory."""
        info = {"engine": self.engine, "kind": "write" if is_destructive(sql) else "read",
                "sql": sql, "plan": [], "affected": None}
        if self.engine != "sqlite":
            info["note"] = (f"exact preview needs sqlite; review this {self.engine} statement "
                            "carefully before confirming")
            return info
        con = self.connect()
        try:
            info["plan"] = [r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]
        except sqlite3.Error as e:
            info["error"] = str(e)
        if info["kind"] == "write":
            cur = con.cursor()
            try:
                cur.execute("SAVEPOINT _ttm_preview")
                info["affected"] = cur.execute(sql).rowcount   # -1 for pure DDL
            except sqlite3.Error as e:
                info["error"] = str(e)
            finally:
                try:
                    cur.execute("ROLLBACK TO _ttm_preview")
                    cur.execute("RELEASE _ttm_preview")
                except sqlite3.Error:
                    pass
        return info

    def execute(self, sql: str) -> dict:
        """Run a write FOR REAL and commit. Caller MUST have obtained consent first."""
        con = self.connect()
        cur = con.cursor()
        cur.execute(sql)
        con.commit()
        return {"affected": cur.rowcount, "committed": True}


def _mysql_params(spec: str) -> dict:
    m = re.match(r"mysql://(?:([^:@]+)(?::([^@]*))?@)?([^:/]+)(?::(\d+))?/(\w+)", spec)
    if not m:
        return {"host": "localhost", "database": spec.rsplit("/", 1)[-1]}
    user, pw, host, port, db = m.groups()
    p = {"host": host or "localhost", "database": db}
    if user:
        p["user"] = user
    if pw:
        p["password"] = pw
    if port:
        p["port"] = int(port)
    return p
