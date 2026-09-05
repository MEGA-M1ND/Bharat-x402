"""Reserved authority: what actually stands behind a request.

THE PROBLEM THIS SOLVES
-----------------------
After Phase 2 an agent has an operator and a consent with limits. That is a
statement of *permission*. It is still not a statement that anything backs the
spend — a consent for ₹50,000 written by an operator with nothing behind it
authorises exactly as much content as one backed by real funds.

So this module adds the step between "you are allowed to" and "here is the
content": an amount is **reserved** against an authority balance before the
handler runs, and only converted into a receivable once the content was
actually delivered.

Content is released against a reservation. Not against a signature, and not
against a promise.

WHY A CHECK IS NOT ENOUGH, AND THE ONE RULE THAT MATTERS
--------------------------------------------------------
The obvious implementation reads the balance, decides, and then writes:

    if account.available >= amount:        # <- two concurrent requests
        account.available -= amount        #    both pass this line

That is the classic overspend. Both requests observe the same balance before
either has written, and both proceed. A transaction does not save you: it
serialises the writes, not the decision.

Every state change here is therefore a **single conditional UPDATE** whose
WHERE clause contains the check:

    UPDATE authority_accounts
       SET available_paise = available_paise - ?, reserved_paise = reserved_paise + ?
     WHERE account_id = ? AND available_paise >= ?

`WHERE available_paise >= ?` takes a row lock in both engines, and only one
concurrent transaction can observe `rowcount == 1`. The other sees `0` and is
refused. There is no read-then-write gap to lose a race in, and no
process-local lock is load-bearing — which matters because the deployed
version runs on serverless instances that share nothing.

This is the same discipline `create_commitment` already used for offers. It is
applied here to money.

THREE BACKINGS, AND WHAT EACH ONE HONESTLY IS
---------------------------------------------
  * **prefunded** — a balance topped up in advance. No credit risk: the
    authority exists before the content does. In this project the top-up is a
    test-mode control-plane call, not a received payment.

  * **simulated_reserve** — models UPI Reserve Pay: an amount blocked once and
    debited repeatedly until exhausted or expired. The *domain* behaviour is
    real code with real concurrency guarantees. **No money is held anywhere.**
    There is no NPCI mandate, no bank, and no public Reserve Pay API was
    available to this project — activation is gated behind a Razorpay support
    request. It is a simulation and is labelled as one everywhere it appears.

  * **credit** — no funds at all. A limit the platform is exposed to, tracked
    as exposure and capped by a risk ceiling. Usage backed by credit is
    **never** called paid, and the amount owed is reported as outstanding
    rather than folded into revenue.

WHAT THIS MODULE DOES NOT DECIDE
--------------------------------
Whether the *consent* permits the spend at all — that is `consent.py`, and it
runs first. This module answers the narrower question of whether there is room,
and holds it if so.
"""

from __future__ import annotations

from dataclasses import dataclass

# Backing types. Strings rather than an enum because they are stored in the
# database and appear in JSON; an enum would be converted at both boundaries
# for no benefit.
PREFUNDED = "prefunded"
SIMULATED_RESERVE = "simulated_reserve"
CREDIT = "credit"
BACKINGS = (PREFUNDED, SIMULATED_RESERVE, CREDIT)

# Backings where no money exists and the platform carries the exposure. Used
# to decide whether usage may be described as funded, and to keep credit
# exposure out of anything labelled revenue.
UNFUNDED_BACKINGS = frozenset({CREDIT})

# How long a reservation is held before the sweeper returns it.
#
# Long enough to cover a slow publisher handler and a retry; short enough that
# a crashed request does not strand an agent's authority for the rest of the
# day. The x402 middleware calls /verify then the handler then /settle within
# one HTTP request, so the realistic window is seconds.
DEFAULT_RESERVATION_TTL_SECONDS = 900


