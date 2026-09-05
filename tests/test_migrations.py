"""Migration integrity — the things Postgres would reject and SQLite would not.

This suite runs on SQLite by default, and SQLite is forgiving in exactly the
places that matter here: it resolves foreign keys lazily, so a table can
reference one declared later; it never sees the generated Postgres DDL at all.
Both of those have already caused a real failure in this project, discovered
only when CI ran against a real Postgres container.

So these tests read the SQL as text and check the properties Postgres cares
about, without needing a Postgres to be running. They are not a replacement for
the Postgres CI job — they are the part of it that can run anywhere, and they
fail in seconds instead of after a container boots.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "facilitator" / "migrations"

pytestmark = pytest.mark.secure_defaults


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def declared_and_referenced(sql: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Returns tables declared in order, and (table, referenced_table) pairs."""
    declared: list[str] = []
    references: list[tuple[str, str]] = []
    current = None

    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue

        created = re.match(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", stripped, re.IGNORECASE
        )
        if created:
            current = created.group(1).lower()
            declared.append(current)
            continue

        referenced = re.search(r"REFERENCES\s+(\w+)", stripped, re.IGNORECASE)
        if referenced and current:
            references.append((current, referenced.group(1).lower()))

    return declared, references


class TestForeignKeyOrdering:
    """Postgres resolves foreign keys at CREATE TABLE time. SQLite does not."""

    def test_generated_schema_declares_every_table_before_it_is_referenced(self):
        """The bug this catches has happened twice in this repository.

        `commitments` referenced `batches` before it was declared, and the
        Phase 2 identity tables referenced `operators`. Both worked perfectly
        on SQLite and errored on `CREATE TABLE` under Postgres — a failure that
        only appears in CI, or worse, on a first production deploy.
        """
        import sys

        sys.path.insert(0, str(REPO_ROOT / "facilitator"))
        from ledger import schema_sql

        declared, references = declared_and_referenced(schema_sql("postgres"))
        position = {name: index for index, name in enumerate(declared)}

        out_of_order = [
            (table, target)
            for table, target in references
            # A self-reference is fine — `journal_transactions.reverses_txn_id`
            # points at its own table, which Postgres accepts.
            if target != table
            and target in position
            and position[target] > position[table]
        ]

        assert not out_of_order, (
            "These tables reference a table declared AFTER them. Postgres will refuse "
            "the CREATE TABLE; SQLite will not notice.\n  "
            + "\n  ".join(f"{t} -> {target}" for t, target in out_of_order)
        )

    @pytest.mark.parametrize("path", migration_files(), ids=lambda p: p.name)
    def test_each_migration_declares_before_referencing(self, path: Path):
        """Same rule, per migration file.

        A migration applied to an empty database has to stand on its own for
        the tables it creates. It may reference tables an *earlier* migration
        created, which is why only same-file forward references fail here.
        """
        declared, references = declared_and_referenced(path.read_text(encoding="utf-8"))
        position = {name: index for index, name in enumerate(declared)}

        out_of_order = [
            (table, target)
            for table, target in references
            if target != table
            and target in position
            and position[target] > position[table]
        ]
        assert not out_of_order, f"{path.name}: " + ", ".join(
            f"{t} -> {target}" for t, target in out_of_order
        )


