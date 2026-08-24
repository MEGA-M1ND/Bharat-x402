-- Bharat x402 — migration 003: which instrument settled a batch.
--
-- WHO NEEDS THIS: databases created before UPI Reserve Pay was added as a
-- second settlement instrument. A database built from the current
-- 001_init.sql already has the column — 001 is regenerated from
-- ledger.schema_sql(), so it is always the complete current schema.
--
-- Why the column exists at all: a Reserve Pay debit has no URL, and neither
-- does a *failed* Payment Link. Without this they are indistinguishable in
-- the ledger. See facilitator/reserve_pay.py.
--
-- Safe to run more than once.

ALTER TABLE batches
  ADD COLUMN IF NOT EXISTS instrument TEXT NOT NULL DEFAULT 'payment_link';
