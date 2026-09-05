-- Bharat x402 — migration 006: double-entry journal.
--
-- WHO NEEDS THIS: any database created before Phase 4. A database built from
-- the current 001_init.sql already has the tables — 001 is regenerated from
-- ledger.schema_sql() — but NOT the seeded accounts, because those are
-- reference data rather than schema. Run the INSERTs at the bottom regardless.
--
-- WHY A JOURNAL: the status columns on `commitments` and `batches` answer
-- "where is this commitment?". They cannot answer "what happened to the money,
-- in what order, and does it add up?" — and a refund allocated across an
-- aggregate collection, or a write-off that has to leave a trail, needs the
-- second question answered.
--
-- TWO RULES, both enforced in code rather than by constraints because SQL
-- cannot express either cheaply:
--   1. Every transaction's debits equal its credits (ledger.post_journal).
--   2. Nothing is ever updated or deleted. Errors are corrected by posting a
--      compensating transaction that references the original.
--
-- SAFE TO RUN MORE THAN ONCE: tables are IF NOT EXISTS and the account
-- INSERTs are ON CONFLICT DO NOTHING.
--
-- NON-DESTRUCTIVE: no existing row is touched. Commitments that predate this
-- migration have no journal entries, and are not backfilled — inventing
-- postings for money that moved before anyone was recording it would be
-- fabricating an audit trail, which is worse than not having one. Reports that
-- reconcile the journal against the commitment tables should therefore be run
-- over a date range starting after this migration was applied.
--
-- ORDER MATTERS: `accounts` before `journal_entries`, which has a foreign key
-- to it. Postgres resolves foreign keys at CREATE TABLE time.
--
-- THE ACCOUNT SEED IS GENERATED, NOT HAND-MAINTAINED. It is the literal
-- contents of journal.CHART_OF_ACCOUNTS, so there is exactly one definition of
-- what an account means — in the module that also defines what may be posted
-- to it. Regenerate rather than hand-editing if that tuple changes.


-- The chart of accounts.
CREATE TABLE IF NOT EXISTS accounts (
    account_code   TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    -- asset | liability | equity | revenue | expense
    account_type   TEXT NOT NULL,
    -- debit | credit: which direction increases this account.
    normal_balance TEXT NOT NULL
);

-- One economic event. Immutable once posted.
CREATE TABLE IF NOT EXISTS journal_transactions (
    txn_id       TEXT PRIMARY KEY,
    -- The idempotency key. UNIQUE is what makes a replayed command post
    -- nothing, rather than application logic remembering to check.
    command_ref  TEXT NOT NULL UNIQUE,
    txn_type     TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    posted_at    TEXT NOT NULL,
    description  TEXT,
    -- Set on a compensating transaction, naming the one it reverses.
    reverses_txn_id TEXT,
    FOREIGN KEY (reverses_txn_id) REFERENCES journal_transactions (txn_id)
);

-- The legs. `amount_paise` is always POSITIVE; `direction` carries the sign,
-- so the same fact cannot be written two ways.
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id      TEXT PRIMARY KEY,
    txn_id        TEXT NOT NULL,
    account_code  TEXT NOT NULL,
    direction     TEXT NOT NULL,
    amount_paise  INTEGER NOT NULL,
    agent_id      TEXT,
    operator_id   TEXT,
    merchant_id   TEXT,
    commitment_id TEXT,
    batch_id      TEXT,
    FOREIGN KEY (txn_id) REFERENCES journal_transactions (txn_id),
    FOREIGN KEY (account_code) REFERENCES accounts (account_code)
);

CREATE INDEX IF NOT EXISTS idx_journal_entries_account ON journal_entries (account_code);
CREATE INDEX IF NOT EXISTS idx_journal_entries_txn ON journal_entries (txn_id);
CREATE INDEX IF NOT EXISTS idx_journal_entries_commitment ON journal_entries (commitment_id);


-- Reference data, generated from journal.CHART_OF_ACCOUNTS.

INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('1100', 'Agent receivable', 'asset', 'debit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('1200', 'Gateway clearing', 'asset', 'debit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('2100', 'Operator reserved authority', 'liability', 'credit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('2200', 'Publisher payable', 'liability', 'credit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('2300', 'Refund liability', 'liability', 'credit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('2400', 'Tax payable', 'liability', 'credit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('4100', 'Platform fee revenue', 'revenue', 'credit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('5100', 'Gateway fee expense', 'expense', 'debit')
  ON CONFLICT (account_code) DO NOTHING;
INSERT INTO accounts (account_code, name, account_type, normal_balance)
  VALUES ('5200', 'Bad debt expense', 'expense', 'debit')
  ON CONFLICT (account_code) DO NOTHING;
