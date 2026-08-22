"""Offer signing and payment-proof verification.

===========================================================================
TWO SIGNATURES, TWO DIFFERENT JOBS — read before judging the crypto
===========================================================================
There are two signed objects in this protocol and they have opposite trust
requirements. Using one primitive for both is the mistake this file used to
make, and the fix is not "HMAC is weak" — HMAC-SHA256 is a perfectly strong
MAC. The problem is *shape*: a MAC's verifier holds the same key the signer
does, so anyone who can check a signature can also produce one.

  1. **The offer** (facilitator -> agent, then back again).
     Signed with HMAC-SHA256 under the facilitator's own secret, and that is
     the *correct* choice, not a leftover. The facilitator is both the signer
     and the only party that ever verifies it — the signature exists so the
     facilitator can detect its own ledger row being edited underneath it.
     Nobody else needs to check it, so there is nothing for a public key to
     buy here, and a symmetric MAC is faster and simpler.

  2. **The commitment** (agent -> facilitator).
     This one is evidence in a dispute: it is the agent saying "I agree to
     owe ₹5 for this fetch". Under a shared secret the facilitator could
     mint that statement itself, so the proof settles nothing — a publisher
     and an agent who disagree about a charge cannot be adjudicated from it.
     Non-repudiation requires that the verifier *cannot* sign, which means
     asymmetric. Real x402 on EVM gets this from an EIP-3009 signature over
     the agent's wallet key; the equivalent for an off-chain rupee rail is a
     plain signing keypair, so this uses **Ed25519**.

Ed25519 specifically, over ECDSA or RSA: no parameter choices to get wrong,
no per-signature nonce to leak a private key when a PRNG misbehaves (which is
how PlayStation 3 and several Bitcoin wallets lost their keys), deterministic
signatures, and 32-byte public keys that fit comfortably in a JSON payload.

THE DOWNGRADE ATTACK, AND WHY THE CLIENT DOES NOT PICK THE ALGORITHM
--------------------------------------------------------------------
`verify_payment_proof` decides which primitive to demand by looking up what
the *facilitator* has on record for that agent — never by reading an
algorithm field out of the attacker-supplied payload. If an agent has
registered a key, an Ed25519 signature is required and an HMAC one is
refused, full stop.

This matters more than it looks. Protocols that let the presenter name their
own algorithm have been broken exactly this way for a decade: JWT's
`alg: none` and its HMAC/RSA confusion bug are the canonical case, where a
token signed with the *public* key as an HMAC secret verified fine. Algorithm
agility is fine; attacker-chosen algorithm agility is a vulnerability.

The HMAC path survives only for agents with no registered key, and only while
`ALLOW_HMAC_FALLBACK` is on — the migration ramp, so existing clients keep
working while they roll keys out. Turning it off makes registration
mandatory. Every fallback verification is logged as a downgrade so the ramp
is visible rather than permanent-by-accident.

What was already right here, and is unchanged:

  * Constant-time comparison, so signature checking cannot be timed.
  * Canonical serialisation, so the same logical offer always signs the same
    bytes — the classic source of signature bugs.
  * Offer expiry, so a quote cannot be replayed a week later.
  * Single-use offers, enforced in the ledger by a UNIQUE constraint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


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
# Ed25519 — the agent's own key
# ---------------------------------------------------------------------------
#
# Keys and signatures travel as base64 of their raw bytes: 32 bytes of public
# key (44 base64 chars), 64 bytes of signature (88 chars). Raw rather than
# PEM or DER because there is exactly one key type here and no algorithm
# identifier to negotiate — see the module docstring on why letting the
# payload name its own algorithm is a vulnerability rather than a feature.

ALGORITHM = "ed25519"


class KeyFormatError(Exception):
    """A key or signature was not decodable as base64 Ed25519 material."""


def generate_keypair() -> tuple[str, str]:
    """Creates a fresh Ed25519 keypair.

    Returns:
        `(private_key_b64, public_key_b64)`, both base64 of raw bytes. The
        private half never leaves the agent that generated it; only the
        public half is registered with the facilitator.
    """
    private = Ed25519PrivateKey.generate()
    return private_key_to_b64(private), public_key_b64_for(private)


def private_key_to_b64(private: Ed25519PrivateKey) -> str:
    """Serialises a private key to base64 of its raw 32 bytes."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    raw = private.private_bytes(
        encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption()
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_b64_for(private: Ed25519PrivateKey) -> str:
    """The base64 public half of a private key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = private.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def load_private_key(private_key_b64: str) -> Ed25519PrivateKey:
    """Parses a base64 raw private key.

    Raises:
        KeyFormatError: If it is not 32 decodable bytes.
    """
    try:
        raw = base64.b64decode(private_key_b64, validate=True)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - every failure means the same thing to the caller
        raise KeyFormatError("Private key must be base64 of 32 raw Ed25519 bytes.") from exc


def load_public_key(public_key_b64: str) -> Ed25519PublicKey:
    """Parses a base64 raw public key.

    Raises:
        KeyFormatError: If it is not 32 decodable bytes.
    """
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
    except Exception as exc:  # noqa: BLE001 - every failure means the same thing to the caller
        raise KeyFormatError("Public key must be base64 of 32 raw Ed25519 bytes.") from exc


def sign_ed25519(payload: dict, private_key_b64: str) -> str:
    """Signs the canonical form of `payload` with an agent's private key.

    Args:
        payload: What to sign.
        private_key_b64: Base64 raw Ed25519 private key.

    Returns:
        Base64 of the 64-byte signature.
    """
    private = load_private_key(private_key_b64)
    signature = private.sign(canonical_json(payload).encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def verify_ed25519(payload: dict, signature_b64: str, public_key_b64: str) -> bool:
    """Checks an Ed25519 signature over the canonical form of `payload`.

    No constant-time note needed here, unlike the HMAC path: verification is
    a public-key operation over public data, and there is no secret in this
    function for a timing side channel to leak.

    Args:
        payload: The signed object.
        signature_b64: Base64 signature to check.
        public_key_b64: The agent's registered base64 public key.

    Returns:
        Whether the signature is valid. A malformed key or signature is a
        failed verification, not an exception — a caller checking a proof
        wants a yes/no, and "unparseable" is a no.
    """
    try:
        public = load_public_key(public_key_b64)
        public.verify(
            base64.b64decode(signature_b64 or "", validate=True),
            canonical_json(payload).encode("utf-8"),
        )
    except (InvalidSignature, KeyFormatError, ValueError, TypeError):
        return False
    return True


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
    agent_key: Mapping[str, Any] | None = None,
    allow_hmac_fallback: bool = True,
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
        secret: The facilitator's own HMAC secret. Always used to check the
            stored offer's integrity; used for the *agent's* signature only on
            the fallback path below.
        agent_key: The agent's registered key row, or None if it has never
            registered. When present, an Ed25519 signature is required and an
            HMAC one is refused — the decision is made from this argument, not
            from anything in `payload`.
        allow_hmac_fallback: Whether an unregistered agent may still pay with
            a shared-secret HMAC signature. The migration ramp; turning it off
            makes key registration mandatory.
        now: Injectable clock, for tests.

    Returns:
        `{"offerId": ..., "agentId": ..., "amountPaise": ..., "proofScheme": ...}`
        on success. `proofScheme` reports which primitive actually verified,
        so the caller can log a fallback as the downgrade it is.

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

    # Which primitive is demanded is decided *here*, from what the facilitator
    # holds on record — never from an algorithm field in the agent-supplied
    # payload. An attacker who can pick the algorithm picks the weakest one.
    if agent_key is not None:
        if agent_key["algorithm"] != ALGORITHM:
            raise VerificationError(
                "unsupported_algorithm",
                f"Agent {offer['agentId']} is registered with unsupported algorithm "
                f"{agent_key['algorithm']!r}; this facilitator verifies {ALGORITHM}.",
            )
        if not verify_ed25519(expected, agent_signature, agent_key["public_key"]):
            raise VerificationError(
                "invalid_signature",
                "Agent signature does not match the offer it claims to accept. "
                "Either the payload was altered in transit or it was not signed by "
                "the key registered to this agent.",
            )
        proof_scheme = ALGORITHM
    else:
        # No registered key. Either accept the legacy shared-secret proof, or
        # refuse outright once the migration is finished.
        if not allow_hmac_fallback:
            raise VerificationError(
                "agent_not_registered",
                f"Agent {offer['agentId']} has no registered signing key. Register an "
                "Ed25519 public key with POST /agents/register before paying.",
            )
        if not verify_signature(expected, agent_signature, secret):
            raise VerificationError(
                "invalid_signature",
                "Agent signature does not match the offer it claims to accept. "
                "Either the payload was altered in transit or it was signed with "
                "the wrong key.",
            )
        proof_scheme = "hmac-sha256"

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
        "proofScheme": proof_scheme,
    }
