"""Dialect shim so ledger.py can speak SQLite locally and Postgres in
production without maintaining two copies of every query.

===========================================================================
WHY A SHIM, NOT SQLALCHEMY, NOT AN ABSTRACT BASE CLASS
===========================================================================
The SQL in ledger.py is already close to dialect-neutral — COALESCE, SUM,
COUNT, GROUP BY mean the same thing in both engines. The real differences
are small and enumerable:

  1. Placeholder style: `?` (SQLite) vs `%s` (Postgres/psycopg).
  2. Auto-incrementing ids: `AUTOINCREMENT` vs `GENERATED ... AS IDENTITY`.
  3. Foreign-key declaration order. SQLite resolves FKs lazily; Postgres
     errors at CREATE TABLE time if the referenced table doesn't exist yet.

An abstract base class or a full ORM would mean rewriting every query twice,
in the one file where a rewrite bug means money is wrong. This module
translates (1) mechanically so every existing `conn.execute(sql, params)`
call site in ledger.py stays byte-identical; (2) and (3) are handled once,
in the schema definition itself.

The blind `?` -> `%s` replace in `_to_postgres_placeholders` is safe only
because no SQL string in ledger.py contains a literal `?` outside a
placeholder position — true by construction (every query in that file is
written by hand, not assembled from free text) and, more convincingly than
any static check of that claim would be, exercised end-to-end: CI runs the
entire test suite against a real Postgres service container, so a stray `?`
would surface as a syntax error there, not as a comment nobody re-derives.

===========================================================================
ON THE POSTGRES "SUM() RETURNS A DECIMAL" GOTCHA — AND WHY IT DOES NOT APPLY HERE
===========================================================================
It is a documented psycopg/Postgres trap that `SUM()` over a `bigint` column
returns `numeric`, which psycopg maps to Python's `Decimal` — not JSON
serialisable, so an endpoint returning it 500s in production while every
local SQLite-backed test stays green.

This project's money columns are declared `INTEGER`, not `BIGINT` — plenty
for a demo bounded by realistic test-mode volumes (₹21m before overflow).
Postgres's own type-promotion rules mean `SUM(integer)` returns `bigint`,
which psycopg maps to a native Python `int`, same as SQLite. So the trap
does not fire against this schema. It would if a column were ever widened to
`BIGINT` — worth remembering if that happens, not worth a defensive `CAST`
against a failure mode the schema does not have. The CI job that runs the
full suite against real Postgres (see .github/workflows/ci.yml) is the
actual proof of this, not this comment.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def dialect_for(dsn: str) -> str:
    """"postgres" or "sqlite", from what the connection string looks like.

    Args:
        dsn: A Postgres connection URL, or a SQLite file path.

    Returns:
        "postgres" if `dsn` looks like a Postgres URL, else "sqlite".
    """
    return "postgres" if dsn.startswith(POSTGRES_PREFIXES) else "sqlite"


class UniqueConstraintError(Exception):
    """A write violated a UNIQUE or PRIMARY KEY constraint, on either engine.

    `sqlite3.IntegrityError` and `psycopg.errors.UniqueViolation` are
    different exception hierarchies; `Conn.execute` catches both and raises
    this instead, so ledger.py has exactly one exception type to catch
    regardless of which database answered.
    """


def _to_postgres_placeholders(sql: str) -> str:
    """Translates SQLite's `?` placeholders to Postgres's `%s`.

    Safe only because no query string in this project embeds a literal `?`
    outside a placeholder position — see the module docstring.
    """
    return sql.replace("?", "%s")


def _split_sql_statements(script: str) -> list[str]:
    """Splits a `;`-separated DDL script into individual statements.

    psycopg has no `executescript` — unlike sqlite3, it expects one statement
    per `execute()` call. A naive split on `;` is fine for this project's DDL:
    no string literals or stored procedures contain an embedded semicolon.
    """
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class Conn:
    """One DB-API connection, SQLite or Postgres, behind one small interface.

    Every call site in ledger.py that does `conn.execute(sql, params)` or
    `conn.executemany(sql, seq)` continues to work unmodified against either
    engine; this class is where the difference actually lives.
    """

    def __init__(self, raw: Any, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Runs one statement, returning a cursor-like object.

        The returned object supports `.fetchone()`, `.fetchall()`, and
        `.rowcount` identically in both engines — psycopg's cursor and
        sqlite3's cursor already agree on that surface.

        Raises:
            UniqueConstraintError: On a UNIQUE/PRIMARY KEY violation, in
                place of the engine's own exception type.
        """
        if self.dialect == "postgres":
            cur = self._raw.cursor()
            try:
                cur.execute(_to_postgres_placeholders(sql), tuple(params))
            except psycopg.errors.UniqueViolation as exc:
                raise UniqueConstraintError(str(exc)) from exc
            return cur

        try:
            return self._raw.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            # sqlite3 has one exception class for every constraint kind
            # (UNIQUE, NOT NULL, CHECK, FOREIGN KEY...), unlike psycopg,
            # which only ever raises UniqueViolation here for a uniqueness
            # problem. Narrow the SQLite side by message so the two engines
            # translate the same *kind* of failure, not every failure.
            if "UNIQUE constraint failed" in str(exc):
                raise UniqueConstraintError(str(exc)) from exc
            raise

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        """Runs one statement over many parameter sets."""
        if self.dialect == "postgres":
            cur = self._raw.cursor()
            cur.executemany(_to_postgres_placeholders(sql), [tuple(p) for p in seq])
            return
        self._raw.executemany(sql, seq)

    def executescript(self, script: str) -> None:
        """Runs a `;`-separated DDL script. Used only for schema bootstrap."""
        if self.dialect == "postgres":
            cur = self._raw.cursor()
            for statement in _split_sql_statements(script):
                cur.execute(statement)
            return
        self._raw.executescript(script)


