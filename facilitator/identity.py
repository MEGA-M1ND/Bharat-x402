"""Operators, merchants, API credentials, and scopes.

WHY THIS EXISTS
---------------
Before this module every operational endpoint on the facilitator was public.
`/ledger/summary`, `/economics`, `/ledger/events`, and `/settle-batch` took an
`agentId` query parameter and returned that agent's data to anyone who asked.
An `agentId` in a query string is a *filter*. It was being used as
authorization, and those are not the same thing — the second one requires
proving you are that party.

The consequence was not theoretical. On a deployed instance, any visitor could
read every publisher's revenue, every agent's spend, and the entire audit
trail, and could trigger a settlement run over somebody else's commitments.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
Scoped bearer API keys with hashed storage, rotation, and revocation. Not
OAuth. OAuth solves delegated third-party authorization — a user granting an
application access to their data at a provider. Nothing here has that shape:
the caller *is* the tenant, there is no consenting resource owner in the loop,
and no third party needs a token minted on someone's behalf. An OAuth server
would be several hundred lines of authorization-code plumbing answering a
question nobody asked, and would be worse security for it, because the part
that actually matters — hashed storage, narrow scopes, tenant isolation, real
revocation — would be diluted across it.

THE PARTS THAT ACTUALLY MATTER
------------------------------
  * **Only hashes are stored.** The plaintext key is returned once, at
    issuance, and is unrecoverable afterwards. Disclosure of the credentials
    table yields nothing an attacker can authenticate with.
  * **Constant-time comparison.** Lookup is by hash, and the hash comparison
    itself uses `compare_digest`, so a timing signal cannot walk a key out.
  * **Scopes are narrow and additive.** A key that reads a ledger cannot
    settle a batch.
  * **The tenant comes from the key, never from the request.** This is the
    whole fix. A caller cannot name the tenant it wants to act as.

WHY SHA-256 AND NOT ARGON2 OR BCRYPT
------------------------------------
A password hash is deliberately slow because passwords are low-entropy and
guessable. These keys are 256 bits from `secrets.token_urlsafe` — there is no
dictionary to attack and no offline guessing advantage to remove. What a slow
KDF *would* add is a hard cost on every authenticated request, which is a
denial-of-service surface rather than a defence.

The relevant distinction is entropy, not hash family. If these were ever
derived from anything a human chose, this reasoning would stop applying and
the hash would have to change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

# Prefix on every issued key. Makes a leaked credential greppable in logs and
# in secret-scanning rules — the same reason Razorpay ships `rzp_test_` and
# GitHub ships `ghp_`. A secret you cannot recognise is a secret you cannot
# revoke when it turns up somewhere it should not be.
KEY_PREFIX = "bx402_"

# Characters of the key kept in the clear for display. Enough for a human to
# tell two keys apart in a listing; far too few to be worth brute-forcing the
# remainder from.
DISPLAY_PREFIX_LENGTH = 12


class AuthError(Exception):
    """Authentication or authorization failed.

    Carries a machine-readable `reason` alongside a human message, matching
    the shape `VerificationError` already uses for payment proofs, so the
    HTTP layer has one pattern for both.
    """

    def __init__(self, reason: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------- scopes ---

# Scope tokens. Deliberately coarse enough to reason about and fine enough
# that a reporting integration does not need the ability to move money.
#
# Read scopes are separated from write scopes because the common case — a
# dashboard, a digest cron, an Agent Studio insights agent — is read-only, and
# handing those a key that can also trigger settlement is how a reporting bug
# becomes a financial incident.
SCOPE_LEDGER_READ = "ledger:read"
SCOPE_ECONOMICS_READ = "economics:read"
SCOPE_EVENTS_READ = "events:read"
SCOPE_AGENTS_WRITE = "agents:write"
SCOPE_CONSENT_READ = "consent:read"
SCOPE_CONSENT_WRITE = "consent:write"
SCOPE_SETTLE_WRITE = "settle:write"
SCOPE_KEYS_WRITE = "keys:write"
SCOPE_ADMIN = "admin"

ALL_SCOPES = (
    SCOPE_LEDGER_READ,
    SCOPE_ECONOMICS_READ,
    SCOPE_EVENTS_READ,
    SCOPE_AGENTS_WRITE,
    SCOPE_CONSENT_READ,
    SCOPE_CONSENT_WRITE,
    SCOPE_SETTLE_WRITE,
    SCOPE_KEYS_WRITE,
    SCOPE_ADMIN,
)

# Sensible defaults for the two tenant kinds, so issuing a key does not
# require thinking about scopes to get a safe result.
OPERATOR_DEFAULT_SCOPES = (
    SCOPE_LEDGER_READ,
    SCOPE_ECONOMICS_READ,
    SCOPE_EVENTS_READ,
    SCOPE_AGENTS_WRITE,
    SCOPE_CONSENT_READ,
    SCOPE_CONSENT_WRITE,
)
MERCHANT_DEFAULT_SCOPES = (
    SCOPE_LEDGER_READ,
    SCOPE_ECONOMICS_READ,
    SCOPE_EVENTS_READ,
    SCOPE_SETTLE_WRITE,
)


def normalise_scopes(scopes: object) -> tuple[str, ...]:
    """Validates and de-duplicates a scope collection.

    Args:
        scopes: An iterable of scope tokens, or a space-separated string.

    Returns:
        Sorted, de-duplicated, validated scope tokens.

    Raises:
        ValueError: If any token is not a known scope. Unknown scopes are
            rejected rather than dropped: a typo that silently produces a key
            with *fewer* permissions than intended is a confusing outage, and
            one that silently produces *more* would be a vulnerability.
    """
    if isinstance(scopes, str):
        tokens = scopes.split()
    else:
        tokens = [str(token) for token in scopes]

    unknown = sorted({token for token in tokens if token not in ALL_SCOPES})
    if unknown:
        raise ValueError(
            f"unknown scope(s): {', '.join(unknown)}. Known scopes: {', '.join(ALL_SCOPES)}"
        )
    return tuple(sorted(set(tokens)))


# ------------------------------------------------------------ key material ---


def generate_api_key() -> str:
    """Mints a new plaintext API key.

    32 bytes from `secrets.token_urlsafe` — a CSPRNG, not `random`. The
    resulting key is shown to its owner exactly once.

    Returns:
        The plaintext key, including its recognisable prefix.
    """
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(plaintext: str) -> str:
    """Hashes a plaintext key for storage and lookup.

    SHA-256, deliberately — see the module docstring on why a slow KDF is the
    wrong tool for a 256-bit random credential.

    Args:
        plaintext: The key as presented by the caller.

    Returns:
        Lowercase hex digest.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def key_display_prefix(plaintext: str) -> str:
    """The non-secret fragment stored for display in listings."""
    return plaintext[:DISPLAY_PREFIX_LENGTH]


