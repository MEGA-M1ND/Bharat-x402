-- Bharat x402 — migration 004: operators, credentials, and spending consent.
--
-- WHO NEEDS THIS: any database created before Phase 2. A database built from
-- the current 001_init.sql already has everything here — 001 is regenerated
-- from ledger.schema_sql(), so it is always the complete current schema rather
-- than a historical snapshot.
--
-- WHY 004 EXISTS ANYWAY: most of the changes below are new tables, which 001's
-- `CREATE TABLE IF NOT EXISTS` would happily create on a re-run. The added
-- COLUMNS are not. `CREATE TABLE IF NOT EXISTS agents` is a silent no-op
-- against an existing `agents` table, so re-running 001 on a deployed database
-- adds the new tables and quietly skips `agents.operator_id`,
-- `agents.status`, `commitments.operator_id` and `commitments.consent_id` —
-- leaving the consent code writing to columns that do not exist. Same trap as
-- migration 002. Hence explicit ALTERs.
--
-- WHAT THIS CHANGES, in one line: content used to be released because a
-- pseudonymous key signed a promise. After this, there is a party behind that
-- key, a consent bounding what it may spend, and a way to revoke both.
--
-- SAFE TO RUN MORE THAN ONCE: every statement is IF NOT EXISTS.
--
-- NON-DESTRUCTIVE: no existing row is modified and no column is dropped.
-- Existing agents get `operator_id = NULL`, which is the honest
-- representation of what they are — identities claimed under
-- trust-on-first-use, with nobody on the hook for them. Existing commitments
-- keep `operator_id`/`consent_id` NULL for the same reason. Backfilling those
-- would be inventing an authorisation that never happened.
--
-- ORDER MATTERS: `operators` and `merchants` are created first because
-- `agents`, `api_credentials`, `spending_consents` and `consent_publishers`
-- all reference them. Postgres resolves foreign keys at CREATE TABLE time and
-- errors if the referenced table does not exist yet; SQLite resolves lazily
-- and would not have caught it.


-- The party that answers for an agent's spending.
CREATE TABLE IF NOT EXISTS operators (
    operator_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL
);

-- The publisher being paid. Consents may be scoped to these.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id                  TEXT PRIMARY KEY,
    display_name                 TEXT NOT NULL,
    settlement_account_reference TEXT,
    status                       TEXT NOT NULL DEFAULT 'active',
    created_at                   TEXT NOT NULL
);

-- Control-plane API keys. Only the HASH is stored: the plaintext is returned
-- once at issuance and is unrecoverable afterwards, so a disclosure of this
-- table yields nothing an attacker can authenticate with.
CREATE TABLE IF NOT EXISTS api_credentials (
    credential_id TEXT PRIMARY KEY,
    operator_id   TEXT,
    merchant_id   TEXT,
    label         TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    scopes        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT,
    replaced_by   TEXT,
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id)
);

-- Signing-key lifecycle. Credentials are rows, not a column that gets
-- overwritten, so revoking a key stops new authorization without invalidating
-- the acceptances it already signed.
CREATE TABLE IF NOT EXISTS agent_credentials (
    credential_id TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    algorithm     TEXT NOT NULL,
    public_key    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    valid_from    TEXT NOT NULL,
    valid_until   TEXT,
    revoked_at    TEXT,
    replaced_by   TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
);

-- Proof-of-possession challenges. Single-use and time-limited.
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

-- An operator's standing authorisation for one agent to spend, with limits in
-- integer paise. Modelled on the consent shape Razorpay and NPCI describe for
-- UPI Reserve Pay.
CREATE TABLE IF NOT EXISTS spending_consents (
    consent_id              TEXT PRIMARY KEY,
    operator_id             TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
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

-- Publisher scope. No rows means "any publisher".
CREATE TABLE IF NOT EXISTS consent_publishers (
    consent_id  TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    PRIMARY KEY (consent_id, merchant_id),
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id)
);


-- The columns 001 cannot add to an existing database. NULL on every existing
-- row, deliberately: see the header.
ALTER TABLE agents      ADD COLUMN IF NOT EXISTS operator_id TEXT;
ALTER TABLE agents      ADD COLUMN IF NOT EXISTS status      TEXT NOT NULL DEFAULT 'active';
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS operator_id TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS consent_id  TEXT;


CREATE INDEX IF NOT EXISTS idx_api_credentials_hash ON api_credentials (key_hash);
CREATE INDEX IF NOT EXISTS idx_agent_credentials_agent
    ON agent_credentials (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_consents_agent ON spending_consents (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agents_operator ON agents (operator_id);
CREATE INDEX IF NOT EXISTS idx_commitments_operator ON commitments (operator_id, settle_date);
