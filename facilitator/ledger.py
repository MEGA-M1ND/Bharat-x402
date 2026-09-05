"""Ledger and audit trail for the INR facilitator — SQLite locally, Postgres
in production, behind the shim in db.py.

Every state change a payment goes through is written here, and every write also
emits a structured JSON line to stdout. The ledger is the answer to the
question a publisher will actually ask — *which agent owes me what, and did it
get paid?* — so it is deliberately the most boring, most explicit code in the
project.

Six tables:

    agents         an agent's registered Ed25519 public key
    offers         a quoted price, signed and time-limited
    batches        one real Razorpay charge covering many commitments
    commitments    an agent's accepted offer — a debt, not yet money
    events         append-only log of everything that happened
    webhook_events Razorpay callbacks we have already processed

(`batches` is declared before `commitments` because `commitments.batch_id`
references it — SQLite resolves foreign keys lazily and does not care about
declaration order, but Postgres checks at `CREATE TABLE` time and errors if
the referenced table does not exist yet.)

The offer -> commitment -> batch progression is the whole idea. A ₹5 API call
becomes a commitment instantly (cheap, no gateway involved), and rupees move
later when many commitments are charged together.

COMMITTED IS NOT COLLECTED
--------------------------
Two different questions live in this schema and it is worth being precise
about which column answers which, because conflating them is how a payments
ledger ends up overstating revenue:

  * `commitments.status = 'settled'` means *this debt has been assigned to a
    batch*. It is no longer separately chargeable and will not be
    double-billed. It does **not** mean anyone has paid.
  * `batches.status = 'paid'` means Razorpay told us — over a signed webhook
    (see webhooks.py) — that money actually arrived.

A Payment Link that was created is an invoice, not a receipt. Until the
webhook lands, the honest description of a batch is "billed, awaiting
payment", which is why `daily_summary` reports `committedPaise` and
`collectedPaise` as separate figures rather than one number labelled
"revenue".

Money is stored as INTEGER paise throughout. Never floats — a float would
silently lose precision on rupee arithmetic and there is no reason to risk it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import db

# Serialises writes within one process. Correctness does not depend on this —
# `create_commitment`'s atomic conditional UPDATE (see below) is what actually
# prevents a double-charge, and that holds under Postgres across separate
# processes regardless. This lock only avoids SQLite's "database is locked"
# noise under FastAPI's threadpool, and costs nothing worth special-casing
# away for Postgres, so it applies uniformly to both.
_WRITE_LOCK = threading.Lock()

DEFAULT_DB_PATH = os.getenv("LEDGER_DB_PATH", "./data/ledger.db")

# How long `log_event` stops attempting audit inserts after one fails. Short,
# because the cost of being wrong is only that recovery is noticed a few
# seconds late; long enough that a burst of events during an outage does not
# pay a connection timeout each. See `Ledger._audit_writes_allowed`.
AUDIT_BREAKER_SECONDS = float(os.getenv("LEDGER_AUDIT_BREAKER_SECONDS", "30"))

# Default cap for `create_commitment`, meaning "no limit". Kept here rather
# than imported from limits.py so ledger.py stays free of any dependency on
# the policy layer — the ledger enforces a number it is handed, and has no
# opinion about where that number comes from.
UNLIMITED_PAISE = 2**62


class DailyCapExceeded(Exception):
    """Booking a commitment would take an agent over its daily cap.

    Deliberately not a `ValueError`. `main.py` already maps `ValueError` from
    `create_commitment` to "this offer was already spent, return the existing
    commitment" — and treating a cap breach that way would answer a refused
    payment with somebody else's receipt.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        settle_date: str,
        committed_paise: int,
        amount_paise: int,
        daily_cap_paise: int,
    ) -> None:
        self.agent_id = agent_id
        self.settle_date = settle_date
        self.committed_paise = committed_paise
        self.amount_paise = amount_paise
        self.daily_cap_paise = daily_cap_paise
        self.remaining_paise = max(daily_cap_paise - committed_paise, 0)
        super().__init__(
            f"agent {agent_id} has committed {committed_paise} paise on {settle_date}; "
            f"adding {amount_paise} would exceed the cap of {daily_cap_paise} paise "
            f"({self.remaining_paise} remaining)"
        )


# Matches the `user:password@` portion of any connection URL. Database driver
# errors quote the DSN they failed on more often than you would like — psycopg
# does it on several connection failures — so any driver message that escapes
# this module has to be scrubbed first, not merely assumed to be clean.
_DSN_CREDENTIALS_RE = re.compile(r"://[^\s/@]+:[^\s/@]+@")


def redact_credentials(text: str) -> str:
    """Strips `user:password@` out of any connection URL in `text`.

    Keeps the scheme and host, which are what make an error diagnosable, and
    drops the part that must never reach a log line or an HTTP response.

    Args:
        text: Arbitrary message text, typically a driver exception.

    Returns:
        The same text with credentials replaced.
    """
    return _DSN_CREDENTIALS_RE.sub("://<redacted>@", text)


def utc_now() -> str:
    """Current time as an ISO-8601 UTC string.

    One timestamp format everywhere, so ledger rows sort lexicographically and
    the reporting script does not need a date parser.
    """
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_utc() -> str:
    """Current UTC date as YYYY-MM-DD — the batching key."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _events_id_column(dialect: str) -> str:
    """The one line of DDL that actually differs between engines.

    Every other table, column, and constraint below is identical SQL in both
    SQLite and Postgres — this auto-incrementing primary key is the single
    exception, so it is the only thing parameterised rather than duplicating
    the whole schema for one line's difference.
    """
    if dialect == "postgres":
        return "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
    return "id INTEGER PRIMARY KEY AUTOINCREMENT"


def schema_sql(dialect: str) -> str:
    """The full schema, for either engine.

    Args:
        dialect: "sqlite" or "postgres".
    """
    return f"""
-- ======================================================================
-- IDENTITY AND CONSENT
-- ======================================================================
-- Declared first because `agents`, `api_credentials`, and `spending_consents`
-- all reference them. Postgres resolves foreign keys at CREATE TABLE time and
-- errors if the referenced table does not exist yet; SQLite resolves lazily
-- and would not have caught the ordering. Same trap as `batches` before
-- `commitments`.

-- The party that answers for an agent's spending. An agent is a *process*;
-- an operator is who you would invoice, suspend, or argue with.
--
-- This is the entity the original design was missing entirely. Without it a
-- pseudonymous key promised to pay and content was released on that promise,
-- with nothing standing behind it. See docs/adr/0001.
CREATE TABLE IF NOT EXISTS operators (
    operator_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    -- active | suspended | closed
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL
);

-- The publisher being paid. Consents are scoped to these, so an operator can
-- authorise spend at one publisher without authorising it everywhere.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id                  TEXT PRIMARY KEY,
    display_name                 TEXT NOT NULL,
    -- Where collected value would eventually be paid out. Opaque here: this
    -- project never performs a payout.
    settlement_account_reference TEXT,
    status                       TEXT NOT NULL DEFAULT 'active',
    created_at                   TEXT NOT NULL
);

-- Control-plane API keys.
--
-- Only the HASH is stored. The plaintext is returned once, at issuance, and
-- is unrecoverable afterwards — so a disclosure of this table yields nothing
-- an attacker can authenticate with. `key_prefix` is a non-secret fragment
-- kept so a human can tell two keys apart in a listing without the listing
-- being a credential dump.
CREATE TABLE IF NOT EXISTS api_credentials (
    credential_id TEXT PRIMARY KEY,
    -- Exactly one of these is set: the tenant this key speaks for. Enforced
    -- in `create_api_credential` rather than by a CHECK, because the check
    -- would have to differ per dialect.
    operator_id   TEXT,
    merchant_id   TEXT,
    label         TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    -- Space-separated scope tokens. A string rather than a join table: scopes
    -- are read on every authenticated request and never queried across rows.
    scopes        TEXT NOT NULL,
    -- active | revoked
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT,
    -- Set on rotation, so an audit can follow a key's lineage.
    replaced_by   TEXT,
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id)
);

-- One row per agent that has registered a signing key. Holds only the
-- *public* half: the facilitator can verify an agent's commitments and can
-- never produce one, which is the entire point of moving off a shared HMAC
-- secret (see payment_verifier.py).
--
-- `public_key`/`algorithm` are retained as the *currently active* key. They
-- are a denormalisation of `agent_credentials`, kept because every existing
-- verification path reads them and because the join is on the hot path of
-- every paid request. `agent_credentials` is authoritative for validity.
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    -- base64 of the raw 32-byte Ed25519 public key.
    public_key    TEXT NOT NULL,
    algorithm     TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    -- Null for agents enrolled under trust-on-first-use, which is exactly the
    -- gap this column exists to make visible.
    operator_id   TEXT,
    -- active | suspended | closed
    status        TEXT NOT NULL DEFAULT 'active',
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id)
);

-- The signing-key lifecycle: enrollment, rotation, revocation, expiry.
--
-- Credentials are ROWS, not a column that gets overwritten, and that is the
-- whole design. Revocation must stop *new* authorization without invalidating
-- *past* evidence — an acceptance signed last week has to stay verifiable
-- against the key that signed it, or rotating a key would silently destroy
-- the audit trail that makes Ed25519 worth using at all.
CREATE TABLE IF NOT EXISTS agent_credentials (
    credential_id TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    algorithm     TEXT NOT NULL,
    public_key    TEXT NOT NULL,
    -- active | superseded | revoked
    status        TEXT NOT NULL DEFAULT 'active',
    valid_from    TEXT NOT NULL,
    -- Null means no expiry.
    valid_until   TEXT,
    revoked_at    TEXT,
    replaced_by   TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
);

-- Proof-of-possession challenges for authenticated key enrollment.
--
-- Replaces "the first caller to claim an id owns it". An operator asks for a
-- challenge, the agent signs the nonce with the private key it claims to
-- hold, and only a valid signature binds the public key. Single-use and
-- time-limited, for the same reason offers are.
CREATE TABLE IF NOT EXISTS enrollment_challenges (
    challenge_id TEXT PRIMARY KEY,
    operator_id  TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    nonce        TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id)
);