def keys_match(presented_hash: str, stored_hash: str) -> bool:
    """Constant-time hash comparison.

    Lookup is already by hash, so an attacker cannot easily time the database
    index — but the final comparison is free to make constant-time and there
    is no reason to leave a timing oracle in the one place a credential is
    checked.
    """
    return hmac.compare_digest(presented_hash, stored_hash)


def parse_bearer(header_value: str | None) -> str:
    """Extracts the token from an `Authorization: Bearer <token>` header.

    Args:
        header_value: Raw header value, or None if absent.

    Returns:
        The token.

    Raises:
        AuthError: If the header is missing or not a Bearer credential.
    """
    if not header_value:
        raise AuthError(
            "missing_credentials",
            "This endpoint requires an API key. Send 'Authorization: Bearer <key>'.",
        )

    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError(
            "malformed_credentials",
            "Authorization header must be of the form 'Bearer <key>'.",
        )
    return parts[1].strip()


# --------------------------------------------------------------- principal ---


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, and the tenant it may act for.

    `operator_id` and `merchant_id` are mutually exclusive — a credential
    belongs to exactly one tenant. Both being None is impossible for an
    authenticated principal and is rejected at issuance.

    Frozen because a principal is derived from a credential at the start of a
    request and must not be edited by anything downstream. A handler that can
    widen its own authority is a handler that will.
    """

    credential_id: str
    operator_id: str | None
    merchant_id: str | None
    scopes: tuple[str, ...]
    label: str

    @property
    def tenant_kind(self) -> str:
        return "operator" if self.operator_id else "merchant"

    @property
    def tenant_id(self) -> str:
        """The single tenant this principal speaks for."""
        tenant = self.operator_id or self.merchant_id
        if tenant is None:  # pragma: no cover - rejected at issuance
            raise AuthError("invalid_principal", "credential belongs to no tenant", 500)
        return tenant

    def has_scope(self, scope: str) -> bool:
        """Whether this principal carries `scope`.

        `admin` implies everything. It exists so a demo or a break-glass
        operator key does not need nine tokens listed, and is never a default.
        """
        return SCOPE_ADMIN in self.scopes or scope in self.scopes

    def require(self, scope: str) -> None:
        """Raises unless this principal carries `scope`.

        Raises:
            AuthError: 403, with the missing scope named. Naming it is safe —
                the caller already authenticated — and turns an opaque denial
                into something actionable.
        """
        if not self.has_scope(scope):
            raise AuthError(
                "insufficient_scope",
                f"This key lacks the '{scope}' scope. It has: {', '.join(self.scopes) or 'none'}.",
                403,
            )

    def require_operator(self) -> str:
        """The operator id, or 403 if this is a merchant key."""
        if not self.operator_id:
            raise AuthError(
                "wrong_tenant_kind",
                "This operation requires an operator key; a merchant key was presented.",
                403,
            )
        return self.operator_id

    def require_merchant(self) -> str:
        """The merchant id, or 403 if this is an operator key."""
        if not self.merchant_id:
            raise AuthError(
                "wrong_tenant_kind",
                "This operation requires a merchant key; an operator key was presented.",
                403,
            )
        return self.merchant_id
