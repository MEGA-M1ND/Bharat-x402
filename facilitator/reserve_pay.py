"""UPI Reserve Pay (SBMD) — a simulated second settlement instrument.

---------------------------------------------------------------------------
WHAT THIS IS FOR
---------------------------------------------------------------------------
The README claims the commitment ledger is indifferent to what settles it, and
that swapping Payment Links for UPI Reserve Pay would touch one file. A claim
like that is free to make and easy to get wrong, so this exists to test it by
actually building the second instrument. What it cost is written up at the
bottom of this docstring — the claim was close, not exact.

**Simulated, and not close to the real thing.** Razorpay's agentic-payments
product runs on UPI Reserve Pay and is in closed beta; there is no public API
to integrate against. So this models the *shape* of the instrument — a block
of funds authorised once, debited repeatedly without a checkout page — and
fabricates the identifiers. It is deliberately not reachable in any mode that
touches the real Razorpay API.

---------------------------------------------------------------------------
WHY THE SHAPE MATTERS MORE THAN THE FEES
---------------------------------------------------------------------------
`razorpay_client.py` lists four barriers to per-request INR settlement. Reserve
Pay does not remove all of them, and it is worth being precise about which,
because the tempting overclaim is "Reserve Pay fixes everything":

  1. **The ₹1.00 gateway minimum — NOT fixed.** A debit is still a payment
     instruction on the UPI rail, and nothing about a block makes a 50-paise
     debit economic. Batching is still what makes sub-rupee pricing exist.
  2. **Checkout has a human in it — FIXED, and this is the point.** A Payment
     Link is a page somebody opens. A mandate is authorised once, and every
     debit after that is server-to-server with no PIN prompt and no page. That
     is the barrier that made a hosted link absurd in the path of a
     machine-to-machine request, and it is the one this removes.
  3. **Fixed per-transaction cost — unchanged.** Still multiplies by N.
  4. **Percentage fees — unchanged**, and still neutral to batching.

So the honest summary: Reserve Pay fixes the barrier that made the *instrument*
wrong for agents, and leaves the economics that make *batching* necessary
exactly where they were. Both are needed, which is why this sits behind the
same batch settlement rather than replacing it.

---------------------------------------------------------------------------
WHAT THE SWAP ACTUALLY COST
---------------------------------------------------------------------------
"Only `razorpay_client.py` changes" was nearly right. In full:

  * this file, the adapter;
  * `razorpay_client.py`, which gained an instrument-agnostic `create_charge`
    and dispatches on config;
  * two call sites renamed in `main.py` — `create_payment_link` became
    `create_charge`. Keeping the old name would have meant a method called
    `create_payment_link` returning a mandate debit, which is the kind of lie
    that costs somebody an afternoon later;
  * one nullable `batches.instrument` column, because without it a Reserve Pay
    debit and a *failed* Payment Link are indistinguishable in the ledger —
    both have a null URL.

Nothing in the ledger's money handling, the settlement batching, the
commitment lifecycle, or the webhook intake moved. That was the part worth
proving.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Per-agent block ceiling, in paise. The amount a mandate authorises up front
# and debits draw down against. Default ₹5,000 — large next to a day of
# rupee-priced agent traffic, which is the point of a block.
DEFAULT_BLOCK_PAISE = 500_000


class ReservePayError(Exception):
    """A debit could not be made against a mandate."""


@dataclass(frozen=True)
class Mandate:
    """A block of funds authorised once and debited many times.

    Attributes:
        mandate_id: Razorpay-ish identifier for the consent.
        agent_id: Who authorised it.
        max_amount_paise: Ceiling the block authorises in total.
        valid_until: ISO-8601 UTC expiry.
    """

    mandate_id: str
    agent_id: str
    max_amount_paise: int
    valid_until: str


class MockReservePay:
    """A stand-in for UPI Reserve Pay, good enough to settle a batch against.

    Args:
        block_paise: What a mandate authorises. From
            `RESERVE_PAY_BLOCK_PAISE` when not given.
        validity_days: How long a mandate lasts.
    """

    def __init__(self, block_paise: int | None = None, validity_days: int = 30) -> None:
        self.block_paise = (
            block_paise
            if block_paise is not None
            else int(os.getenv("RESERVE_PAY_BLOCK_PAISE", str(DEFAULT_BLOCK_PAISE)))
        )
        self.validity_days = validity_days

    @property
    def mode(self) -> str:
        return "reserve_pay_mock"

    def ensure_mandate(self, agent_id: str) -> Mandate:
        """The mandate for an agent, created on first use.

        The id is *derived* from the agent id rather than generated and
        stored, for the same reason the console's simulated agent key is
        derived: this runs serverless, so anything held in process memory is
        gone on the next cold start and a freshly generated id would make the
        same agent look like a new counterparty every few minutes.

        A real integration would persist the mandate — it represents a
        customer's authorisation and cannot be reconstructed from a hash — so
        this is the one place the simulation is structurally, not just
        cosmetically, unlike the real thing.
        """
        digest = hashlib.sha256(f"reserve-pay-mandate:{agent_id}".encode()).hexdigest()
        valid_until = (
            datetime.now(UTC) + timedelta(days=self.validity_days)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        return Mandate(
            mandate_id=f"rpmandate_{digest[:20]}",
            agent_id=agent_id,
            max_amount_paise=self.block_paise,
            valid_until=valid_until,
        )

    def debit(
        self,
        *,
        mandate: Mandate,
        amount_paise: int,
        reference_id: str,
        already_debited_paise: int = 0,
    ) -> dict:
        """Debits a mandate, server-to-server, with no page for anyone to open.

        Args:
            mandate: The consent to draw against.
            amount_paise: How much to take.
            reference_id: Our batch id, for reconciliation.
            already_debited_paise: What this mandate has been drawn down by
                already. Passed in rather than tracked here — the ledger is
                the record of what has been debited, and a second tally kept
                in this object would be one that disagrees with it.

        Returns:
            The same shape a Payment Link returns, so the caller does not
            branch on the instrument — except `short_url`, which is `None`
            because the absence of a page to open is the whole feature.

        Raises:
            ReservePayError: If the debit exceeds what the block authorises.
        """
        if amount_paise <= 0:
            raise ReservePayError(f"cannot debit {amount_paise} paise")

        remaining = mandate.max_amount_paise - already_debited_paise
        if amount_paise > remaining:
            raise ReservePayError(
                f"debit of {amount_paise} paise exceeds the {remaining} paise remaining "
                f"on mandate {mandate.mandate_id} (block {mandate.max_amount_paise} paise). "
                "A real mandate would need re-authorising by the payer."
            )

        digest = hashlib.sha256(f"{mandate.mandate_id}:{reference_id}".encode()).hexdigest()
        return {
            "id": f"rpdebit_{digest[:20]}",
            # Deliberately None. A Payment Link's URL is a page a human opens;
            # a mandate debit has no equivalent, and inventing one would hide
            # the single most important difference between the two.
            "short_url": None,
            # Debited immediately rather than "created" and awaiting payment.
            # No webhook is needed to learn this settled — which is why the
            # batch is marked paid inline; see razorpay_client.create_charge.
            "status": "captured",
            "mode": self.mode,
            "amount": amount_paise,
            "currency": "INR",
            "instrument": "reserve_pay",
            "mandateId": mandate.mandate_id,
            "mandateRemainingPaise": remaining - amount_paise,
        }
