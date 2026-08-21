-- Bharat x402 — migration 002: agent signing keys and Razorpay webhooks.
--
-- WHO NEEDS THIS: only databases that had 001_init.sql applied *before*
-- Ed25519 agent keys and webhook intake were added. A database created from
-- the current 001_init.sql already has everything here and can skip it —
-- 001 is regenerated from ledger.schema_sql(), so it is always the complete
-- current schema, not a historical snapshot.
--
-- WHY 002 EXISTS AT ALL, given that: three of the changes below are new
-- tables, which 001's `CREATE TABLE IF NOT EXISTS` would happily create on
-- a re-run. The batch columns are not. `CREATE TABLE IF NOT EXISTS batches`
-- is a silent no-op against an existing `batches` table, so re-running 001
-- on an already-deployed database adds the new tables and quietly skips the
-- new columns — leaving the webhook handler writing to columns that do not
-- exist. Hence explicit ALTERs.
--
-- Safe to run more than once: every statement is IF NOT EXISTS.

-- The public half of each agent's signing keypair. The facilitator stores
-- only this, never a private key — that asymmetry is the point. See
-- facilitator/payment_verifier.py.
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    public_key    TEXT NOT NULL,
    algorithm     TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

-- Razorpay retries webhooks until it gets a 2xx, so duplicate deliveries are
-- routine. This table's primary key is what makes processing exactly-once.
CREATE TABLE IF NOT EXISTS webhook_events (
    dedupe_key      TEXT PRIMARY KEY,
    event           TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    payment_link_id TEXT,
    outcome         TEXT NOT NULL,
    detail          TEXT
);

-- Payment confirmation, filled in by the webhook rather than by the
-- settlement run that created the link. A created Payment Link is an
-- invoice; only these columns being non-null means money arrived.
ALTER TABLE batches ADD COLUMN IF NOT EXISTS paid_at             TEXT;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS amount_paid_paise   INTEGER;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT;

-- The webhook's lookup path: Razorpay names a plink_ id, we need its batch.
CREATE INDEX IF NOT EXISTS idx_batches_payment_link ON batches (payment_link_id);