-- An operator's standing authorisation for one agent to spend, with limits.
--
-- Modelled on the consent shape Razorpay and NPCI describe for UPI Reserve
-- Pay: a one-time authorisation carrying spending limits, revocable at any
-- time, under which an agent transacts without re-prompting. Every limit is
-- integer paise.
--
-- `reserved_paise` and `consumed_paise` are the live counters. They move
-- inside conditional UPDATEs (see authority.py), never read-then-write, so
-- concurrent requests cannot jointly exceed one limit.
CREATE TABLE IF NOT EXISTS spending_consents (
    consent_id              TEXT PRIMARY KEY,
    operator_id             TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
    -- active | suspended | revoked | expired
    status                  TEXT NOT NULL DEFAULT 'active',
    currency                TEXT NOT NULL DEFAULT 'INR',
    per_request_limit_paise INTEGER NOT NULL,
    daily_limit_paise       INTEGER NOT NULL,
    total_limit_paise       INTEGER NOT NULL,
    reserved_paise          INTEGER NOT NULL DEFAULT 0,
    consumed_paise          INTEGER NOT NULL DEFAULT 0,
    valid_from              TEXT NOT NULL,
    valid_until             TEXT,
    created_at              TEXT NOT NULL,
    revoked_at              TEXT,
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id),
    FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
);

-- Publisher scope for a consent. No rows means "any publisher"; one or more
-- restricts the consent to exactly those merchants.
--
-- A join table rather than a list column because "which consents may spend at
-- this merchant?" is a real query, and because a scope that is wrong is a
-- scope that authorises spending somewhere it should not.
CREATE TABLE IF NOT EXISTS consent_publishers (
    consent_id  TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    PRIMARY KEY (consent_id, merchant_id),
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id)
);

-- ======================================================================
-- AUTHORITY: what actually stands behind a request
-- ======================================================================

-- The balance a consent draws against.
--
-- WHAT THIS IS NOT: money. Nothing here is held at a bank, and no NPCI
-- mandate exists. `simulated_reserve` models the *domain* behaviour of UPI
-- Reserve Pay — an amount blocked up front and debited repeatedly — because
-- the accounting and concurrency questions are real even when the block is
-- not. See docs/gap-analysis.md, which says so in the same words.
--
-- Every column is integer paise, and they are related by an invariant the
-- tests assert:
--
--     funded = available + reserved + captured - refunded
--
-- `available` is what a new request may draw on. `reserved` is held for
-- requests in flight. `captured` has been converted into a receivable and is
-- gone from this account's spendable balance for good.
CREATE TABLE IF NOT EXISTS authority_accounts (
    account_id      TEXT PRIMARY KEY,
    consent_id      TEXT NOT NULL UNIQUE,
    operator_id     TEXT NOT NULL,
    -- prefunded        a balance topped up in advance. No credit risk.
    -- simulated_reserve a modelled Reserve Pay block. Explicitly simulated.
    -- credit           no funds at all; a limit the platform is exposed to.
    backing         TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'INR',
    funded_paise    INTEGER NOT NULL DEFAULT 0,
    available_paise INTEGER NOT NULL DEFAULT 0,
    reserved_paise  INTEGER NOT NULL DEFAULT 0,
    captured_paise  INTEGER NOT NULL DEFAULT 0,
    refunded_paise  INTEGER NOT NULL DEFAULT 0,
    -- Credit only: how far the platform will let exposure run. Zero for the
    -- funded backings, where `available_paise` is the whole story.
    credit_limit_paise INTEGER NOT NULL DEFAULT 0,
    -- Collection failures that have not been recovered. Past a threshold this
    -- suspends further authorization — see limits.py.
    overdue_paise   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id),
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id)
);

-- An amount held against an account while a request is in flight.
--
-- The lifecycle that makes "content is released against authority" true
-- rather than aspirational:
--
--   held      -> reserved before the handler runs
--   captured  -> fulfillment succeeded; converted to a receivable, once
--   released  -> fulfillment failed; the authority goes back
--   expired   -> neither happened before the TTL; swept back
--
-- `offer_id` is UNIQUE. That is the exactly-once guarantee for capture,
-- enforced by the database rather than by application logic remembering to
-- check — the same discipline as `commitments.offer_id`. A retried request
-- finds the existing reservation instead of holding a second amount.
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    consent_id     TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    offer_id       TEXT NOT NULL UNIQUE,
    amount_paise   INTEGER NOT NULL,
    -- held | captured | released | expired
    status         TEXT NOT NULL DEFAULT 'held',
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    resolved_at    TEXT,
    commitment_id  TEXT,
    FOREIGN KEY (account_id) REFERENCES authority_accounts (account_id),
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id)
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id      TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    resource_url  TEXT,
    amount_paise  INTEGER NOT NULL,
    asset         TEXT NOT NULL,
    scheme        TEXT NOT NULL,
    network       TEXT NOT NULL,
    pay_to        TEXT NOT NULL,
    nonce         TEXT NOT NULL,
    signature     TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    -- open: quoted, not yet spent. consumed: turned into a commitment.
    status        TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id            TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    settle_date         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    commitment_count    INTEGER NOT NULL,
    total_paise         INTEGER NOT NULL,
    -- What Razorpay gave us back. Null in dry-run.
    payment_link_id     TEXT,
    payment_link_url    TEXT,
    -- created | paid | expired | cancelled | failed | dry_run
    --
    -- `created` means an invoice exists, NOT that money arrived. Only the
    -- signed Razorpay webhook moves a batch to `paid` (see webhooks.py).
    status              TEXT NOT NULL,
    razorpay_mode       TEXT NOT NULL,
    error_message       TEXT,
    -- Filled in by the webhook, not by the settlement run that created the
    -- link. Null until Razorpay confirms payment.
    paid_at             TEXT,
    amount_paid_paise   INTEGER,
    razorpay_payment_id TEXT,
    -- payment_link | reserve_pay. Which instrument settled this batch.
    --
    -- Earns its place: a Reserve Pay debit has no URL, and neither does a
    -- *failed* Payment Link, so without this the two are indistinguishable
    -- in the ledger. See reserve_pay.py.
    instrument          TEXT NOT NULL DEFAULT 'payment_link'
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    -- One commitment per offer, enforced by the database rather than by
    -- application logic. This is what makes double-charging structurally
    -- impossible rather than merely unlikely.
    offer_id      TEXT NOT NULL UNIQUE,
    agent_id      TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    amount_paise  INTEGER NOT NULL,
    asset         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    settle_date   TEXT NOT NULL,
    -- deferred: awaiting batch. per_request: charged immediately.
    mode          TEXT NOT NULL,
    -- pending | settled | failed
    --
    -- Note on the word: `settled` here has always meant "assigned to a
    -- batch", never "money arrived". It is retained because reporting and a
    -- number of tests read it, and renaming a stored value is a data
    -- migration rather than a rename. `collected` is the word reserved for
    -- gateway-confirmed money, and lives on `batches.status`.
    status        TEXT NOT NULL DEFAULT 'pending',
    batch_id      TEXT,
    -- Who answers for this debt, and under what authorisation.
    --
    -- Null on commitments booked before operators existed, and on any booked
    -- through the unsafe demo path. A null here means "nobody is on the hook
    -- for this but a pseudonymous key", which is precisely the state worth
    -- being able to query for.
    operator_id   TEXT,
    consent_id    TEXT,
    FOREIGN KEY (offer_id) REFERENCES offers (offer_id),
    FOREIGN KEY (batch_id) REFERENCES batches (batch_id),
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id),
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id)
);

-- Append-only. Nothing in this table is ever updated or deleted; it is the
-- audit trail you would hand an auditor or replay to reconstruct a day.
CREATE TABLE IF NOT EXISTS events (
    {_events_id_column(dialect)},
    ts           TEXT NOT NULL,
    event        TEXT NOT NULL,
    agent_id     TEXT,
    resource_id  TEXT,
    amount_paise INTEGER,
    status       TEXT,
    detail       TEXT
);