class TestIdempotence:
    """Every migration must be safe to run more than once."""

    @pytest.mark.parametrize("path", migration_files(), ids=lambda p: p.name)
    def test_every_statement_is_guarded(self, path: Path):
        """An operator who runs a migration twice must not get an error.

        In practice they will: migrations get re-applied after a restore, on a
        replica, or because someone was not sure whether it had run. A file
        that only works once turns that into an outage.
        """
        sql = path.read_text(encoding="utf-8")
        # Strip comments before checking, or the prose explaining the guards
        # would satisfy the check on its own.
        body = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )

        unguarded: list[str] = []
        for statement in re.split(r";\s*\n", body):
            # The guard is checked against the WHOLE statement; only the
            # failure message is truncated. Checking the truncated form is a
            # mistake this test made on its first run: `ON CONFLICT DO NOTHING`
            # sits past the 100th character of an INSERT, so every correctly
            # guarded seed was reported as unguarded.
            full = " ".join(statement.split())
            if not full:
                continue
            upper = full.upper()
            head = full[:100]
            if upper.startswith("CREATE TABLE") and "IF NOT EXISTS" not in upper:
                unguarded.append(head)
            elif upper.startswith("CREATE INDEX") and "IF NOT EXISTS" not in upper:
                unguarded.append(head)
            elif upper.startswith("ALTER TABLE") and "IF NOT EXISTS" not in upper:
                unguarded.append(head)
            elif upper.startswith("INSERT INTO") and "ON CONFLICT" not in upper:
                unguarded.append(head)

        assert not unguarded, (
            f"{path.name} has statements that would fail on a re-run:\n  "
            + "\n  ".join(unguarded)
        )


class TestMigrationsMatchTheGeneratedSchema:
    def test_001_is_the_current_generated_schema(self):
        """001_init.sql is generated from `ledger.schema_sql`, not hand-written.

        Its own header says so. If it drifts, a fresh Postgres database gets a
        different schema from the one every SQLite test runs against — and the
        difference surfaces as a missing column in production.
        """
        import sys

        sys.path.insert(0, str(REPO_ROOT / "facilitator"))
        from ledger import schema_sql

        generated = schema_sql("postgres")
        on_disk = (MIGRATIONS / "001_init.sql").read_text(encoding="utf-8")

        # The file is the header comment plus the generated body, so the body
        # must appear verbatim. Compared with whitespace normalised, because a
        # trailing-newline difference is not drift.
        assert " ".join(generated.split()) in " ".join(on_disk.split()), (
            "facilitator/migrations/001_init.sql is out of date. Regenerate it:\n"
            "  python -c \"import sys; sys.path.insert(0, 'facilitator'); "
            'from ledger import schema_sql; print(schema_sql(\'postgres\'))"'
        )

    def test_the_seeded_accounts_match_the_chart_of_accounts(self):
        """Migration 006's INSERTs are generated from journal.CHART_OF_ACCOUNTS.

        Two definitions of what an account means would drift, and the FK on
        `journal_entries.account_code` means the drift shows up as a posting
        failing rather than as a wrong report — late, and far from the cause.
        """
        import sys

        sys.path.insert(0, str(REPO_ROOT / "facilitator"))
        from journal import CHART_OF_ACCOUNTS

        sql = (MIGRATIONS / "006_double_entry_journal.sql").read_text(encoding="utf-8")
        seeded = set(re.findall(r"VALUES \('(\d+)', '([^']+)'", sql))
        expected = {(code, name) for code, name, _, _ in CHART_OF_ACCOUNTS}

        assert seeded == expected, (
            "Migration 006's account seed has drifted from journal.CHART_OF_ACCOUNTS. "
            "Regenerate it rather than hand-editing."
        )


class TestNoDestructiveStatements:
    """A migration must never destroy data that predates it."""

    @pytest.mark.parametrize("path", migration_files(), ids=lambda p: p.name)
    def test_no_drops_updates_or_deletes(self, path: Path):
        """DROP, DELETE, and UPDATE have no place in these files.

        Adding a column and backfilling it is one thing; a migration that
        UPDATEs existing rows is rewriting history that someone may already
        have reported on. Where this project needed to represent "these old
        rows were never backed by anything" — agents enrolled under
        trust-on-first-use, commitments with no operator — it left the columns
        NULL and said so in the header, rather than inventing values.
        """
        body = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        forbidden = re.findall(
            r"^\s*(DROP\s+TABLE|DROP\s+COLUMN|DELETE\s+FROM|UPDATE\s+\w+\s+SET)",
            body,
            re.IGNORECASE | re.MULTILINE,
        )
        assert not forbidden, f"{path.name} contains destructive SQL: {forbidden}"
