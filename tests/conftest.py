"""Shared pytest fixtures.

Two kinds of test live in this suite:

  * Service-level tests drive the facilitator through FastAPI's TestClient
    against a throwaway SQLite file. They need nothing running and are the bulk
    of the coverage.

  * Integration tests drive the real HTTP path through the Express resource
    server, which means both services must be up. Locally they skip when
    nothing is listening. In CI they must not skip silently — a test suite that
    quietly stops testing the thing it exists to test is worse than no suite —
    so setting REQUIRE_INTEGRATION=1 turns an unreachable service into a
    failure instead.

A third axis, orthogonal to both: which *database* the ledger runs on. Every
test above runs unmodified against SQLite by default, and against real
Postgres when TEST_LEDGER_DSN is set — see `ledger_path` below. CI's
"Facilitator + tests (Postgres)" job sets it against a postgres:16 service
container, which is the actual proof that facilitator/db.py's dialect shim
did not quietly break the money-handling code it sits in front of.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The facilitator's modules import each other by bare name (`from ledger
# import ...`), so its directory has to be importable. pyproject sets this for
# pytest too; doing it here as well keeps the file runnable on its own.
sys.path.insert(0, str(REPO_ROOT / "facilitator"))

TEST_SECRET = "test-secret-not-the-demo-one"
RESOURCE_URL = os.getenv("RESOURCE_URL", "http://localhost:3402/premium/market-report")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "http://localhost:8402")

# A postgres:// URL, set only by the CI job that runs this suite against real
# Postgres. Unset everywhere else, including local dev — this project has no
# Postgres available on the machine it was built on, so "run pytest" staying
# SQLite-only by default is what keeps that true.
TEST_LEDGER_DSN = os.getenv("TEST_LEDGER_DSN")


def _reset_postgres_ledger(dsn: str) -> None:
    """Makes one shared Postgres database look like a fresh SQLite file.

    TEST_LEDGER_DSN points every test in a run at the *same* physical
    database — there is no cheap per-test-database equivalent to a throwaway
    SQLite file — so isolation comes from resetting state before each test
    instead of from a fresh file. Ensures the schema exists first (the
    container starts genuinely empty and nothing else has necessarily run
    yet), then empties every table.

    `TRUNCATE ... RESTART IDENTITY` also resets the `events.id` sequence, so
    ids stay small and predictable the same way a fresh SQLite file's would.

    Reuses `db._split_sql_statements` rather than splitting on `;` again
    here — a second hand-rolled splitter is exactly how this project's own
    schema comment (its own semicolon, mid-sentence) broke the *first* one
    without anyone noticing until real Postgres said so in CI.
    """
    import psycopg
    from db import _split_sql_statements
    from ledger import schema_sql

    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in _split_sql_statements(schema_sql("postgres")):
            conn.execute(statement)
        # Every table, not just the money ones. `agents` and `webhook_events`
        # both carry uniqueness constraints that are the *point* of them —
        # first-registration-wins and exactly-once webhook delivery — so
        # leaving either populated between tests makes a second run of the
        # same test fail on a conflict that has nothing to do with the code
        # under test. The SQLite path gets this free by using a fresh file.
        # Every table, including the Phase 2 identity and consent ones. A
        # missing name here does not fail loudly — it leaks rows into the next
        # test, which shows up much later as an order-dependent failure on
        # Postgres only. `operators` and `merchants` are especially easy to
        # forget and especially annoying: a leftover operator makes a
        # "create then read back" test pass for the wrong reason.
        conn.execute(
            "TRUNCATE TABLE agents, offers, batches, commitments, events, webhook_events,"
            " operators, merchants, api_credentials, agent_credentials,"
            " enrollment_challenges, spending_consents, consent_publishers,"
            " authority_accounts, reservations"
            " RESTART IDENTITY CASCADE"
        )


# The five switches whose *production* default is closed, and which the bulk
# of this suite predates.
#
# Phase 2 changed each of these defaults from permissive to secure:
#
#   DEMO_OPEN_DASHBOARD   /ledger/summary, /economics and /ledger/events used
#                         to answer anyone. They now require an API key.
#   ALLOW_HMAC_FALLBACK   an unregistered agent could pay with a shared-secret
#                         MAC. Registration is now required.
#   DEMO_UNSAFE_TOFU      anyone could bind a key to an unclaimed agent id by
#                         being first. Enrollment now needs an authenticated
#                         operator and proof of possession.
#   REQUIRE_CONSENT       an agent could spend with no operator behind it.
#                         A consent is now required to incur any expense.
#   AUTHORITY_REQUIRED    content was released against a promise. An amount
#                         must now be reserved against a real authority
#                         balance before the handler runs.
#
# The existing tests exercise the *payment flow* — negotiation, double-spend,
# batching, webhooks — and are not about authorization. Rewriting all of them
# to carry bearer tokens would obscure what each is actually asserting without
# testing anything new, so they run under the legacy profile.
#
# The secure defaults are not therefore untested: `tests/test_control_plane.py`
# builds the facilitator with this fixture disabled and asserts, endpoint by
# endpoint, that each one refuses an unauthenticated caller and refuses a
# caller from the wrong tenant. Opting *out* of the demo profile is how a test
# declares it is about authorization.
LEGACY_DEMO_PROFILE = {
    "DEMO_OPEN_DASHBOARD": "true",
    "ALLOW_HMAC_FALLBACK": "true",
    "DEMO_UNSAFE_TOFU": "true",
    # An agent with no operator consent may still spend. With it on, /offer
    # and /settle refuse any agent that no operator has authorised.
    "REQUIRE_CONSENT": "false",
    # Content may be released without an amount being held against an
    # authority balance first. The last switch, and the one that separates
    # "you are allowed to" from "something stands behind it".
    "AUTHORITY_REQUIRED": "false",
}


@pytest.fixture(autouse=True)
def legacy_demo_profile(request, monkeypatch):
    """Runs the pre-Phase-2 permissive profile, unless a test opts out.

    Mark a test or class with `@pytest.mark.secure_defaults` to get the
    production configuration instead — closed dashboard, no HMAC fallback, no
    trust-on-first-use, and both consent and reserved authority required.
    """
    if request.node.get_closest_marker("secure_defaults"):
        # Explicitly clear rather than merely not setting them: a stray value
        # in the developer's own environment must not be able to turn a
        # security test into a passing no-op.
        for name in LEGACY_DEMO_PROFILE:
            monkeypatch.delenv(name, raising=False)
        return

    for name, value in LEGACY_DEMO_PROFILE.items():
        monkeypatch.setenv(name, value)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "secure_defaults: run with the production-like closed configuration "
        "rather than the permissive demo profile",
    )


@pytest.fixture
def ledger_path(tmp_path):
    """A connection string for one test's worth of fresh, isolated ledger.

    Defaults to a throwaway SQLite file path. When TEST_LEDGER_DSN is set,
    returns that Postgres URL instead, after truncating it — every test in
    the suite that takes `ledger_path` (directly, or via the `ledger` and
    `facilitator_app` fixtures below) then runs against real Postgres with
    no change to the test itself. The name stays `ledger_path` for exactly
    that reason: nothing downstream needs to know which engine it got.

    Settlement sweeps every pending commitment in the database, so tests
    that shared state would interfere in ways that depend on execution
    order — for SQLite that is naturally avoided by a fresh file; for the
    shared Postgres database it is why this resets before every test.
    """
    if TEST_LEDGER_DSN:
        _reset_postgres_ledger(TEST_LEDGER_DSN)
        return TEST_LEDGER_DSN
    return str(tmp_path / "ledger.db")


@pytest.fixture
def ledger(ledger_path):
    """A `Ledger` bound to a throwaway database."""
    from ledger import Ledger

    return Ledger(str(ledger_path))


@pytest.fixture
def facilitator_app(ledger_path, monkeypatch):
    """The facilitator, configured for tests and reloaded.

    `main.py` reads its configuration at import time, which is the right thing
    for a service and awkward for a test. Reloading after setting the
    environment gives each test a facilitator wired to its own ledger with a
    secret that is not the one committed to the repo.
    """
    monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("MOCK_RAZORPAY", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
    monkeypatch.setenv("SETTLEMENT_MODE", "deferred")
    monkeypatch.setenv("OFFER_TTL_SECONDS", "300")

    import main

    importlib.reload(main)
    return main


@pytest.fixture
def client(facilitator_app):
    """A TestClient for the facilitator.

    Leaves `raise_server_exceptions` at its default, so an unexpected error in
    a test surfaces as that error rather than as a mysterious 500.
    """
    from fastapi.testclient import TestClient

    with TestClient(facilitator_app.app) as test_client:
        yield test_client


@pytest.fixture
def crashing_client(facilitator_app):
    """A TestClient that returns the 500 instead of re-raising.

    Needed to assert on what a *caller* sees when the service breaks — the
    default client re-raises server-side exceptions, so the response never
    reaches the test.
    """
    from fastapi.testclient import TestClient

    with TestClient(facilitator_app.app, raise_server_exceptions=False) as test_client:
        yield test_client


def _service_up(url: str) -> bool:
    """Whether something is answering at `url`."""
    try:
        httpx.get(url, timeout=2.0)
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def live_services():
    """Ensures both services are reachable, or skips.

    Under REQUIRE_INTEGRATION the skip becomes a failure, so CI cannot pass by
    quietly not running the integration tests.
    """
    required = os.getenv("REQUIRE_INTEGRATION") == "1"
    missing = [
        name
        for name, url in (
            ("resource-server", f"{RESOURCE_URL.rsplit('/premium', 1)[0]}/health"),
            ("facilitator", f"{FACILITATOR_URL}/health"),
        )
        if not _service_up(url)
    ]

    if missing:
        message = (
            f"{', '.join(missing)} not reachable. Start them with the commands in "
            "docs/demo-script.md to run the integration tests."
        )
        if required:
            pytest.fail(f"REQUIRE_INTEGRATION=1 but {message}")
        pytest.skip(message)

    return {"resource": RESOURCE_URL, "facilitator": FACILITATOR_URL}