-- Razorpay retries a failed delivery with exponential backoff for 24 hours
-- (after which the webhook is disabled), so the same event will arrive more
-- than once as a matter of course — not as an edge case. The
-- primary key is the dedupe guard: processing claims the key with an INSERT
-- first, and a duplicate delivery loses that INSERT and returns early
-- without touching a batch. Same discipline as `commitments.offer_id`:
-- exactly-once is enforced by a database constraint, not by application
-- logic remembering to check.
CREATE TABLE IF NOT EXISTS webhook_events (
    dedupe_key      TEXT PRIMARY KEY,
    event           TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    payment_link_id TEXT,
    -- claimed | applied | ignored | unknown_link
    outcome         TEXT NOT NULL,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_commitments_settlement
    ON commitments (status, agent_id, settle_date);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_batches_payment_link ON batches (payment_link_id);

-- The authentication hot path: every control-plane request hashes its bearer
-- token and looks it up here.
CREATE INDEX IF NOT EXISTS idx_api_credentials_hash ON api_credentials (key_hash);
-- "Which key is currently active for this agent?" — read on every paid request.
CREATE INDEX IF NOT EXISTS idx_agent_credentials_agent
    ON agent_credentials (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_consents_agent ON spending_consents (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agents_operator ON agents (operator_id);
-- Tenant-scoped reads: "everything this operator is on the hook for".
CREATE INDEX IF NOT EXISTS idx_commitments_operator ON commitments (operator_id, settle_date);
-- The sweeper's query: held reservations past their TTL.
CREATE INDEX IF NOT EXISTS idx_reservations_expiry ON reservations (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_authority_operator ON authority_accounts (operator_id);
"""


class Ledger:
    """The facilitator's book of record — SQLite locally, Postgres in production.

    Args:
        dsn: A SQLite file path, or a `postgres://`/`postgresql://` connection
            URL. `db.dialect_for` decides which from the prefix.

    Schema bootstrap differs by engine. SQLite always runs `CREATE TABLE IF
    NOT EXISTS` on construction, as it always has — cheap, and a demo file
    that does not exist yet is the common case. Postgres does not, by
    default: schema changes on a shared production database belong in a
    migration applied deliberately (see `facilitator/migrations/001_init.sql`),
    not as a side effect of importing this module on every cold start. Set
    `LEDGER_AUTO_MIGRATE=1` to bootstrap a Postgres schema automatically
    anyway — used by CI, where the test database starts genuinely empty and
    there is no one to run a migration by hand.
    """

    def __init__(self, dsn: str = DEFAULT_DB_PATH) -> None:
        self.dsn = dsn
        self.dialect = db.dialect_for(dsn)

        # Zero means "closed" — attempt every audit insert. Set to a
        # monotonic deadline when one fails. Only ever gates `log_event`;
        # money writes are never suppressed.
        self._audit_breaker_open_until = 0.0

        if self.dialect == "sqlite":
            self.db_path = dsn
            parent = os.path.dirname(os.path.abspath(dsn))
            os.makedirs(parent, exist_ok=True)
            self._init_schema()
        else:
            self.db_path = None
            if os.getenv("LEDGER_AUTO_MIGRATE", "").strip().lower() in ("1", "true", "yes"):
                self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[db.Conn]:
        """Yields a connection, committing on success and rolling back on error."""
        with db.connect(self.dsn, self.dialect) as conn:
            yield conn

    def _init_schema(self) -> None:
        with _WRITE_LOCK, self._connect() as conn:
            conn.executescript(schema_sql(self.dialect))

    # -- audit -------------------------------------------------------------

    def log_event(
        self,
        event: str,
        *,
        agent_id: str | None = None,
        resource_id: str | None = None,
        amount_paise: int | None = None,
        status: str | None = None,
        **detail: Any,
    ) -> None:
        """Records one thing that happened, to both the ledger and stdout.

        Writing to two places on purpose: the table is queryable after the
        fact, and the stdout line is what you watch during a live demo. A
        rejected payment always goes through here with its reason — nothing in
        this service fails silently.

        Args:
            event: Machine-readable event name, e.g. "payment_verified".
            agent_id: Which agent this concerns.
            resource_id: Which resource this concerns.
            amount_paise: Money involved, in paise.
            status: Outcome marker, e.g. "ok" or "rejected".
            **detail: Anything else worth keeping; stored as JSON.

        Never raises, and writes stdout *before* the database. Both of those
        are deliberate, and they are the fix for a real failure this service
        had: with Postgres unreachable, a rejected webhook spent ten seconds
        blocking on the audit insert's pool timeout, then the exception
        reached the unhandled-error handler, which called this method again
        for another ten seconds, and a request that should have been a fast
        400 became a slow 500. Razorpay would have timed out and retried,
        adding load to a service already in trouble.

        Ordering stdout first means the event survives a dead database rather
        than being lost with it — the previous order lost both the row *and*
        the log line. A failed insert is then reported as its own
        `ledger_write_failed` line, so a degraded audit trail is greppable
        instead of silent.

        **This leniency is scoped to the audit log and must stay that way.**
        Money writes — `create_commitment`, `record_batch`, `mark_batch_paid`
        — raise on failure and have to keep raising: an unrecorded commitment
        is revenue lost, whereas an unrecorded log line is still sitting in
        stdout. Note that a dead ledger cannot produce a charged-but-unlogged
        payment either, because the commitment write would have failed first.
        """
        ts = utc_now()
        detail_json = json.dumps(detail, sort_keys=True, default=str) if detail else None

        line = {
            "ts": ts,
            "service": "facilitator",
            "event": event,
            **({"agentId": agent_id} if agent_id else {}),
            **({"resourceId": resource_id} if resource_id else {}),
            **({"amountPaise": amount_paise} if amount_paise is not None else {}),
            **({"status": status} if status else {}),
            **detail,
        }
        print(json.dumps(line, sort_keys=True, default=str), file=sys.stdout, flush=True)

        if not self._audit_writes_allowed():
            return

        try:
            with _WRITE_LOCK, self._connect() as conn:
                conn.execute(
                    "INSERT INTO events"
                    " (ts, event, agent_id, resource_id, amount_paise, status, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, event, agent_id, resource_id, amount_paise, status, detail_json),
                )
        except Exception as exc:  # noqa: BLE001 - reported below, never raised
            self._audit_breaker_open_until = time.monotonic() + AUDIT_BREAKER_SECONDS
            print(
                json.dumps(
                    {
                        "ts": ts,
                        "service": "facilitator",
                        "event": "ledger_write_failed",
                        "status": "degraded",
                        "droppedEvent": event,
                        "error": redact_credentials(f"{type(exc).__name__}: {str(exc)[:200]}"),
                        "note": (
                            "Audit row not written; the event above is only in this log. "
                            f"Suppressing audit inserts for {AUDIT_BREAKER_SECONDS}s."
                        ),
                    },
                    sort_keys=True,
                ),
                file=sys.stdout,
                flush=True,
            )
        else:
            self._audit_breaker_open_until = 0.0

    def _audit_writes_allowed(self) -> bool:
        """Whether to attempt an audit insert, or skip it after a recent failure.

        A plain try/except still pays the connection timeout on *every* call,
        which for a service logging several events per request is most of the
        latency an outage causes. Once an insert has failed, this stops trying
        for a short window so the remaining calls are instant.

        Deliberately crude: one timestamp, no half-open probing, no counters.
        The window is short enough that recovery is picked up on the next
        event after it lapses, and this is the audit path — a smarter breaker
        here would be more moving parts guarding something that already
        degrades safely to stdout.
        """
        if self._audit_breaker_open_until == 0.0:
            return True
        if time.monotonic() >= self._audit_breaker_open_until:
            return True
        return False

    def check_connection(self) -> tuple[bool, str]:
        """Whether the ledger is reachable right now.

        `SELECT 1` — a round trip that touches no table, so it reports on the
        connection rather than on whether a migration has been applied.

        Never raises. A health check that throws is a health check that turns
        one outage into two, and the caller wants a status to report rather
        than an exception to handle.

        Returns:
            `(ok, detail)` — detail names the failure when ok is False, with
            connection-string credentials scrubbed. Drivers quote the DSN they
            failed on, and this string is returned over HTTP by `/health`.
        """
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never raised
            return False, redact_credentials(f"{type(exc).__name__}: {str(exc)[:200]}")
        return True, "connected"

    # -- agent keys --------------------------------------------------------

    def register_agent(self, *, agent_id: str, public_key: str, algorithm: str) -> dict:
        """Records an agent's public signing key, first-registration-wins.

        Trust-on-first-use, and worth being explicit that this is a real
        limitation rather than a rounding error: the first caller to claim an
        agent id owns it, because there is nothing else here to bind that id
        to. A production facilitator would issue the key at onboarding
        alongside the merchant account it settles into, so the key is
        vouched-for rather than merely first.

        What TOFU *does* buy, and the reason it is worth having over a shared
        secret even so: once an id is claimed, no one else can spend as it,
        and the facilitator itself cannot forge its commitments either. The
        rebinding attempt is refused rather than silently overwriting, which
        is what stops key rotation from doubling as account takeover.

        Args:
            agent_id: Identity being registered.
            public_key: Base64 of the raw Ed25519 public key.
            algorithm: Signature algorithm, currently always "ed25519".

        Returns:
            `{"agentId", "publicKey", "algorithm", "registeredAt", "created"}`.
            `created` is False when this was an idempotent re-registration of
            the identical key.

        Raises:
            ValueError: If the id is already bound to a *different* key.
        """
        registered_at = utc_now()

        with _WRITE_LOCK, self._connect() as conn:
            # `ON CONFLICT DO NOTHING` rather than catching a unique violation,
            # for a dialect reason worth stating because SQLite hides it
            # completely: in Postgres a failed statement aborts the entire
            # transaction, and every subsequent command on that connection
            # fails with InFailedSqlTransaction until it is rolled back. So the
            # obvious try/except INSERT, then SELECT to see what is already
            # there cannot work — the recovery read is itself refused. SQLite
            # has no such rule and passes it happily, which is exactly why the
            # suite runs against both engines.
            #
            # This is also simply better: one statement does the claim, so
            # there is no read-then-write gap, the same reasoning as
            # `create_commitment` below.
            inserted = conn.execute(
                "INSERT INTO agents (agent_id, public_key, algorithm, registered_at, status)"
                " VALUES (?, ?, ?, ?, 'active') ON CONFLICT (agent_id) DO NOTHING",
                (agent_id, public_key, algorithm, registered_at),
            )

            if inserted.rowcount == 1:
                # A credential row as well, in the same transaction.
                #
                # Verification reads `agent_credentials`, not `agents`, so a
                # key registered through this legacy path must appear there
                # too — otherwise a trust-on-first-use agent would register
                # successfully and then be unable to pay, and worse, an
                # operator revoking that credential would have nothing to
                # revoke. One enrollment path, one lifecycle.
                conn.execute(
                    "INSERT INTO agent_credentials (credential_id, agent_id, algorithm,"
                    " public_key, status, valid_from, created_at)"
                    " VALUES (?, ?, ?, ?, 'active', ?, ?)",
                    (
                        f"agck_tofu_{uuid.uuid4().hex[:12]}",
                        agent_id,
                        algorithm,
                        public_key,
                        registered_at,
                        registered_at,
                    ),
                )

            if inserted.rowcount == 0:
                existing = conn.execute(
                    "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
                # Re-registering the same key is a no-op, so an agent that
                # restarts and re-announces itself does not need to care
                # whether it has registered before.
                if existing is not None and existing["public_key"] == public_key:
                    return {
                        "agentId": agent_id,
                        "publicKey": existing["public_key"],
                        "algorithm": existing["algorithm"],
                        "registeredAt": existing["registered_at"],
                        "created": False,
                    }
                raise ValueError(
                    f"agent {agent_id} is already registered with a different public key"
                )

        return {
            "agentId": agent_id,
            "publicKey": public_key,
            "algorithm": algorithm,
            "registeredAt": registered_at,
            "created": True,
        }

    def get_agent(self, agent_id: str) -> Mapping[str, Any] | None:
        """Looks up an agent's registered key, or None if it has never registered."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()

    # == identity, credentials, and consent ================================
    #
    # Everything below is Phase 2. The methods above predate operators and are
    # left untouched so the existing verification path keeps working; these
    # add the authenticated control plane on top rather than rewriting it.

    def create_operator(self, *, operator_id: str, display_name: str) -> dict:
        """Registers an operator — the party that answers for an agent's spend."""
        created_at = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            inserted = conn.execute(
                "INSERT INTO operators (operator_id, display_name, status, created_at)"
                " VALUES (?, ?, 'active', ?) ON CONFLICT (operator_id) DO NOTHING",
                (operator_id, display_name, created_at),
            )
            if inserted.rowcount == 0:
                raise ValueError(f"operator {operator_id} already exists")
        return {
            "operatorId": operator_id,
            "displayName": display_name,
            "status": "active",
            "createdAt": created_at,
        }

    def get_operator(self, operator_id: str) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM operators WHERE operator_id = ?", (operator_id,)
            ).fetchone()

    def set_operator_status(self, *, operator_id: str, status: str) -> bool:
        """Suspends, resumes, or closes an operator. Returns whether a row moved."""
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE operators SET status = ? WHERE operator_id = ?", (status, operator_id)
            )
            return result.rowcount > 0

    def create_merchant(
        self, *, merchant_id: str, display_name: str, settlement_account_reference: str | None
    ) -> dict:
        """Registers a publisher."""
        created_at = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            inserted = conn.execute(
                "INSERT INTO merchants (merchant_id, display_name,"
                " settlement_account_reference, status, created_at)"
                " VALUES (?, ?, ?, 'active', ?) ON CONFLICT (merchant_id) DO NOTHING",
                (merchant_id, display_name, settlement_account_reference, created_at),
            )
            if inserted.rowcount == 0:
                raise ValueError(f"merchant {merchant_id} already exists")
        return {
            "merchantId": merchant_id,
            "displayName": display_name,
            "settlementAccountReference": settlement_account_reference,
            "status": "active",
            "createdAt": created_at,
        }

    def get_merchant(self, merchant_id: str) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,)
            ).fetchone()

    # -- API credentials ---------------------------------------------------

    def create_api_credential(
        self,
        *,
        credential_id: str,
        operator_id: str | None,
        merchant_id: str | None,
        label: str,
        key_prefix: str,
        key_hash: str,
        scopes: str,
    ) -> dict:
        """Stores the HASH of a newly issued API key.

        The plaintext never reaches this method. It is generated, hashed, and
        returned to its owner by the endpoint; only the digest is persisted,
        so this table is not a credential store an attacker can use.

        Raises:
            ValueError: If neither or both tenant ids are given. A credential
                that belongs to no tenant would authenticate as nobody, and
                one belonging to two would be a cross-tenant hole by
                construction — so this is refused here rather than left to a
                CHECK constraint that would have to differ per dialect.
        """
        if bool(operator_id) == bool(merchant_id):
            raise ValueError(
                "an API credential must belong to exactly one tenant: "
                "give operator_id or merchant_id, not neither and not both"
            )

        created_at = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO api_credentials (credential_id, operator_id, merchant_id, label,"
                " key_prefix, key_hash, scopes, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    credential_id,
                    operator_id,
                    merchant_id,
                    label,
                    key_prefix,
                    key_hash,
                    scopes,
                    created_at,
                ),
            )
        return {
            "credentialId": credential_id,
            "operatorId": operator_id,
            "merchantId": merchant_id,
            "label": label,
            "keyPrefix": key_prefix,
            "scopes": scopes.split(),
            "status": "active",
            "createdAt": created_at,
        }

    def find_api_credential(self, key_hash: str) -> Mapping[str, Any] | None:
        """Looks up an active credential by hash.

        Filtering `status = 'active'` in SQL rather than in Python means a
        revoked key cannot be authenticated by a caller that forgets to check
        — the row simply is not returned.
        """
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM api_credentials WHERE key_hash = ? AND status = 'active'",
                (key_hash,),
            ).fetchone()

    def touch_api_credential(self, credential_id: str) -> None:
        """Records last use, for the "is this key still needed?" question.

        Best-effort and deliberately swallowed: a failure to update a
        timestamp must never fail an otherwise-valid authenticated request.
        """
        try:
            with _WRITE_LOCK, self._connect() as conn:
                conn.execute(
                    "UPDATE api_credentials SET last_used_at = ? WHERE credential_id = ?",
                    (utc_now(), credential_id),
                )
        except Exception as exc:  # noqa: BLE001 - never fail a request over telemetry
            self.log_event(
                "credential_touch_failed",
                status="degraded",
                credentialId=credential_id,
                error=redact_credentials(str(exc)),
            )

    def revoke_api_credential(
        self, *, credential_id: str, tenant_id: str, replaced_by: str | None = None
    ) -> bool:
        """Revokes a key, but only one belonging to `tenant_id`.

        The tenant predicate is inside the UPDATE, not checked before it. A
        read-then-write would let a caller revoke another tenant's credential
        if the check and the write ever drifted apart; here the database
        refuses, and `rowcount == 0` is indistinguishable from "no such key",
        which is also the right thing to tell a caller asking about somebody
        else's credential.
        """
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE api_credentials SET status = 'revoked', revoked_at = ?, replaced_by = ?"
                " WHERE credential_id = ? AND status = 'active'"
                "   AND (operator_id = ? OR merchant_id = ?)",
                (utc_now(), replaced_by, credential_id, tenant_id, tenant_id),
            )
            return result.rowcount > 0

    def list_api_credentials(self, *, tenant_id: str) -> list[dict]:
        """Every credential for one tenant. Hashes are never returned."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT credential_id, operator_id, merchant_id, label, key_prefix, scopes,"
                " status, created_at, last_used_at, revoked_at, replaced_by"
                " FROM api_credentials WHERE operator_id = ? OR merchant_id = ?"
                " ORDER BY created_at DESC",
                (tenant_id, tenant_id),
            ).fetchall()
        return [
            {
                "credentialId": r["credential_id"],
                "label": r["label"],
                "keyPrefix": r["key_prefix"],
                "scopes": (r["scopes"] or "").split(),
                "status": r["status"],
                "createdAt": r["created_at"],
                "lastUsedAt": r["last_used_at"],
                "revokedAt": r["revoked_at"],
                "replacedBy": r["replaced_by"],
            }
            for r in rows
        ]

    # -- agent signing credentials ----------------------------------------

    def create_enrollment_challenge(
        self, *, challenge_id: str, operator_id: str, agent_id: str, nonce: str, ttl_seconds: int
    ) -> dict:
        """Issues a single-use nonce for proof-of-possession enrollment."""
        now = datetime.now(UTC)
        expires_at = (
            (now + timedelta(seconds=ttl_seconds))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        created_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO enrollment_challenges (challenge_id, operator_id, agent_id, nonce,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (challenge_id, operator_id, agent_id, nonce, expires_at, created_at),
            )
        return {
            "challengeId": challenge_id,
            "agentId": agent_id,
            "nonce": nonce,
            "expiresAt": expires_at,
        }

    def consume_enrollment_challenge(
        self, *, challenge_id: str, operator_id: str
    ) -> Mapping[str, Any] | None:
        """Claims a challenge exactly once, atomically.

        The conditional UPDATE is the whole mechanism, for the same reason it
        is in `create_commitment`: a SELECT-then-UPDATE lets two concurrent
        enrollments both observe an unconsumed challenge and both proceed. A
        replayable challenge is not a challenge.

        Returns the claimed row, or None if it was already used, expired, or
        belongs to another operator.
        """
        with _WRITE_LOCK, self._connect() as conn:
            claimed = conn.execute(
                "UPDATE enrollment_challenges SET consumed_at = ?"
                " WHERE challenge_id = ? AND operator_id = ?"
                "   AND consumed_at IS NULL AND expires_at > ?",
                (utc_now(), challenge_id, operator_id, utc_now()),
            )
            if claimed.rowcount == 0:
                return None
            return conn.execute(
                "SELECT * FROM enrollment_challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()

    def enroll_agent_credential(
        self,
        *,
        credential_id: str,
        agent_id: str,
        operator_id: str | None,
        public_key: str,
        algorithm: str,
        valid_until: str | None = None,
        rotate: bool = False,
    ) -> dict:
        """Binds a signing key to an agent, under an operator.

        Two rows move together and must not diverge: `agents` carries the
        currently-active key (read on the hot path of every paid request) and
        `agent_credentials` carries the full lifecycle. Both are written in
        one transaction.

        `rotate=True` supersedes the current credential and installs the new
        one. Without it, binding a *different* key to an existing agent is
        refused — which is what keeps key rotation and account takeover from
        being the same request. Re-enrolling the identical key is a no-op, so
        an agent that restarts and re-announces itself does not have to know
        whether it has enrolled before.

        Raises:
            ValueError: On a rebinding attempt without `rotate`, or when the
                agent belongs to a different operator.
        """
        now = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()

            if existing is not None:
                # An agent already claimed by another operator is never
                # re-bindable. Without this, enrolling a key for someone
                # else's agent id would be a takeover with extra steps.
                current_operator = existing["operator_id"]
                if (
                    current_operator is not None
                    and operator_id is not None
                    and current_operator != operator_id
                ):
                    raise ValueError(
                        f"agent {agent_id} belongs to operator {current_operator}"
                    )

                if existing["public_key"] == public_key and not rotate:
                    return {
                        "agentId": agent_id,
                        "credentialId": credential_id,
                        "publicKey": public_key,
                        "algorithm": existing["algorithm"],
                        "operatorId": current_operator or operator_id,
                        "created": False,
                        "rotated": False,
                    }

                if not rotate:
                    raise ValueError(
                        f"agent {agent_id} is already registered with a different public key"
                    )

                # Supersede — never delete. A revoked or replaced credential
                # has to stay readable, or acceptances it signed stop being
                # verifiable and the audit trail loses exactly the evidence
                # that made Ed25519 worth using.
                conn.execute(
                    "UPDATE agent_credentials SET status = 'superseded', replaced_by = ?"
                    " WHERE agent_id = ? AND status = 'active'",
                    (credential_id, agent_id),
                )
                conn.execute(
                    "UPDATE agents SET public_key = ?, algorithm = ?, operator_id ="
                    " COALESCE(operator_id, ?) WHERE agent_id = ?",
                    (public_key, algorithm, operator_id, agent_id),
                )
            else:
                conn.execute(
                    "INSERT INTO agents (agent_id, public_key, algorithm, registered_at,"
                    " operator_id, status) VALUES (?, ?, ?, ?, ?, 'active')",
                    (agent_id, public_key, algorithm, now, operator_id),
                )

            conn.execute(
                "INSERT INTO agent_credentials (credential_id, agent_id, algorithm, public_key,"
                " status, valid_from, valid_until, created_at)"
                " VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (credential_id, agent_id, algorithm, public_key, now, valid_until, now),
            )

        return {
            "agentId": agent_id,
            "credentialId": credential_id,
            "publicKey": public_key,
            "algorithm": algorithm,
            "operatorId": operator_id,
            "validUntil": valid_until,
            "created": existing is None,
            "rotated": existing is not None,
        }

    def get_active_agent_credential(self, agent_id: str) -> Mapping[str, Any] | None:
        """The agent's currently-usable signing key, or None.

        Validity is evaluated in SQL — status active, and inside its window.
        An expired or revoked credential is simply not returned, so a caller
        that forgets to check cannot accidentally authorize against one.
        """
        now = utc_now()
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM agent_credentials"
                " WHERE agent_id = ? AND status = 'active'"
                "   AND valid_from <= ?"
                "   AND (valid_until IS NULL OR valid_until > ?)"
                " ORDER BY created_at DESC",
                (agent_id, now, now),
            ).fetchone()

    def list_agent_credentials(self, agent_id: str) -> list[dict]:
        """Full credential history for an agent, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_credentials WHERE agent_id = ? ORDER BY created_at DESC",
                (agent_id,),
            ).fetchall()
        return [
            {
                "credentialId": r["credential_id"],
                "algorithm": r["algorithm"],
                "publicKey": r["public_key"],
                "status": r["status"],
                "validFrom": r["valid_from"],
                "validUntil": r["valid_until"],
                "revokedAt": r["revoked_at"],
                "replacedBy": r["replaced_by"],
            }
            for r in rows
        ]

    def revoke_agent_credential(self, *, credential_id: str, agent_id: str) -> bool:
        """Revokes one signing credential.

        Revocation stops *new* authorization. It does not rewrite history: the
        row stays, so a signature produced while the key was valid remains
        verifiable against it. If the revoked credential was the active one,
        `agents.public_key` is left in place but
        `get_active_agent_credential` will now return None — which is what
        actually gates authorization.
        """
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE agent_credentials SET status = 'revoked', revoked_at = ?"
                " WHERE credential_id = ? AND agent_id = ? AND status != 'revoked'",
                (utc_now(), credential_id, agent_id),
            )
            return result.rowcount > 0

    def set_agent_status(self, *, agent_id: str, status: str, operator_id: str) -> bool:
        """Suspends or resumes an agent, scoped to its owning operator."""
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE agents SET status = ? WHERE agent_id = ? AND operator_id = ?",
                (status, agent_id, operator_id),
            )
            return result.rowcount > 0

    def list_agents_for_operator(self, operator_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, status, registered_at, algorithm FROM agents"
                " WHERE operator_id = ? ORDER BY registered_at DESC",
                (operator_id,),
            ).fetchall()
        return [
            {
                "agentId": r["agent_id"],
                "status": r["status"],
                "registeredAt": r["registered_at"],
                "algorithm": r["algorithm"],
            }
            for r in rows
        ]

    # -- spending consents -------------------------------------------------

    def create_consent(
        self,
        *,
        consent_id: str,
        operator_id: str,
        agent_id: str,
        per_request_limit_paise: int,
        daily_limit_paise: int,
        total_limit_paise: int,
        valid_from: str | None = None,
        valid_until: str | None = None,
        merchant_ids: list[str] | None = None,
        currency: str = "INR",
    ) -> dict:
        """Records an operator's authorisation for an agent to spend.

        Limits are integer paise. A limit of 0 means "no limit configured for
        this dimension", which is why every caller checks `> 0` before
        enforcing — 0 as "spend nothing" would make an unset field silently
        deny everything.
        """
        now = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO spending_consents (consent_id, operator_id, agent_id, status,"
                " currency, per_request_limit_paise, daily_limit_paise, total_limit_paise,"
                " reserved_paise, consumed_paise, valid_from, valid_until, created_at)"
                " VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 0, 0, ?, ?, ?)",
                (
                    consent_id,
                    operator_id,
                    agent_id,
                    currency,
                    int(per_request_limit_paise),
                    int(daily_limit_paise),
                    int(total_limit_paise),
                    valid_from or now,
                    valid_until,
                    now,
                ),
            )
            for merchant_id in merchant_ids or []:
                conn.execute(
                    "INSERT INTO consent_publishers (consent_id, merchant_id) VALUES (?, ?)"
                    " ON CONFLICT (consent_id, merchant_id) DO NOTHING",
                    (consent_id, merchant_id),
                )

        return {
            "consentId": consent_id,
            "operatorId": operator_id,
            "agentId": agent_id,
            "status": "active",
            "currency": currency,
            "perRequestLimitPaise": int(per_request_limit_paise),
            "dailyLimitPaise": int(daily_limit_paise),
            "totalLimitPaise": int(total_limit_paise),
            "reservedPaise": 0,
            "consumedPaise": 0,
            "validFrom": valid_from or now,
            "validUntil": valid_until,
            "merchantIds": sorted(merchant_ids or []),
            "createdAt": now,
        }

    def get_consent(self, consent_id: str) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM spending_consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()

    def active_consent_for_agent(self, agent_id: str) -> Mapping[str, Any] | None:
        """The consent an agent's spend should be authorised against.

        Validity is filtered in SQL for the same reason as agent credentials:
        an expired consent should not be *returned* and then hopefully
        rejected downstream. Newest first, so re-consenting supersedes in
        practice without needing an explicit swap.
        """
        now = utc_now()
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM spending_consents"
                " WHERE agent_id = ? AND status = 'active'"
                "   AND valid_from <= ?"
                "   AND (valid_until IS NULL OR valid_until > ?)"
                " ORDER BY created_at DESC",
                (agent_id, now, now),
            ).fetchone()

    def consent_merchant_scope(self, consent_id: str) -> frozenset[str]:
        """Merchants a consent is restricted to. Empty means unrestricted."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT merchant_id FROM consent_publishers WHERE consent_id = ?", (consent_id,)
            ).fetchall()
        return frozenset(r["merchant_id"] for r in rows)

    def set_consent_status(self, *, consent_id: str, operator_id: str, status: str) -> bool:
        """Suspends, resumes, or revokes a consent.

        Scoped to the owning operator inside the UPDATE — one operator must
        not be able to revoke another's authorisation, and the predicate being
        in the write rather than a preceding read is what guarantees it.

        Revocation is terminal: a revoked consent cannot be resumed, because
        "resume" on a withdrawn authorisation is a new authorisation and
        should be an explicit new consent with its own audit row.
        """
        revoked_at = utc_now() if status == "revoked" else None
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE spending_consents SET status = ?,"
                " revoked_at = COALESCE(?, revoked_at)"
                " WHERE consent_id = ? AND operator_id = ? AND status != 'revoked'",
                (status, revoked_at, consent_id, operator_id),
            )
            return result.rowcount > 0

    def list_consents(self, *, operator_id: str, agent_id: str | None = None) -> list[dict]:
        """Consents belonging to one operator, optionally narrowed to an agent."""
        sql = "SELECT * FROM spending_consents WHERE operator_id = ?"
        params: list[Any] = [operator_id]
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY created_at DESC"

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "consentId": r["consent_id"],
                "operatorId": r["operator_id"],
                "agentId": r["agent_id"],
                "status": r["status"],
                "currency": r["currency"],
                "perRequestLimitPaise": r["per_request_limit_paise"],
                "dailyLimitPaise": r["daily_limit_paise"],
                "totalLimitPaise": r["total_limit_paise"],
                "reservedPaise": r["reserved_paise"],
                "consumedPaise": r["consumed_paise"],
                "validFrom": r["valid_from"],
                "validUntil": r["valid_until"],
                "revokedAt": r["revoked_at"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ]

    # -- authority accounts and reservations -------------------------------

    def create_authority_account(
        self,
        *,
        account_id: str,
        consent_id: str,
        operator_id: str,
        backing: str,
        funded_paise: int = 0,
        credit_limit_paise: int = 0,
    ) -> dict:
        """Opens the balance a consent draws against.

        `funded_paise` is credited to `available_paise` at creation, which is
        what makes the invariant hold from the first row:

            funded = available + reserved + captured - refunded
        """
        now = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO authority_accounts (account_id, consent_id, operator_id, backing,"
                " funded_paise, available_paise, reserved_paise, captured_paise, refunded_paise,"
                " credit_limit_paise, overdue_paise, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, 0, 'active', ?, ?)",
                (
                    account_id,
                    consent_id,
                    operator_id,
                    backing,
                    int(funded_paise),
                    int(funded_paise),
                    int(credit_limit_paise),
                    now,
                    now,
                ),
            )
        return {
            "accountId": account_id,
            "consentId": consent_id,
            "operatorId": operator_id,
            "backing": backing,
            "fundedPaise": int(funded_paise),
            "availablePaise": int(funded_paise),
            "creditLimitPaise": int(credit_limit_paise),
        }

    def get_authority_account(self, *, consent_id: str) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM authority_accounts WHERE consent_id = ?", (consent_id,)
            ).fetchone()

    def fund_authority_account(self, *, account_id: str, amount_paise: int) -> bool:
        """Tops up a prefunded or simulated-reserve balance.

        Test mode only. This is not a received payment — it is an operator
        control-plane call that says "assume this much is behind the consent".
        Both counters move together so the invariant is never briefly false.
        """
        if amount_paise <= 0:
            raise ValueError("top-up must be a positive integer paise amount")
        with _WRITE_LOCK, self._connect() as conn:
            result = conn.execute(
                "UPDATE authority_accounts"
                "   SET funded_paise = funded_paise + ?,"
                "       available_paise = available_paise + ?,"
                "       updated_at = ?"
                " WHERE account_id = ? AND status = 'active'",
                (int(amount_paise), int(amount_paise), utc_now(), account_id),
            )
            return result.rowcount > 0

    def reserve_authority(
        self,
        *,
        reservation_id: str,
        account_id: str,
        consent_id: str,
        agent_id: str,
        offer_id: str,
        amount_paise: int,
        ttl_seconds: int,
        backing: str,
        credit_limit_paise: int,
    ) -> Mapping[str, Any]:
        """Holds `amount_paise` against an account, atomically.

        THE ONE THING THIS FUNCTION EXISTS TO GET RIGHT: the availability
        check is inside the UPDATE's WHERE clause, never a preceding SELECT.

            UPDATE ... WHERE account_id = ? AND available_paise >= ?

        `WHERE available_paise >= ?` takes a row lock in both SQLite and
        Postgres, and only one of two concurrent transactions can ever observe
        `rowcount == 1`. The loser sees `0` and is refused. A read-then-write
        would let both pass, and a transaction would not help — it serialises
        the writes, not the decision.

        Credit-backed accounts have no balance to draw down, so the ceiling is
        checked against the limit instead, by the same mechanism: a predicate
        in the WHERE clause rather than a decision made beforehand.

        Idempotent on `offer_id`, which is UNIQUE: a retried request finds its
        existing reservation rather than holding a second amount.

        Returns:
            The reservation row.

        Raises:
            AuthorityError: If there is not enough authority, or the account
                is not active.
        """
        from authority import CREDIT, AuthorityError

        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        expires_at = (
            (now + timedelta(seconds=ttl_seconds))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        amount = int(amount_paise)

        with _WRITE_LOCK, self._connect() as conn:
            # Idempotency first. `ON CONFLICT DO NOTHING` rather than a
            # try/except INSERT: in Postgres a failed statement aborts the
            # whole transaction and the recovery SELECT would itself be
            # refused. SQLite hides that completely, which is exactly why the
            # suite runs on both.
            claimed = conn.execute(
                "INSERT INTO reservations (reservation_id, account_id, consent_id, agent_id,"
                " offer_id, amount_paise, status, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'held', ?, ?)"
                " ON CONFLICT (offer_id) DO NOTHING",
                (
                    reservation_id,
                    account_id,
                    consent_id,
                    agent_id,
                    offer_id,
                    amount,
                    created_at,
                    expires_at,
                ),
            )
            if claimed.rowcount == 0:
                existing = conn.execute(
                    "SELECT * FROM reservations WHERE offer_id = ?", (offer_id,)
                ).fetchone()
                if existing is not None:
                    return existing
                raise AuthorityError(
                    "reservation_failed",
                    f"could not reserve against offer {offer_id}",
                )

            if backing == CREDIT:
                # No balance to draw down. The ceiling is the limit minus what
                # is already held or captured, evaluated in the same statement
                # that increments the hold.
                moved = conn.execute(
                    "UPDATE authority_accounts"
                    "   SET reserved_paise = reserved_paise + ?, updated_at = ?"
                    " WHERE account_id = ? AND status = 'active'"
                    "   AND (reserved_paise + captured_paise - refunded_paise + ?)"
                    "       <= credit_limit_paise",
                    (amount, utc_now(), account_id, amount),
                )
            else:
                moved = conn.execute(
                    "UPDATE authority_accounts"
                    "   SET available_paise = available_paise - ?,"
                    "       reserved_paise = reserved_paise + ?,"
                    "       updated_at = ?"
                    " WHERE account_id = ? AND status = 'active' AND available_paise >= ?",
                    (amount, amount, utc_now(), account_id, amount),
                )

            if moved.rowcount == 0:
                # The hold did not happen, so the reservation row must not
                # survive. Deleting a row this transaction itself inserted a
                # moment ago is not destroying history — nothing outside this
                # transaction has ever seen it.
                conn.execute(
                    "DELETE FROM reservations WHERE reservation_id = ?", (reservation_id,)
                )
                account = conn.execute(
                    "SELECT * FROM authority_accounts WHERE account_id = ?", (account_id,)
                ).fetchone()
                if account is None:
                    raise AuthorityError(
                        "no_authority_account",
                        f"consent {consent_id} has no authority account",
                    )
                if account["status"] != "active":
                    raise AuthorityError(
                        "authority_account_inactive",
                        f"authority account is {account['status']}",
                        accountStatus=account["status"],
                    )
                if backing == CREDIT:
                    used = (
                        account["reserved_paise"]
                        + account["captured_paise"]
                        - account["refunded_paise"]
                    )
                    raise AuthorityError(
                        "credit_limit_exceeded",
                        f"{amount} paise would take exposure to {used + amount} paise, "
                        f"over the credit limit of {credit_limit_paise} paise",
                        exposurePaise=used,
                        creditLimitPaise=credit_limit_paise,
                    )
                raise AuthorityError(
                    "insufficient_authority",
                    f"{amount} paise exceeds the {account['available_paise']} paise "
                    "available. Top up the authority account, or fetch something cheaper.",
                    availablePaise=account["available_paise"],
                    amountPaise=amount,
                )

            return conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
            ).fetchone()

    def capture_reservation(
        self, *, reservation_id: str, commitment_id: str
    ) -> bool:
        """Converts a held reservation into captured usage, exactly once.

        Called after the content was actually delivered. The conditional
        `WHERE status = 'held'` is what makes it exactly-once: a second call
        moves no rows and returns False, so a retried settlement cannot
        capture the same authority twice.

        Both the reservation and the account move in one transaction, so the
        invariant `funded = available + reserved + captured - refunded` is
        never observably violated.
        """
        now = utc_now()
        with _WRITE_LOCK, self._connect() as conn:
            claimed = conn.execute(
                "UPDATE reservations"
                "   SET status = 'captured', resolved_at = ?, commitment_id = ?"
                " WHERE reservation_id = ? AND status = 'held'",
                (now, commitment_id, reservation_id),
            )
            if claimed.rowcount == 0:
                return False

            row = conn.execute(
                "SELECT account_id, amount_paise FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            conn.execute(
                "UPDATE authority_accounts"
                "   SET reserved_paise = reserved_paise - ?,"
                "       captured_paise = captured_paise + ?,"
                "       updated_at = ?"
                " WHERE account_id = ?",
                (row["amount_paise"], row["amount_paise"], now, row["account_id"]),
            )
            return True

    def release_reservation(self, *, reservation_id: str, reason: str = "released") -> bool:
        """Returns held authority when fulfillment did not happen.

        The counterpart to capture, and the reason a failed request does not
        silently consume an agent's balance for the rest of the day.
        """
        now = utc_now()
        status = "expired" if reason == "expired" else "released"
        with _WRITE_LOCK, self._connect() as conn:
            claimed = conn.execute(
                "UPDATE reservations SET status = ?, resolved_at = ?"
                " WHERE reservation_id = ? AND status = 'held'",
                (status, now, reservation_id),
            )
            if claimed.rowcount == 0:
                return False

            row = conn.execute(
                "SELECT account_id, amount_paise FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            conn.execute(
                "UPDATE authority_accounts"
                "   SET available_paise = available_paise + ?,"
                "       reserved_paise = reserved_paise - ?,"
                "       updated_at = ?"
                " WHERE account_id = ?",
                (row["amount_paise"], row["amount_paise"], now, row["account_id"]),
            )
            return True

    def get_reservation_by_offer(self, offer_id: str) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM reservations WHERE offer_id = ?", (offer_id,)
            ).fetchone()

    def expire_stale_reservations(self, *, now: str | None = None) -> int:
        """Sweeps held reservations past their TTL, returning the authority.

        Without this, a request that dies between reserving and settling
        strands an agent's balance until someone notices. Returns how many
        were swept, so a scheduler can report it rather than sweeping
        silently.
        """
        cutoff = now or utc_now()
        with self._connect() as conn:
            stale = conn.execute(
                "SELECT reservation_id FROM reservations"
                " WHERE status = 'held' AND expires_at <= ?",
                (cutoff,),
            ).fetchall()

        swept = 0
        for row in stale:
            if self.release_reservation(
                reservation_id=row["reservation_id"], reason="expired"
            ):
                swept += 1
        return swept

    def committed_against_consent(self, *, consent_id: str, settle_date: str) -> int:
        """What has already been committed under one consent, on one date.

        The daily-limit input. Scoped to the consent rather than the agent so
        that re-consenting starts a fresh daily window, which is the behaviour
        an operator issuing a new authorisation would expect.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT CAST(COALESCE(SUM(amount_paise), 0) AS BIGINT) AS total"
                " FROM commitments WHERE consent_id = ? AND settle_date = ?"
                "   AND status != 'failed'",
                (consent_id, settle_date),
            ).fetchone()
        return int(row["total"]) if row else 0

    # -- offers ------------------------------------------------------------

    def insert_offer(self, offer: dict, signature: str) -> None:
        """Stores a freshly quoted offer.

        Args:
            offer: The offer body that was signed.
            signature: HMAC over that body.
        """
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO offers (offer_id, agent_id, resource_id, resource_url, amount_paise,"
                " asset, scheme, network, pay_to, nonce, signature, issued_at, expires_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (
                    offer["offerId"],
                    offer["agentId"],
                    offer["resourceId"],
                    offer.get("resourceUrl"),
                    int(offer["amountPaise"]),
                    offer["asset"],
                    offer["scheme"],
                    offer["network"],
                    offer["payTo"],
                    offer["nonce"],
                    signature,
                    offer["issuedAt"],
                    offer["expiresAt"],
                ),
            )

    def get_offer(self, offer_id: str) -> Mapping[str, Any] | None:
        """Looks up one offer, or None."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,))
            return cur.fetchone()

    def recent_offer_count(self, *, agent_id: str, within_seconds: int = 60) -> int:
        """How many quotes an agent has been issued recently.

        Counts `offers.issued_at` rather than reading the `events` table: the
        offers row is the thing that was actually created, and it is the table
        the rate limit is trying to protect from growing without bound.

        String comparison on ISO-8601 UTC timestamps, which sort
        lexicographically — the reason `utc_now` renders them that way, and
        why this needs no date functions and behaves identically in both
        engines.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=within_seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM offers WHERE agent_id = ? AND issued_at >= ?",
                (agent_id, cutoff),
            ).fetchone()
        return int(row["n"])

    def committed_today(self, *, agent_id: str, settle_date: str | None = None) -> int:
        """What an agent has already committed for a settlement date, in paise.

        Every commitment regardless of status, because a settled one is still
        money the agent owes or has paid — excluding it would let an agent
        reset its own cap by triggering a batch run.
        """
        settle_date = settle_date or today_utc()
        with self._connect() as conn:
            return self._committed_in(conn, agent_id=agent_id, settle_date=settle_date)

    @staticmethod
    def _committed_in(conn: db.Conn, *, agent_id: str, settle_date: str) -> int:
        """The same sum, on a connection the caller already holds.

        Exists so the refusal path inside `create_commitment` can report the
        current total without opening a second connection mid-transaction —
        which under Postgres would read outside the transaction it is about to
        roll back, and report a number that never existed.
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM commitments"
            " WHERE agent_id = ? AND settle_date = ?",
            (agent_id, settle_date),
        ).fetchone()
        return int(row["total"])

    # -- commitments -------------------------------------------------------

    def get_commitment_by_offer(self, offer_id: str) -> Mapping[str, Any] | None:
        """Finds the commitment an offer already produced, if any.

        Used to make settlement idempotent: a retried /settle returns the
        original commitment rather than creating a second debt.
        """
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM commitments WHERE offer_id = ?", (offer_id,))
            return cur.fetchone()

    def create_commitment(
        self,
        *,
        commitment_id: str,
        offer_id: str,
        agent_id: str,
        resource_id: str,
        amount_paise: int,
        asset: str,
        mode: str,
        settle_date: str | None = None,
        daily_cap_paise: int = UNLIMITED_PAISE,
        operator_id: str | None = None,
        consent_id: str | None = None,
    ) -> dict:
        """Turns an open offer into a commitment, atomically.

        "Atomically" here does not mean "inside one transaction" — a
        transaction alone does not prevent two concurrent callers both
        reading `status = 'open'` before either has written, and both then
        proceeding to insert a commitment. It means the offer is claimed with
        a single conditional UPDATE:

            UPDATE offers SET status = 'consumed' WHERE offer_id = ? AND status = 'open'

        `WHERE status = 'open'` takes a row lock in both engines, and only one
        concurrent transaction can ever observe `rowcount == 1` — the other
        sees `0` and takes the "already consumed" branch below. There is no
        read-then-write gap to lose a race in. The UNIQUE constraint on
        `commitments.offer_id` is a backstop behind this, not the mechanism
        doing the actual work; it only fires if the claim above is ever wrong,
        translated to `ValueError` via `db.UniqueConstraintError` so it stays
        indistinguishable from the ordinary "already spent" case to callers.

        This is also what makes `_WRITE_LOCK` unnecessary for correctness —
        it was, before this method looked like this, the only thing standing
        between SQLite and a real double-spend under FastAPI's threadpool.

        Args:
            commitment_id: Id to assign.
            offer_id: The offer being spent.
            agent_id: Who owes the money.
            resource_id: What they bought.
            amount_paise: How much, in paise.
            asset: Currency code.
            mode: "deferred" or "per_request".
            settle_date: Batching key; defaults to today UTC.
            daily_cap_paise: Most this agent may have committed for
                `settle_date`, enforced inside the INSERT below. Defaults to
                effectively unlimited so every existing caller — the tests,
                the CLI flows — behaves exactly as before.

        Returns:
            The created commitment as a dict.

        Raises:
            ValueError: If the offer is missing or already spent.
            DailyCapExceeded: If booking this would take the agent over its
                cap. The transaction rolls back, so the offer stays spendable.
        """
        settle_date = settle_date or today_utc()
        created_at = utc_now()

        with _WRITE_LOCK, self._connect() as conn:
            claimed = conn.execute(
                "UPDATE offers SET status = 'consumed' WHERE offer_id = ? AND status = 'open'",
                (offer_id,),
            )
            if claimed.rowcount == 0:
                row = conn.execute(
                    "SELECT status FROM offers WHERE offer_id = ?", (offer_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"offer {offer_id} does not exist")
                raise ValueError(f"offer {offer_id} is already {row['status']}")

            try:
                # `INSERT ... SELECT ... WHERE <cap not breached>` rather than
                # reading the total and then deciding, for the same reason the
                # offer above is claimed with a conditional UPDATE: two
                # concurrent settlements would both pass a separate SELECT and
                # both insert, landing the agent over its cap by one payment.
                # Folding the check into the INSERT makes that impossible —
                # the subquery is evaluated inside the same statement.
                #
                # A refusal raises, which rolls the whole transaction back
                # (see db.connect), so the offer claimed above returns to
                # 'open' and the agent can spend it tomorrow rather than
                # having it burned by hitting a limit.
                inserted = conn.execute(
                    "INSERT INTO commitments (commitment_id, offer_id, agent_id, resource_id,"
                    " amount_paise, asset, created_at, settle_date, mode, status,"
                    " operator_id, consent_id)"
                    " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?"
                    " WHERE COALESCE((SELECT SUM(amount_paise) FROM commitments"
                    "                 WHERE agent_id = ? AND settle_date = ?), 0) + ? <= ?",
                    (
                        commitment_id,
                        offer_id,
                        agent_id,
                        resource_id,
                        int(amount_paise),
                        asset,
                        created_at,
                        settle_date,
                        mode,
                        operator_id,
                        consent_id,
                        agent_id,
                        settle_date,
                        int(amount_paise),
                        int(daily_cap_paise),
                    ),
                )
                if inserted.rowcount == 0:
                    already = self._committed_in(
                        conn, agent_id=agent_id, settle_date=settle_date
                    )
                    raise DailyCapExceeded(
                        agent_id=agent_id,
                        settle_date=settle_date,
                        committed_paise=already,
                        amount_paise=int(amount_paise),
                        daily_cap_paise=int(daily_cap_paise),
                    )
            except db.UniqueConstraintError as exc:
                # Should be unreachable given the claim above — offer_id
                # UNIQUE is a backstop, not the primary guard. Kept because an
                # untested backstop is not really a backstop.
                raise ValueError(f"offer {offer_id} already has a commitment") from exc

        return {
            "commitmentId": commitment_id,
            "offerId": offer_id,
            "agentId": agent_id,
            "resourceId": resource_id,
            "amountPaise": int(amount_paise),
            "asset": asset,
            "createdAt": created_at,
            "settleDate": settle_date,
            "mode": mode,
            "status": "pending",
            # Null means nobody is on the hook for this receivable but a
            # pseudonymous key — the state the unsafe demo path produces, and
            # worth being able to see rather than inferring from an absence.
            "operatorId": operator_id,
            "consentId": consent_id,
        }

    def pending_commitments(
        self, *, agent_id: str | None = None, settle_date: str | None = None
    ) -> list[Mapping[str, Any]]:
        """Lists unsettled commitments, optionally narrowed to one agent or day."""
        query = "SELECT * FROM commitments WHERE status = 'pending'"
        params: list[Any] = []
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if settle_date:
            query += " AND settle_date = ?"
            params.append(settle_date)
        query += " ORDER BY created_at ASC"

        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def pending_agents(self, *, settle_date: str | None = None) -> list[str]:
        """Agent ids that currently owe something."""
        query = "SELECT DISTINCT agent_id FROM commitments WHERE status = 'pending'"
        params: list[Any] = []
        if settle_date:
            query += " AND settle_date = ?"
            params.append(settle_date)

        with self._connect() as conn:
            return [r["agent_id"] for r in conn.execute(query, params).fetchall()]

    # -- batches -----------------------------------------------------------

    def record_batch(
        self,
        *,
        batch_id: str,
        agent_id: str,
        settle_date: str,
        commitment_ids: list[str],
        total_paise: int,
        payment_link_id: str | None,
        payment_link_url: str | None,
        status: str,
        razorpay_mode: str,
        error_message: str | None = None,
        instrument: str = "payment_link",
        amount_paid_paise: int | None = None,
    ) -> None:
        """Writes the batch and marks its commitments settled, in one transaction.

        If the Razorpay call failed, the commitments stay `pending` so the next
        run picks them up again — a failed charge must never look like a paid
        one.

        Args:
            instrument: What settled it — "payment_link" or "reserve_pay".
            amount_paid_paise: Set only when the instrument settles inline. A
                Payment Link leaves this None until its webhook lands, because
                a created link is an invoice; a mandate debit has already
                taken the money by the time it returns, and there is no
                webhook to wait for. Passing it is what lets `collectedPaise`
                stay meaningful across both instruments.
        """
        settled_inline = amount_paid_paise is not None
        paid_at = utc_now() if settled_inline else None

        # An instrument that takes the money as it goes is `paid`, not
        # `created`. Skipping this would leave the batch carrying an
        # `amount_paid_paise` that `daily_summary` never counts, because that
        # query filters on `status = 'paid'` — so collected revenue would read
        # as zero while the money had genuinely arrived.
        stored_status = "paid" if settled_inline and status == "created" else status

        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO batches (batch_id, agent_id, settle_date, created_at,"
                " commitment_count, total_paise, payment_link_id, payment_link_url,"
                " status, razorpay_mode, error_message, instrument, amount_paid_paise,"
                " paid_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    agent_id,
                    settle_date,
                    utc_now(),
                    len(commitment_ids),
                    int(total_paise),
                    payment_link_id,
                    payment_link_url,
                    stored_status,
                    razorpay_mode,
                    error_message,
                    instrument,
                    amount_paid_paise,
                    paid_at,
                ),
            )

            if status == "created":
                conn.executemany(
                    "UPDATE commitments SET status = 'settled', batch_id = ?"
                    " WHERE commitment_id = ?",
                    [(batch_id, cid) for cid in commitment_ids],
                )
            elif status == "failed":
                # Leave them pending on purpose — see docstring.
                conn.executemany(
                    "UPDATE commitments SET status = 'pending' WHERE commitment_id = ?",
                    [(cid,) for cid in commitment_ids],
                )

    def debited_today(self, *, agent_id: str, settle_date: str | None = None) -> int:
        """What an agent's UPI Reserve Pay mandate has been drawn down by.

        A mandate authorises a block and is debited against repeatedly, so a
        debit needs to know what is left. That comes from here rather than a
        counter held in the gateway object, which on a serverless deployment
        would reset on every cold start and let the same block be spent twice.

        Counts only `reserve_pay` batches: a Payment Link draws on nothing.
        """
        settle_date = settle_date or today_utc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_paise), 0) AS total FROM batches"
                " WHERE agent_id = ? AND settle_date = ? AND instrument = 'reserve_pay'"
                " AND status <> 'failed'",
                (agent_id, settle_date),
            ).fetchone()
        return int(row["total"])

    def get_batch(self, batch_id: str) -> Mapping[str, Any] | None:
        """Looks up one batch, or None."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()

    def get_batch_by_link(self, payment_link_id: str) -> Mapping[str, Any] | None:
        """Finds the batch a Razorpay Payment Link belongs to.

        The lookup a webhook needs: Razorpay tells us about a `plink_...`, and
        we have to map it back to the commitments it covers.
        """
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM batches WHERE payment_link_id = ?", (payment_link_id,)
            ).fetchone()

    # -- webhooks ----------------------------------------------------------

    def claim_webhook(
        self, *, dedupe_key: str, event: str, payment_link_id: str | None
    ) -> bool:
        """Claims a webhook delivery for processing, exactly once.

        Razorpay retries a failed delivery with exponential backoff for 24
        hours, so redelivery is routine rather than exceptional — and a
        redelivered `payment_link.paid` that gets
        applied twice would double-count collected revenue. The guard is a
        primary-key INSERT rather than a "have we seen this?" SELECT: two
        concurrent deliveries of the same event both pass a SELECT check, and
        only one can win an INSERT.

        Args:
            dedupe_key: Stable identifier for this delivery.
            event: Razorpay event name.
            payment_link_id: The link this concerns, when there is one.

        Returns:
            True if this caller now owns the delivery, False if it was already
            processed and should be acknowledged without further action.
        """
        with _WRITE_LOCK, self._connect() as conn:
            # Same `ON CONFLICT DO NOTHING` as `register_agent`, and for the
            # same reason: a caught unique violation leaves a Postgres
            # transaction aborted, so nothing else can run on that connection
            # afterwards. Nothing does here *today*, which is the trap — the
            # next person to add a statement below this line would find it
            # failing for a reason that has nothing to do with what they wrote.
            claimed = conn.execute(
                "INSERT INTO webhook_events"
                " (dedupe_key, event, received_at, payment_link_id, outcome)"
                " VALUES (?, ?, ?, ?, 'claimed') ON CONFLICT (dedupe_key) DO NOTHING",
                (dedupe_key, event, utc_now(), payment_link_id),
            )
            return claimed.rowcount == 1

    def finish_webhook(self, *, dedupe_key: str, outcome: str, **detail: Any) -> None:
        """Records how a claimed webhook delivery turned out."""
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "UPDATE webhook_events SET outcome = ?, detail = ? WHERE dedupe_key = ?",
                (
                    outcome,
                    json.dumps(detail, sort_keys=True, default=str) if detail else None,
                    dedupe_key,
                ),
            )

    def mark_batch_paid(
        self,
        *,
        payment_link_id: str,
        amount_paid_paise: int,
        razorpay_payment_id: str | None,
    ) -> Mapping[str, Any] | None:
        """Marks a batch actually paid, on confirmation from Razorpay.

        This is the only place a batch becomes `paid`. The settlement run that
        created the link deliberately does not, because creating an invoice is
        not evidence that anyone settled it.

        Conditional on the batch not already being paid, for the same reason
        `create_commitment` claims its offer conditionally — the check and the
        write have to be one statement or a concurrent redelivery slips
        between them.

        Args:
            payment_link_id: Razorpay's `plink_...` id.
            amount_paid_paise: What Razorpay says arrived, in paise.
            razorpay_payment_id: The underlying `pay_...` id, when present.

        Returns:
            The updated batch row, or None if there is no such link or it was
            already marked paid.
        """
        with _WRITE_LOCK, self._connect() as conn:
            updated = conn.execute(
                "UPDATE batches SET status = 'paid', paid_at = ?, amount_paid_paise = ?,"
                " razorpay_payment_id = ?"
                " WHERE payment_link_id = ? AND status <> 'paid'",
                (utc_now(), int(amount_paid_paise), razorpay_payment_id, payment_link_id),
            )
            if updated.rowcount == 0:
                return None
            return conn.execute(
                "SELECT * FROM batches WHERE payment_link_id = ?", (payment_link_id,)
            ).fetchone()

    def release_batch(
        self, *, payment_link_id: str, status: str
    ) -> Mapping[str, Any] | None:
        """Voids an unpaid batch and returns its commitments to the queue.

        For `payment_link.expired` and `payment_link.cancelled`: the invoice
        will never be paid, but the debt behind it is still real. Detaching
        the commitments and setting them back to `pending` puts them in front
        of the next settlement run, which re-bills them on a fresh link.

        Deliberately refuses to touch a batch that is already `paid`. An
        `expired` event can legitimately arrive after a `paid` one — Razorpay
        expires the link on schedule regardless of whether it was used — and
        acting on it would un-collect money that was genuinely received.

        Args:
            payment_link_id: Razorpay's `plink_...` id.
            status: New batch status, "expired" or "cancelled".

        Returns:
            The updated batch row, or None if there is no such link or it was
            already paid or already in this state.
        """
        with _WRITE_LOCK, self._connect() as conn:
            updated = conn.execute(
                "UPDATE batches SET status = ?"
                " WHERE payment_link_id = ? AND status NOT IN ('paid', ?)",
                (status, payment_link_id, status),
            )
            if updated.rowcount == 0:
                return None

            batch = conn.execute(
                "SELECT * FROM batches WHERE payment_link_id = ?", (payment_link_id,)
            ).fetchone()
            conn.execute(
                "UPDATE commitments SET status = 'pending', batch_id = NULL WHERE batch_id = ?",
                (batch["batch_id"],),
            )
            return batch

    # -- reporting ---------------------------------------------------------

    def revenue_below(self, amount_paise: int, settle_date: str | None = None) -> dict:
        """Revenue made up of individually-too-small charges.

        The headline number in the publisher's report: money that per-request
        settlement could not have collected at any price, because each charge
        sits under the payment gateway's floor.

        Computed from the actual commitment rows rather than inferred from
        per-agent averages. An agent that fetches both a ₹5 report and forty
        ₹0.50 API calls has a mean above the floor while forty of its
        forty-one charges are below it, so an average would understate this
        badly.

        Args:
            amount_paise: The floor to compare against.
            settle_date: Day to inspect. Defaults to today UTC.

        Returns:
            `{"count": ..., "totalPaise": ...}` for commitments under the floor.
        """
        settle_date = settle_date or today_utc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(amount_paise), 0) AS total"
                " FROM commitments WHERE settle_date = ? AND amount_paise < ?",
                (settle_date, int(amount_paise)),
            ).fetchone()
        return {"count": row["count"], "totalPaise": row["total"]}

    def daily_summary(self, settle_date: str | None = None, agent_id: str | None = None) -> dict:
        """Aggregates one day's activity for the publisher's report.

        `agent_id` exists for the console UI: with several visitors hitting a
        shared deployment, an unfiltered summary is everyone's revenue mixed
        together, and an unfiltered `batches` list lets one visitor see (and
        via `/settle-batch`, sweep) another's pending commitments. Every
        caller that predates the console — `reporting/daily_summary.py`, the
        CI flow scripts, this method's own tests — passes no `agent_id` and
        sees exactly what they saw before.

        Args:
            settle_date: Day to summarise, YYYY-MM-DD. Defaults to today UTC.
            agent_id: Narrow to one agent. Omit for the whole day.

        Returns:
            Totals, per-agent breakdown, and the batches generated.
        """
        settle_date = settle_date or today_utc()
        agent_filter = " AND agent_id = ?" if agent_id else ""
        params: tuple = (settle_date, agent_id) if agent_id else (settle_date,)

        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS requests, COALESCE(SUM(amount_paise), 0) AS total_paise"
                f" FROM commitments WHERE settle_date = ?{agent_filter}",
                params,
            ).fetchone()

            by_agent = conn.execute(
                "SELECT agent_id, COUNT(*) AS requests,"
                " COALESCE(SUM(amount_paise), 0) AS total_paise,"
                " SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) AS settled,"
                " SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending"
                f" FROM commitments WHERE settle_date = ?{agent_filter}"
                " GROUP BY agent_id ORDER BY total_paise DESC",
                params,
            ).fetchall()

            by_resource = conn.execute(
                "SELECT resource_id, COUNT(*) AS requests,"
                " COALESCE(SUM(amount_paise), 0) AS total_paise"
                f" FROM commitments WHERE settle_date = ?{agent_filter}"
                " GROUP BY resource_id ORDER BY total_paise DESC",
                params,
            ).fetchall()

            batches = conn.execute(
                f"SELECT * FROM batches WHERE settle_date = ?{agent_filter}"
                " ORDER BY created_at ASC",
                params,
            ).fetchall()

            rejections = conn.execute(
                "SELECT COUNT(*) AS n FROM events"
                f" WHERE status = 'rejected' AND substr(ts, 1, 10) = ?{agent_filter}",
                params,
            ).fetchone()

            # Money Razorpay has actually confirmed, as distinct from money
            # that has been billed. Reads `amount_paid_paise` — what the
            # webhook reported arriving — rather than the batch total, so a
            # partial payment is not rounded up into a full one.
            collected = conn.execute(
                "SELECT COALESCE(SUM(amount_paid_paise), 0) AS collected,"
                " COUNT(*) AS paid_batches"
                f" FROM batches WHERE status = 'paid' AND settle_date = ?{agent_filter}",
                params,
            ).fetchone()

        return {
            "settleDate": settle_date,
            "agentId": agent_id,
            "requests": totals["requests"],
            "totalPaise": totals["total_paise"],
            # Explicit aliases for the two questions `totalPaise` alone is
            # ambiguous about. See the module docstring: billed is not banked.
            "committedPaise": totals["total_paise"],
            "collectedPaise": collected["collected"],
            "paidBatches": collected["paid_batches"],
            "rejectedPayments": rejections["n"],
            "byAgent": [dict(r) for r in by_agent],
            "byResource": [dict(r) for r in by_resource],
            "batches": [dict(r) for r in batches],
        }

    def commitment_amounts(
        self, *, agent_id: str | None = None, settle_date: str | None = None
    ) -> list[int]:
        """All commitment amounts for a day, regardless of status.

        Feeds `estimate_settlement_cost` for a *read-only* economics view.
        `/settle-batch` computes the same comparison but only ever sees
        commitments still `pending` — so a read built the same way would go
        blank the instant a batch clears, which is a strange way for a
        dashboard card to behave. This counts the whole day's traffic, settled
        or not.

        Args:
            agent_id: Narrow to one agent. Omit for every agent that day.
            settle_date: Day to inspect. Defaults to today UTC.

        Returns:
            One entry per commitment, in paise.
        """
        settle_date = settle_date or today_utc()
        query = "SELECT amount_paise FROM commitments WHERE settle_date = ?"
        params: list[Any] = [settle_date]
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)

        with self._connect() as conn:
            return [int(r["amount_paise"]) for r in conn.execute(query, params).fetchall()]

    def commitment_histogram(
        self, *, agent_id: str | None = None, settle_date: str | None = None
    ) -> list[dict]:
        """A day's charges grouped by size, cheapest first.

        Exists so the console can draw the argument instead of stating it: a
        bar per price point with the gateway minimum marked, where everything
        left of that line is revenue with no per-request path. The totals in
        `estimate_settlement_cost` say the same thing in prose, and a reader
        has to take them on faith.

        Grouped in SQL rather than by returning every amount, because this is
        a public read on a shared deployment and a day of agent traffic is
        thousands of rows the browser has no use for.

        Args:
            agent_id: Narrow to one agent. Omit for the whole day.
            settle_date: Day to inspect. Defaults to today UTC.

        Returns:
            `[{"amountPaise": ..., "count": ..., "totalPaise": ...}, ...]`.
        """
        settle_date = settle_date or today_utc()
        query = (
            "SELECT amount_paise, COUNT(*) AS n,"
            " COALESCE(SUM(amount_paise), 0) AS total"
            " FROM commitments WHERE settle_date = ?"
        )
        params: list[Any] = [settle_date]
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " GROUP BY amount_paise ORDER BY amount_paise ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            {
                "amountPaise": int(row["amount_paise"]),
                "count": int(row["n"]),
                "totalPaise": int(row["total"]),
            }
            for row in rows
        ]

    def list_events(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
        since_id: int | None = None,
    ) -> list[dict]:
        """Recent audit-log rows, newest first — the live feed for the console.

        `detail` is stored as a JSON string (see `log_event`); parsed back into
        an object here so a caller gets a normal value rather than a string to
        parse itself.

        Args:
            agent_id: Narrow to one agent's events.
            limit: Row cap, clamped to [1, 200] regardless of what is asked for
                — this is a public-facing read in the console's case, and
                nothing should be able to ask for the whole table in one call.
            since_id: Only rows with `id` greater than this. Lets a poller ask
                for "what's new" instead of re-fetching everything.

        Returns:
            Rows as camelCase dicts, `detail` already parsed.
        """
        query = (
            "SELECT id, ts, event, agent_id, resource_id, amount_paise, status, detail"
            " FROM events"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if since_id is not None:
            clauses.append("id > ?")
            params.append(since_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "event": row["event"],
                "agentId": row["agent_id"],
                "resourceId": row["resource_id"],
                "amountPaise": row["amount_paise"],
                "status": row["status"],
                "detail": json.loads(row["detail"]) if row["detail"] else {},
            }
            for row in rows
        ]