class AuthorityError(Exception):
    """A reservation, capture, or release could not proceed.

    Same shape as `ConsentDenied` and `VerificationError`: a machine-readable
    `reason` for the protocol layer, a human message that names what was short
    and by how much, and structured `detail` for the audit trail.
    """

    def __init__(self, reason: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class AuthoritySnapshot:
    """What an account looks like right now, for display and for policy.

    Every figure is integer paise. The five that matter are deliberately
    separate: merging any two of them is how a dashboard ends up reporting
    money that has not arrived.
    """

    account_id: str
    consent_id: str
    operator_id: str
    backing: str
    funded_paise: int
    available_paise: int
    reserved_paise: int
    captured_paise: int
    refunded_paise: int
    credit_limit_paise: int
    overdue_paise: int
    status: str

    @property
    def is_funded(self) -> bool:
        """Whether real (or simulated-real) value stands behind this account.

        Credit is not funded. A publisher that will not extend credit checks
        exactly this.
        """
        return self.backing not in UNFUNDED_BACKINGS

    @property
    def spendable_paise(self) -> int:
        """What a new request may draw on.

        For funded backings that is the available balance. For credit it is
        the unused portion of the limit — which is *exposure* the platform
        takes on, not money it has.
        """
        if self.backing == CREDIT:
            used = self.reserved_paise + self.captured_paise - self.refunded_paise
            return max(self.credit_limit_paise - used, 0)
        return self.available_paise

    @property
    def exposure_paise(self) -> int:
        """Value at risk: released to the agent, not yet collected."""
        return max(self.captured_paise - self.refunded_paise, 0)

    def as_dict(self) -> dict:
        return {
            "accountId": self.account_id,
            "consentId": self.consent_id,
            "operatorId": self.operator_id,
            "backing": self.backing,
            "fundedPaise": self.funded_paise,
            "availablePaise": self.available_paise,
            "reservedPaise": self.reserved_paise,
            "capturedPaise": self.captured_paise,
            "refundedPaise": self.refunded_paise,
            "creditLimitPaise": self.credit_limit_paise,
            "overduePaise": self.overdue_paise,
            "spendablePaise": self.spendable_paise,
            "exposurePaise": self.exposure_paise,
            "isFunded": self.is_funded,
            "status": self.status,
            # Said in the payload, not only in the docs. A client rendering
            # this must be able to tell a simulation from a settled fact
            # without reading a README.
            "simulated": self.backing == SIMULATED_RESERVE,
        }


def snapshot_from_row(account) -> AuthoritySnapshot:
    """Builds a snapshot from an `authority_accounts` row.

    Lives here rather than at either call site because two copies of "which
    column maps to which field" is exactly the kind of duplication that drifts
    — one gets a new column and the other silently keeps reporting the old
    shape. Accepts any row type: sqlite3.Row and psycopg dict rows both
    subscript by column name.
    """
    return AuthoritySnapshot(
        account_id=account["account_id"],
        consent_id=account["consent_id"],
        operator_id=account["operator_id"],
        backing=account["backing"],
        funded_paise=account["funded_paise"],
        available_paise=account["available_paise"],
        reserved_paise=account["reserved_paise"],
        captured_paise=account["captured_paise"],
        refunded_paise=account["refunded_paise"],
        credit_limit_paise=account["credit_limit_paise"],
        overdue_paise=account["overdue_paise"],
        status=account["status"],
    )


@dataclass(frozen=True)
class PublisherPolicy:
    """What a publisher will accept before serving content.

    The fourth policy layer (see consent.py). Distinct from the operator's
    consent and the facilitator's risk ceiling because it answers a different
    party's question: the operator decides what its agent *may* spend, the
    platform decides how much exposure it will carry in total, and this
    decides what *this publisher* will hand over content for.

    A newspaper willing to serve anything and invoice later, and an API
    willing to serve only against prefunded balance, are both reasonable and
    cannot be expressed by one setting.
    """

    # Refuse anything not backed by real (or simulated-real) value. The
    # strictest useful setting, and the one that removes credit risk entirely.
    require_funded_authority: bool = False
    # Refuse agents whose operator has no display identity on file.
    require_verified_operator: bool = False
    # Ceiling on uncollected value this publisher will carry for one agent.
    # 0 means no ceiling.
    max_unsecured_exposure_paise: int = 0
    # Empty means "any". Non-empty restricts to named operators.
    allowed_operator_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, environ) -> PublisherPolicy:
        """Builds a policy from environment variables."""

        def flag(name: str) -> bool:
            return (environ.get(name) or "").strip().lower() in ("1", "true", "yes")

        allowed = (environ.get("PUBLISHER_ALLOWED_OPERATORS") or "").split()
        return cls(
            require_funded_authority=flag("PUBLISHER_REQUIRE_FUNDED_AUTHORITY"),
            require_verified_operator=flag("PUBLISHER_REQUIRE_VERIFIED_OPERATOR"),
            max_unsecured_exposure_paise=int(
                environ.get("PUBLISHER_MAX_UNSECURED_EXPOSURE_PAISE", "0") or 0
            ),
            allowed_operator_ids=frozenset(allowed),
        )

    def check(self, snapshot: AuthoritySnapshot, *, amount_paise: int) -> None:
        """Raises unless this publisher will serve against `snapshot`.

        Args:
            snapshot: The authority backing the request.
            amount_paise: What is about to be reserved.

        Raises:
            AuthorityError: With a reason naming which rule refused, so the
                agent can tell "top up your balance" apart from "your operator
                is not on our list".
        """
        if self.require_funded_authority and not snapshot.is_funded:
            raise AuthorityError(
                "funded_authority_required",
                "This publisher serves only against funded authority; this agent is "
                f"backed by {snapshot.backing}.",
                backing=snapshot.backing,
            )

        if self.allowed_operator_ids and snapshot.operator_id not in self.allowed_operator_ids:
            raise AuthorityError(
                "operator_not_allowed",
                f"This publisher does not accept traffic from operator {snapshot.operator_id}.",
                operatorId=snapshot.operator_id,
            )

        if self.max_unsecured_exposure_paise > 0 and not snapshot.is_funded:
            # Only unfunded backings accrue *unsecured* exposure. Prefunded
            # value is already there, so counting it here would refuse
            # perfectly safe traffic.
            projected = snapshot.exposure_paise + amount_paise
            if projected > self.max_unsecured_exposure_paise:
                raise AuthorityError(
                    "unsecured_exposure_exceeded",
                    f"{projected} paise of unsecured exposure would exceed this "
                    f"publisher's ceiling of {self.max_unsecured_exposure_paise} paise.",
                    exposurePaise=snapshot.exposure_paise,
                    ceilingPaise=self.max_unsecured_exposure_paise,
                )

    def describe(self) -> dict:
        """Advertised on /supported, so a client can plan rather than be refused."""
        return {
            "requireFundedAuthority": self.require_funded_authority,
            "requireVerifiedOperator": self.require_verified_operator,
            "maxUnsecuredExposurePaise": self.max_unsecured_exposure_paise or None,
            "allowedOperators": sorted(self.allowed_operator_ids) or None,
        }
