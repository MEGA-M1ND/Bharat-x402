"""Offer signing and payment-proof verification.

===========================================================================
THE SUBSTITUTION THIS FILE MAKES — read before judging the crypto
===========================================================================
Real x402 on EVM proves payment with an EIP-3009 `transferWithAuthorization`
signature: the agent signs with a private key nobody else holds, and the
facilitator verifies against the corresponding public address. Only the agent
can produce that signature, so the proof is non-repudiable.

This project substitutes **HMAC-SHA256 with a shared secret**, and that is a
real downgrade, not a cosmetic one:

  * The facilitator can forge any agent's commitment, because it holds the same
    key it verifies with. There is no non-repudiation — a publisher and an
    agent who disagree about a charge cannot be adjudicated from the proof.
  * Every participant sharing one secret means compromise anywhere is
    compromise everywhere.

It is used here because it keeps the demo to one dependency-free file and
keeps attention on the settlement economics, which is the actual contribution.
Production would issue each agent a keypair (Ed25519 is the obvious pick for a
non-EVM rail) and verify signatures against a registered public key, at which
point this module changes and nothing else does.

What the demo *does* get right, and would keep in production:

  * Constant-time comparison, so signature checking cannot be timed.
  * Canonical serialisation, so the same logical offer always signs the same
    bytes — the classic source of signature bugs.
  * Offer expiry, so a quote cannot be replayed a week later.
  * Single-use offers, enforced in the ledger by a UNIQUE constraint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class VerificationError(Exception):
    """A payment proof was rejected.

    Carries an x402-style `reason` code alongside a human message so the
    facilitator can answer /verify with a machine-readable `invalidReason` and
    still log something a person can read.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def canonical_json(payload: dict) -> str:
    """Serialises a dict to the exact bytes that get signed.

    Sorted keys and no incidental whitespace, so a dict that round-trips
    through JSON, a database, or another language still signs identically.
    Signature schemes break on this more often than on the crypto itself.

    Args:
        payload: The object to serialise.

    Returns:
        Compact JSON with sorted keys.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sign(payload: dict, secret: str) -> str:
    """HMAC-SHA256 over the canonical form of `payload`.

    Args:
        payload: What to sign.
        secret: Shared secret.

    Returns:
        Lowercase hex digest.
    """
    return hmac.new(
        secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_signature(payload: dict, signature: str, secret: str) -> bool:
    """Checks a signature in constant time.

    `compare_digest` rather than `==`: a naive comparison leaks how many
    leading bytes matched, which is enough to forge a digest one byte at a
    time given enough attempts.

    Args:
        payload: The signed object.
        signature: Signature to check.
        secret: Shared secret.

    Returns:
        Whether the signature is valid.
    """
    return hmac.compare_digest(sign(payload, secret), signature or "")


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------


@dataclass
class OfferPolicy:
    """Rules the facilitator applies when quoting.

    Attributes:
        ttl_seconds: How long a quote stays valid.
        scheme: Payment scheme this facilitator issues offers for.
        network: Network id it settles on.
        asset: Currency code.
        max_amount_paise: Refuse to quote above this. A guard against a
            misconfigured resource server asking for ₹10,00,000 a request —
            cheap to add, and the sort of limit a real facilitator needs.
    """

    ttl_seconds: int = 300
    scheme: str = "razorpay-inr"
    network: str = "razorpay:inr-test"
    asset: str = "INR"
    max_amount_paise: int = 100_000  # ₹1,000


def build_offer(
    *,
    agent_id: str,
    resource_id: str,
    amount_paise: int,
    pay_to: str,
    policy: OfferPolicy,
    resource_url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Builds an unsigned offer body.

    Args:
        agent_id: Agent the quote is for. An offer is bound to one agent so it
            cannot be handed to a third party.
        resource_id: What is being bought.
        amount_paise: Price in paise.
        pay_to: Publisher's settlement account.
        policy: Quoting rules.
        resource_url: Optional URL of the resource.
        now: Injectable clock, for tests.

    Returns:
        The offer body, ready to sign.

    Raises:
        VerificationError: If the request is not quotable.
    """
    now = now or datetime.now(UTC)

    if amount_paise <= 0:
        raise VerificationError("invalid_amount", "Amount must be a positive number of paise.")
    if amount_paise > policy.max_amount_paise:
        raise VerificationError(
            "amount_too_large",
            f"Amount {amount_paise} paise exceeds this facilitator's per-request ceiling "
            f"of {policy.max_amount_paise} paise.",
        )
    if not agent_id:
        raise VerificationError("missing_agent_id", "An agent id is required to quote an offer.")

    expires = now + timedelta(seconds=policy.ttl_seconds)

    return {
        "offerId": f"off_{uuid.uuid4().hex[:20]}",
        "agentId": agent_id,
        "resourceId": resource_id,
        "resourceUrl": resource_url,
        "amountPaise": int(amount_paise),
        "asset": policy.asset,
        "scheme": policy.scheme,
        "network": policy.network,
        "payTo": pay_to,
        # Makes each offer unique even for identical parameters, so two
        # requests for the same resource in the same second cannot collide.
        "nonce": secrets.token_hex(16),
        "issuedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expiresAt": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def agent_commitment_body(offer: dict, accepted_at: str) -> dict:
    """The exact object an agent signs to accept an offer.

    Deliberately a narrow subset of the offer rather than the whole thing: the
    agent commits to *who, what, how much, and when*, and to the nonce that
    makes this acceptance unique. Both sides build this identically, which is
    what lets the facilitator recompute and compare.

    Args:
        offer: The offer being accepted.
        accepted_at: When the agent accepted, ISO-8601.

    Returns:
        The canonical commitment body.
    """
    return {
        "offerId": offer["offerId"],
        "agentId": offer["agentId"],
        "amountPaise": int(offer["amountPaise"]),
        "asset": offer["asset"],
        "payTo": offer["payTo"],
        "nonce": offer["nonce"],
        "acceptedAt": accepted_at,
    }


def parse_iso(value: str) -> datetime:
    """Parses one of our ISO-8601 UTC timestamps.

    Args:
        value: Timestamp string, `Z` suffix accepted.

    Returns:
        A timezone-aware datetime.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_payment_proof(
    *,
    payload: dict,
    offer_row: dict | None,
    requirements: dict,
    secret: str,
    now: datetime | None = None,
) -> dict:
    """Checks an agent's payment proof against the offer it claims to satisfy.

    Ordered cheapest-check-first, and every rejection carries a distinct reason
    code so the ledger records *why* rather than a generic failure.

    Args:
        payload: The x402 `payload` object from the agent.
        offer_row: The stored offer, or None if unknown.
        requirements: The x402 `paymentRequirements` the resource server is
            enforcing. Checked against the offer so an agent cannot present a
            valid ₹1 offer to satisfy a ₹500 requirement.
        secret: Shared HMAC secret.
        now: Injectable clock, for tests.

    Returns:
        `{"offerId": ..., "agentId": ..., "amountPaise": ...}` on success.

    Raises:
        VerificationError: With a reason code, on any failure.
    """
    now = now or datetime.now(UTC)

    offer_id = payload.get("offerId")
    if not offer_id:
        raise VerificationError("malformed_payload", "Payment payload has no offerId.")

    agent_signature = payload.get("agentSignature")
    if not agent_signature:
        raise VerificationError("malformed_payload", "Payment payload has no agentSignature.")

    accepted_at = payload.get("acceptedAt")
    if not accepted_at:
        raise VerificationError("malformed_payload", "Payment payload has no acceptedAt.")

    if offer_row is None:
        raise VerificationError(
            "unknown_offer",
            f"Offer {offer_id} was never issued by this facilitator.",
        )

    # Rebuild the offer from the ledger rather than trusting anything the agent
    # sent. The agent's copy is a claim; ours is the record.
    offer = {
        "offerId": offer_row["offer_id"],
        "agentId": offer_row["agent_id"],
        "resourceId": offer_row["resource_id"],
        "resourceUrl": offer_row["resource_url"],
        "amountPaise": int(offer_row["amount_paise"]),
        "asset": offer_row["asset"],
        "scheme": offer_row["scheme"],
        "network": offer_row["network"],
        "payTo": offer_row["pay_to"],
        "nonce": offer_row["nonce"],
        "issuedAt": offer_row["issued_at"],
        "expiresAt": offer_row["expires_at"],
    }

    if not verify_signature(offer, offer_row["signature"], secret):
        # Our own stored signature does not match the stored offer: the ledger
        # has been edited underneath us. Refuse rather than settle.
        raise VerificationError(
            "offer_tampered",
            f"Stored offer {offer_id} fails its own signature check — ledger integrity issue.",
        )

    if parse_iso(offer["expiresAt"]) < now:
        raise VerificationError(
            "offer_expired",
            f"Offer {offer_id} expired at {offer['expiresAt']}.",
        )

    expected = agent_commitment_body(offer, accepted_at)
    if not verify_signature(expected, agent_signature, secret):
        raise VerificationError(
            "invalid_signature",
            "Agent signature does not match the offer it claims to accept. "
            "Either the payload was altered in transit or it was signed with the wrong key.",
        )

    # An offer for ₹1 must not satisfy a requirement for ₹5. Checked explicitly
    # because the resource server and the facilitator are separate services and
    # could drift.
    required_amount = int(requirements.get("amount", 0))
    if int(offer["amountPaise"]) != required_amount:
        raise VerificationError(
            "amount_mismatch",
            f"Offer is for {offer['amountPaise']} paise but the resource requires "
            f"{required_amount} paise.",
        )

    if requirements.get("asset") and requirements["asset"] != offer["asset"]:
        raise VerificationError(
            "asset_mismatch",
            f"Offer settles {offer['asset']}, resource requires {requirements['asset']}.",
        )

    if requirements.get("payTo") and requirements["payTo"] != offer["payTo"]:
        raise VerificationError(
            "recipient_mismatch",
            f"Offer pays {offer['payTo']} but the resource expects {requirements['payTo']}. "
            "A proof issued for one publisher cannot be spent at another.",
        )

    if requirements.get("scheme") and requirements["scheme"] != offer["scheme"]:
        raise VerificationError(
            "scheme_mismatch",
            f"Offer uses scheme {offer['scheme']}, resource requires {requirements['scheme']}.",
        )

    if requirements.get("network") and requirements["network"] != offer["network"]:
        raise VerificationError(
            "network_mismatch",
            f"Offer settles on {offer['network']}, resource requires {requirements['network']}.",
        )

    return {
        "offerId": offer["offerId"],
        "agentId": offer["agentId"],
        "resourceId": offer["resourceId"],
        "amountPaise": int(offer["amountPaise"]),
        "asset": offer["asset"],
        "offerStatus": offer_row["status"],
    }