# ---------------------------------------------------------------------------
# Postgres connection pooling
# ---------------------------------------------------------------------------
#
# Keyed by DSN so tests that spin up their own Postgres connection string
# don't collide with the facilitator's own pool. In practice there is one
# DSN per process.
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


def _get_pool(dsn: str) -> ConnectionPool:
    """Returns (creating once) the process-wide pool for `dsn`.

    `prepare_threshold=None` disables psycopg's automatic query preparation.
    Supabase's connection pooler runs in transaction mode by default, which
    does not support prepared statements — without this, queries succeed
    individually but start failing after the fifth identical execution
    (psycopg's auto-prepare threshold), which is a confusing failure to hit
    without knowing to expect it.

    `min_size=0` so a cold start does not pay for connections it may not
    use; `max_size` stays small because a pooler's own client-connection
    limit is shared across every concurrent serverless instance.
    """
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=int(os.getenv("PG_POOL_MAX", "3")),
                kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": False},
                open=True,
                timeout=10,
            )
            _POOLS[dsn] = pool
        return pool


@contextmanager
def connect(dsn_or_path: str, dialect: str) -> Iterator[Conn]:
    """Yields a `Conn`, committing on success and rolling back on any error.

    For Postgres this checks a connection out of the process-wide pool and
    returns it on exit. For SQLite it opens and closes a plain file
    connection — unchanged from how this project has always worked.

    Args:
        dsn_or_path: Postgres connection URL, or a SQLite file path.
        dialect: "postgres" or "sqlite" — see `dialect_for`.
    """
    if dialect == "postgres":
        pool = _get_pool(dsn_or_path)
        with pool.connection() as raw:
            conn = Conn(raw, dialect)
            try:
                yield conn
                raw.commit()
            except Exception:
                raw.rollback()
                raise
    else:
        raw = sqlite3.connect(dsn_or_path, timeout=10.0)
        raw.row_factory = sqlite3.Row
        # Enforce the foreign keys declared in the schema; SQLite ignores
        # them by default.
        raw.execute("PRAGMA foreign_keys = ON")
        conn = Conn(raw, dialect)
        try:
            yield conn
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
