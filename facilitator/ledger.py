"""SQLite ledger and audit trail for the INR facilitator.

Every state change a payment goes through is written here, and every write also
emits a structured JSON line to stdout. The ledger is the answer to the
question a publisher will actually ask — *which agent owes me what, and did it
get paid?* — so it is deliberately the most boring, most explicit code in the
project.

Four tables:

    offers       a quoted price, signed and time-limited
    commitments  an agent's accepted offer — a debt, not yet money
    batches      one real Razorpay charge covering many commitments
    events       append-only log of everything that happened

The offer -> commitment -> batch progression is the whole idea. A ₹5 API call
becomes a commitment instantly (cheap, no gateway involved), and rupees move
later when many commitments are charged together.

Money is stored as INTEGER paise throughout. Never floats — SQLite's REAL type
would silently lose precision on rupee arithmetic and there is no reason to
risk it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

# Serialises writes. SQLite handles concurrent readers fine, but FastAPI runs
# sync endpoints in a threadpool and we would rather queue briefly than field
# "database is locked" errors in a demo.
_WRITE_LOCK = threading.Lock()

DEFAULT_DB_PATH = os.getenv("LEDGER_DB_PATH", "./data/ledger.db")


def utc_now() -> str:
    """Current time as an ISO-8601 UTC string.

    One timestamp format everywhere, so ledger rows sort lexicographically and
    the reporting script does not need a date parser.
    """
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_utc() -> str:
    """Current UTC date as YYYY-MM-DD — the batching key."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


SCHEMA = """
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
    status        TEXT NOT NULL DEFAULT 'pending',
    batch_id      TEXT,
    FOREIGN KEY (offer_id) REFERENCES offers (offer_id),
    FOREIGN KEY (batch_id) REFERENCES batches (batch_id)
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
    -- created | failed | dry_run
    status              TEXT NOT NULL,
    razorpay_mode       TEXT NOT NULL,
    error_message       TEXT
);

-- Append-only. Nothing in this table is ever updated or deleted; it is the
-- audit trail you would hand an auditor or replay to reconstruct a day.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    event        TEXT NOT NULL,
    agent_id     TEXT,
    resource_id  TEXT,
    amount_paise INTEGER,
    status       TEXT,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_commitments_settlement
    ON commitments (status, agent_id, settle_date);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


class Ledger:
    """The facilitator's book of record.

    Args:
        db_path: Where the SQLite file lives. Parent directories are created.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yields a connection with rows accessible by column name."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Enforce the foreign keys declared above; SQLite ignores them by default.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with _WRITE_LOCK, self._connect() as conn:
            conn.executescript(SCHEMA)

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
        """
        ts = utc_now()
        detail_json = json.dumps(detail, sort_keys=True, default=str) if detail else None

        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO events"
                " (ts, event, agent_id, resource_id, amount_paise, status, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, event, agent_id, resource_id, amount_paise, status, detail_json),
            )

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

    def get_offer(self, offer_id: str) -> sqlite3.Row | None:
        """Looks up one offer, or None."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,))
            return cur.fetchone()

    # -- commitments -------------------------------------------------------

    def get_commitment_by_offer(self, offer_id: str) -> sqlite3.Row | None:
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
    ) -> dict:
        """Turns an open offer into a commitment, atomically.

        The offer status flip and the commitment insert share one transaction,
        so an offer can never be left consumed with no matching debt, nor
        produce two debts. The UNIQUE constraint on offer_id is the backstop.

        Args:
            commitment_id: Id to assign.
            offer_id: The offer being spent.
            agent_id: Who owes the money.
            resource_id: What they bought.
            amount_paise: How much, in paise.
            asset: Currency code.
            mode: "deferred" or "per_request".
            settle_date: Batching key; defaults to today UTC.

        Returns:
            The created commitment as a dict.

        Raises:
            ValueError: If the offer is missing or already spent.
        """
        settle_date = settle_date or today_utc()
        created_at = utc_now()

        with _WRITE_LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM offers WHERE offer_id = ?", (offer_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"offer {offer_id} does not exist")
            if row["status"] != "open":
                raise ValueError(f"offer {offer_id} is already {row['status']}")

            conn.execute(
                "INSERT INTO commitments (commitment_id, offer_id, agent_id, resource_id,"
                " amount_paise, asset, created_at, settle_date, mode, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
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
                ),
            )
            conn.execute(
                "UPDATE offers SET status = 'consumed' WHERE offer_id = ?", (offer_id,)
            )

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
        }

    def pending_commitments(
        self, *, agent_id: str | None = None, settle_date: str | None = None
    ) -> list[sqlite3.Row]:
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
    ) -> None:
        """Writes the batch and marks its commitments settled, in one transaction.

        If the Razorpay call failed, the commitments stay `pending` so the next
        run picks them up again — a failed charge must never look like a paid
        one.
        """
        with _WRITE_LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO batches (batch_id, agent_id, settle_date, created_at,"
                " commitment_count, total_paise, payment_link_id, payment_link_url,"
                " status, razorpay_mode, error_message)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    agent_id,
                    settle_date,
                    utc_now(),
                    len(commitment_ids),
                    int(total_paise),
                    payment_link_id,
                    payment_link_url,
                    status,
                    razorpay_mode,
                    error_message,
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

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        """Looks up one batch, or None."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()

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

    def daily_summary(self, settle_date: str | None = None) -> dict:
        """Aggregates one day's activity for the publisher's report.

        Args:
            settle_date: Day to summarise, YYYY-MM-DD. Defaults to today UTC.

        Returns:
            Totals, per-agent breakdown, and the batches generated.
        """
        settle_date = settle_date or today_utc()

        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS requests, COALESCE(SUM(amount_paise), 0) AS total_paise"
                " FROM commitments WHERE settle_date = ?",
                (settle_date,),
            ).fetchone()

            by_agent = conn.execute(
                "SELECT agent_id, COUNT(*) AS requests,"
                " COALESCE(SUM(amount_paise), 0) AS total_paise,"
                " SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) AS settled,"
                " SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending"
                " FROM commitments WHERE settle_date = ?"
                " GROUP BY agent_id ORDER BY total_paise DESC",
                (settle_date,),
            ).fetchall()

            by_resource = conn.execute(
                "SELECT resource_id, COUNT(*) AS requests,"
                " COALESCE(SUM(amount_paise), 0) AS total_paise"
                " FROM commitments WHERE settle_date = ?"
                " GROUP BY resource_id ORDER BY total_paise DESC",
                (settle_date,),
            ).fetchall()

            batches = conn.execute(
                "SELECT * FROM batches WHERE settle_date = ? ORDER BY created_at ASC",
                (settle_date,),
            ).fetchall()

            rejections = conn.execute(
                "SELECT COUNT(*) AS n FROM events"
                " WHERE status = 'rejected' AND substr(ts, 1, 10) = ?",
                (settle_date,),
            ).fetchone()

        return {
            "settleDate": settle_date,
            "requests": totals["requests"],
            "totalPaise": totals["total_paise"],
            "rejectedPayments": rejections["n"],
            "byAgent": [dict(r) for r in by_agent],
            "byResource": [dict(r) for r in by_resource],
            "batches": [dict(r) for r in batches],
        }
