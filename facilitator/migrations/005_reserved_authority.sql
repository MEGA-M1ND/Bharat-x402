-- Bharat x402 — migration 005: reserved authority.
--
-- WHO NEEDS THIS: any database created before Phase 3. A database built from
-- the current 001_init.sql already has both tables — 001 is regenerated from
-- ledger.schema_sql(), so it is always the complete current schema.
--
-- WHAT THIS CHANGES: after Phase 2 an agent had an operator and a consent with
-- limits. That is a statement of *permission* and says nothing about whether
-- anything stands behind the spend — a consent for ₹50,000 written by an
-- operator with nothing behind it authorises exactly as much content as one
-- backed by funds. These two tables are what "content is released against
-- authority" actually means.
--
-- BOTH ARE NEW TABLES, so unlike migration 004 there are no ALTERs here and
-- re-running 001 would in fact create them. This file exists so the migration
-- sequence stays readable as a history, and so an operator applying migrations
-- in order does not have to reason about which parts of 001 are no-ops.
--
-- SAFE TO RUN MORE THAN ONCE: every statement is IF NOT EXISTS.
--
-- NON-DESTRUCTIVE: nothing existing is modified. Consents created before this
-- migration have no authority account, which under AUTHORITY_REQUIRED=true
-- means their agents are refused until an operator funds one. That is the
-- correct outcome and not a bug: those consents were never backed by anything,
-- and inventing a balance for them during a migration would be fabricating an
-- authorisation that never happened.
--
-- ORDER MATTERS: `spending_consents` and `operators` (migration 004) must
-- exist first. Postgres resolves foreign keys at CREATE TABLE time.


-- The balance a consent draws against.
--
-- NOT MONEY. Nothing here is held at a bank and no NPCI mandate exists.
-- `simulated_reserve` models the domain behaviour of UPI Reserve Pay — an
-- amount blocked up front and debited repeatedly — because the accounting and
-- concurrency questions are real even when the block is not.
--
-- The columns satisfy an invariant the tests assert:
--
--     funded = available + reserved + captured - refunded
CREATE TABLE IF NOT EXISTS authority_accounts (
    account_id      TEXT PRIMARY KEY,
    consent_id      TEXT NOT NULL UNIQUE,
    operator_id     TEXT NOT NULL,
    -- prefunded | simulated_reserve | credit
    backing         TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'INR',
    funded_paise    INTEGER NOT NULL DEFAULT 0,
    available_paise INTEGER NOT NULL DEFAULT 0,
    reserved_paise  INTEGER NOT NULL DEFAULT 0,
    captured_paise  INTEGER NOT NULL DEFAULT 0,
    refunded_paise  INTEGER NOT NULL DEFAULT 0,
    -- Credit only: how far the platform will let exposure run.
    credit_limit_paise INTEGER NOT NULL DEFAULT 0,
    -- Collection failures not yet recovered.
    overdue_paise   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (consent_id) REFERENCES spending_consents (consent_id),
    FOREIGN KEY (operator_id) REFERENCES operators (operator_id)
);

-- An amount held against an account while a request is in flight.
--
-- `offer_id` is UNIQUE, and that constraint is the exactly-once guarantee for
-- capture — enforced by the database rather than by application logic
-- remembering to check, the same discipline as `commitments.offer_id`. A
-- retried request finds its existing reservation instead of holding a second
-- amount.
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


-- The sweeper's query: held reservations past their TTL.
CREATE INDEX IF NOT EXISTS idx_reservations_expiry ON reservations (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_authority_operator ON authority_accounts (operator_id);
