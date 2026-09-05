"""Spending consent: whether an agent may incur an expense at all.

The question this file answers is the one the original design never asked.
x402 establishes that an agent *agreed to a price*. It says nothing about
whether the agent was ever allowed to spend anyone's money. Those are
different questions with different failure modes, and answering only the first
means content is released against a promise from a pseudonymous key.

A consent is an operator saying, in advance: *this agent may spend up to these
amounts, at these publishers, until this date, and I can revoke it.* That is
deliberately the shape Razorpay and NPCI describe for UPI Reserve Pay — a
one-time consent-based authorisation carrying spending limits, under which an
agent transacts without re-prompting, revocable at any time. Modelling the
same shape means the domain would survive being pointed at that rail rather
than having to be redesigned for it.

FOUR LAYERS, AND WHY THEY ARE NOT REDUNDANT
-------------------------------------------
Each is enforced by a different party and defends against a different failure:

  1. **Agent task budget** (`agent-kit/x402_client.py`) — the agent's own
     discipline. Stops a runaway loop early and cheaply. Worth nothing to the
     facilitator: the client is the party whose behaviour is in question, and
     anyone can write a different one.
  2. **Operator consent** (here) — the authoritative limit on what this agent
     may spend. Held by the party who pays.
  3. **Facilitator risk limit** (`limits.py`) — the platform's own ceiling,
     protecting the facilitator and its publishers from any single operator.
     Applies even when an operator has authorised more.
  4. **Publisher acceptance policy** (Phase 3) — what this publisher will
     accept: prefunded authority only, verified operators only, and so on.

A request must satisfy all four. Removing any one leaves a real gap: without
(2) nobody is on the hook; without (3) a generous operator can concentrate the
platform's entire exposure on one agent; without (4) a publisher cannot refuse
credit it does not want to extend.

FAIL CLOSED
-----------
Every evaluation path returns a refusal by default. There is no branch that
falls through to "allowed" — an unknown consent, an expired one, a suspended
operator, a publisher out of scope, and a missing consent entirely all end in
`ConsentDenied`. The one bypass is the explicitly-named unsafe demo mode, and
it is checked by the caller, not here.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not *reserve* anything. Evaluating limits and holding an amount
against them are separate operations, because a check that is not atomic with
the write it guards is a check two concurrent requests can both pass. The
counters live in `spending_consents.reserved_paise`/`consumed_paise` and move
inside conditional UPDATEs — see `authority.py`. This module decides whether a
request is *eligible*; the database decides whether there is room.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Sentinel meaning "no limit configured". Matches ledger.UNLIMITED_PAISE
# rather than importing it, so this module stays independent of the storage
# layer — it is handed numbers and has no opinion about where they come from.
#
# 2**62 comfortably exceeds any real rupee amount while staying inside a
# 64-bit signed integer with room to add to it, so a limit comparison in SQL
# cannot overflow.
UNLIMITED_PAISE = 2**62


class ConsentDenied(Exception):
    """A request was refused by the consent layer.

    Carries a machine-readable `reason` so `/verify` can answer with an x402
    `invalidReason`, and a human message that says which limit was hit and
    what remains — an agent that is told only "denied" can do nothing useful
    with that, whereas one told it has ₹1.50 left picks something cheaper.
    """

    def __init__(self, reason: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class ConsentDecision:
    """The outcome of evaluating a request against a consent."""

    consent_id: str
    operator_id: str
    agent_id: str
    amount_paise: int
    remaining_daily_paise: int
    remaining_total_paise: int

    def as_dict(self) -> dict:
        return {
            "consentId": self.consent_id,
            "operatorId": self.operator_id,
            "agentId": self.agent_id,
            "amountPaise": self.amount_paise,
            "remainingDailyPaise": self.remaining_daily_paise,
            "remainingTotalPaise": self.remaining_total_paise,
        }


def _field(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """Reads a column from a row of either dialect.

    `sqlite3.Row` supports subscripting but has no `.get()`; psycopg's dict
    rows have both. A bare `row.get(...)` therefore works on Postgres and
    raises `AttributeError` on SQLite — a dialect trap of exactly the kind
    this project has been bitten by before, only in the opposite direction.

    Raising `KeyError`/`IndexError` is how sqlite3.Row signals a missing
    column, so both are caught.
    """
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _parse_iso(value: str | None) -> datetime | None:
    """Parses one of our ISO-8601 UTC timestamps, tolerantly.

    Returns None for absent or unparseable input, and every caller treats
    None as "no bound" — which is safe for `valid_until` (no expiry) and for
    `valid_from` (already in force). A malformed timestamp is a data defect,
    not an authorization decision, and refusing every request because a
    formatting bug crept into one row would be its own kind of outage.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate(
    *,
    consent: Mapping[str, Any] | None,
    operator: Mapping[str, Any] | None,
    agent: Mapping[str, Any] | None,
    merchant_id: str | None,
    scoped_merchant_ids: frozenset[str] | set[str],
    amount_paise: int,
    committed_today_paise: int,
    now: datetime | None = None,
) -> ConsentDecision:
    """Decides whether a request is eligible to proceed under `consent`.

    Every check below is a separate refusal reason on purpose. "Denied" is not
    an operable answer for the agent, the operator, or whoever reads the audit
    trail a week later trying to work out why traffic stopped.

    Args:
        consent: Row from `spending_consents`, or None if the agent has none.
        operator: Row from `operators`, or None.
        agent: Row from `agents`, or None.
        merchant_id: Publisher the spend is at, if known.
        scoped_merchant_ids: Merchants this consent is restricted to. Empty
            means unrestricted.
        amount_paise: The quoted amount, in integer paise.
        committed_today_paise: What this agent has already committed against
            this consent's daily window.
        now: Injectable clock, for tests.

    Returns:
        A `ConsentDecision` carrying what remains under each limit, so the
        caller can advertise headroom rather than making the agent discover
        it by being refused.

    Raises:
        ConsentDenied: On any failure. There is no fall-through to allowed.
    """
    now = now or datetime.now(UTC)

    if amount_paise <= 0:
        raise ConsentDenied(
            "invalid_amount", f"amount must be a positive integer paise value, got {amount_paise}"
        )

    # -- the parties -------------------------------------------------------
    if consent is None:
        raise ConsentDenied(
            "no_consent",
            "This agent has no active spending consent. An operator must authorise it "
            "before it can incur an expense.",
        )

    if operator is None:
        raise ConsentDenied(
            "unknown_operator",
            f"consent {consent['consent_id']} references an operator that does not exist",
        )
    if operator["status"] != "active":
        raise ConsentDenied(
            "operator_not_active",
            f"operator {operator['operator_id']} is {operator['status']}",
            operatorStatus=operator["status"],
        )

    # An agent can be suspended without touching its operator or its consent —
    # the narrowest control available, so one misbehaving process does not
    # require revoking an operator's whole authorisation.
    if agent is not None and _field(agent, "status", "active") != "active":
        raise ConsentDenied(
            "agent_not_active",
            f"agent {agent['agent_id']} is {agent['status']}",
            agentStatus=agent["status"],
        )

    # -- the consent itself ------------------------------------------------
    status = consent["status"]
    if status != "active":
        raise ConsentDenied(
            "consent_not_active",
            f"consent {consent['consent_id']} is {status}",
            consentStatus=status,
        )

    valid_from = _parse_iso(_field(consent, "valid_from"))
    if valid_from is not None and now < valid_from:
        raise ConsentDenied(
            "consent_not_yet_valid",
            f"consent {consent['consent_id']} does not take effect until {consent['valid_from']}",
        )

    valid_until = _parse_iso(_field(consent, "valid_until"))
    if valid_until is not None and now >= valid_until:
        raise ConsentDenied(
            "consent_expired",
            f"consent {consent['consent_id']} expired at {consent['valid_until']}",
            validUntil=consent["valid_until"],
        )

    # -- publisher scope ---------------------------------------------------
    # Empty scope means "any publisher". A non-empty scope that does not
    # include this merchant is a refusal, and notably so is an *unknown*
    # merchant: a consent restricted to named publishers must not be spendable
    # at a request whose publisher we cannot identify.
    if scoped_merchant_ids:
        if merchant_id is None or merchant_id not in scoped_merchant_ids:
            raise ConsentDenied(
                "merchant_out_of_scope",
                f"consent {consent['consent_id']} does not authorise spending at "
                f"{merchant_id or 'an unidentified publisher'}",
                merchantId=merchant_id,
                allowed=sorted(scoped_merchant_ids),
            )

    # -- limits ------------------------------------------------------------
    # Checked cheapest and most specific first, so the reason returned is the
    # most useful one rather than whichever limit happened to be tested first.
    per_request = int(consent["per_request_limit_paise"])
    if 0 < per_request < amount_paise:
        raise ConsentDenied(
            "per_request_limit_exceeded",
            f"{amount_paise} paise exceeds the per-request limit of {per_request} paise",
            limitPaise=per_request,
            amountPaise=amount_paise,
        )

    daily = int(consent["daily_limit_paise"])
    remaining_daily = UNLIMITED_PAISE
    if daily > 0:
        remaining_daily = max(daily - committed_today_paise, 0)
        if amount_paise > remaining_daily:
            raise ConsentDenied(
                "daily_limit_exceeded",
                f"{amount_paise} paise exceeds the {remaining_daily} paise remaining today "
                f"under a daily limit of {daily} paise",
                limitPaise=daily,
                committedPaise=committed_today_paise,
                remainingPaise=remaining_daily,
            )

    # The lifetime ceiling. `consumed_paise` counts captured spend and
    # `reserved_paise` counts amounts held for in-flight requests — both must
    # be charged against the total, or two concurrent requests each see room
    # that only one of them can actually have.
    total = int(consent["total_limit_paise"])
    remaining_total = UNLIMITED_PAISE
    if total > 0:
        used = int(_field(consent, "consumed_paise", 0)) + int(
            _field(consent, "reserved_paise", 0)
        )
        remaining_total = max(total - used, 0)
        if amount_paise > remaining_total:
            raise ConsentDenied(
                "total_limit_exceeded",
                f"{amount_paise} paise exceeds the {remaining_total} paise remaining "
                f"under a total consent limit of {total} paise",
                limitPaise=total,
                usedPaise=used,
                remainingPaise=remaining_total,
            )

    return ConsentDecision(
        consent_id=consent["consent_id"],
        operator_id=operator["operator_id"],
        agent_id=consent["agent_id"],
        amount_paise=amount_paise,
        remaining_daily_paise=remaining_daily,
        remaining_total_paise=remaining_total,
    )
