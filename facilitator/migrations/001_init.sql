-- Bharat x402 — Postgres schema, initial migration.
--
-- Apply this once, by hand, in the Supabase SQL editor (or `psql`) before
-- pointing LEDGER_DSN at a Postgres database. It is not applied automatically
-- on startup — see the docstring on Ledger.__init__ in facilitator/ledger.py
-- for why: schema changes on a shared production database belong in a
-- migration run deliberately, not as a side effect of the facilitator
-- importing on a cold start.
--
-- (The one exception is CI, which sets LEDGER_AUTO_MIGRATE=1 to bootstrap a
-- throwaway postgres:16 service container that starts genuinely empty and
-- has no one to run this by hand.)
--
-- THIS FILE IS GENERATED, NOT HAND-MAINTAINED. It is the literal output of
-- `ledger.schema_sql("postgres")` — the same function that builds the SQLite
-- schema, so there is exactly one definition of the schema in the codebase,
-- not two that can quietly drift apart. If ledger.py's schema_sql changes,
-- regenerate this file rather than hand-editing it:
--
--   python -c "import sys; sys.path.insert(0, 'facilitator'); \
--     from ledger import schema_sql; print(schema_sql('postgres'))" \
--     > facilitator/migrations/001_init.sql
--   (then restore this header comment above the generated SQL)

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
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
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
