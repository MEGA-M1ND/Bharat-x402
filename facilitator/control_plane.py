"""The authenticated control plane: operators, merchants, keys, and consent.

FOUR PLANES, AND WHY THEY ARE SEPARATE
--------------------------------------
This service answers to four different kinds of caller, and collapsing them
into one surface is what produced the hole this module closes.

  1. **Public protocol plane** — `/supported`, `/offer`, `/verify`, `/settle`,
     `/health`. Open by design. These *are* the x402 contract, and they are
     authenticated by the thing they carry: a signed acceptance the
     facilitator verifies against a registered key. Requiring a bearer token
     here would break every conforming x402 client for no security gain.

  2. **Gateway callback plane** — `/webhooks/razorpay`. Open, because Razorpay
     cannot hold our API key, and authenticated instead by an HMAC over the
     raw body under a shared secret. Fails closed with no secret configured.

  3. **Agent-authorized plane** — `/agents/{id}/limits`. Reports one agent its
     own headroom, which it already knows.

  4. **Operator / merchant control plane** — everything here, plus the ledger
     reads and settlement runs in `main.py`. Requires a scoped API key, and
     the tenant is taken **from the key**, never from the request.

That last sentence is the entire fix. Before this, `/ledger/summary?agentId=X`
returned X's data to anyone who asked, and `/settle-batch` would run a
settlement over commitments belonging to whoever was named. An id in a query
string is a filter. It was doing the job of authorization, and a filter cannot
do that job — anyone can type a different value.

WHY THERE IS NO ADMIN FREEZE ENDPOINT HERE
------------------------------------------
`limits.py` deliberately exposes the emergency stop as an environment
variable, on the grounds that an unauthenticated `POST /agents/{id}/freeze`
would be a denial-of-service primitive rather than a safety control. That
reasoning was correct *given no authentication*. Now that a control plane
exists, the same operation is offered here as
`POST /control/agents/{id}/status`, scoped to the owning operator: an operator
may suspend its own agents and nobody else's. The env var stays as the
platform-wide break-glass, because a control plane that is itself unreachable
is exactly when you need one.

BOOTSTRAP
---------
The first credential has to come from somewhere. `CONTROL_PLANE_BOOTSTRAP_TOKEN`
authorises operator and merchant creation and nothing else. Unset, tenant
creation is refused entirely — the secure default is "no way in" rather than
"open until configured", which is the same fail-closed discipline the webhook
handler uses.
"""

from __future__ import annotations

import hmac
import os
import secrets
import uuid
from typing import Any

import identity
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from identity import AuthError, Principal
from pydantic import BaseModel, Field

router = APIRouter(prefix="/control", tags=["control-plane"])

# How long a proof-of-possession challenge stays claimable. Short: the agent
# holding the key is signing a nonce it just asked for, so this is a network
# round trip, not a human workflow.
ENROLLMENT_CHALLENGE_TTL_SECONDS = int(os.getenv("ENROLLMENT_CHALLENGE_TTL_SECONDS", "300"))


def bootstrap_token() -> str:
    """The token that authorises creating tenants, or "" if unset.

    Read on each call rather than captured at import so tests and a running
    process can change it without reloading the module.
    """
    return (os.getenv("CONTROL_PLANE_BOOTSTRAP_TOKEN") or "").strip()


def require_bootstrap(header_value: str | None) -> None:
    """Gates tenant creation behind the bootstrap token.

    Raises:
        AuthError: If no token is configured (fail closed — an unset secret
            must not mean "anyone may create an operator"), or if the
            presented token does not match.
    """
    expected = bootstrap_token()
    if not expected:
        raise AuthError(
            "bootstrap_disabled",
            "Tenant creation is disabled: CONTROL_PLANE_BOOTSTRAP_TOKEN is not configured.",
            403,
        )
    presented = identity.parse_bearer(header_value)
    if not hmac.compare_digest(presented, expected):
        raise AuthError("invalid_bootstrap_token", "Bootstrap token rejected.", 403)


def authenticate(ledger: Any, authorization: str | None) -> Principal:
    """Resolves a bearer token to the tenant it speaks for.

    The lookup is by hash. The plaintext key is never stored, so this is the
    only way a presented key can be matched at all — which is the point.

    Args:
        ledger: The ledger to look the credential up in.
        authorization: Raw `Authorization` header value.

    Returns:
        The authenticated `Principal`.

    Raises:
        AuthError: 401 if the header is missing, malformed, or the key is
            unknown or revoked. All of those return the same message: telling
            an unauthenticated caller *which* of those it was is free
            reconnaissance.
    """
    token = identity.parse_bearer(authorization)
    row = ledger.find_api_credential(identity.hash_api_key(token))

    if row is None or not identity.keys_match(
        identity.hash_api_key(token), row["key_hash"]
    ):
        raise AuthError("invalid_credentials", "API key not recognised.", 401)

    ledger.touch_api_credential(row["credential_id"])
    return Principal(
        credential_id=row["credential_id"],
        operator_id=row["operator_id"],
        merchant_id=row["merchant_id"],
        scopes=tuple((row["scopes"] or "").split()),
        label=row["label"],
    )


def auth_error_response(exc: AuthError) -> JSONResponse:
    """Renders an `AuthError` as JSON.

    `WWW-Authenticate` on a 401 is what tells a well-behaved client it needs
    to present a credential rather than that the resource is gone.
    """
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.reason, "message": exc.message},
        headers=headers,
    )


# ------------------------------------------------------------- request bodies


class OperatorCreate(BaseModel):
    displayName: str = Field(min_length=1, max_length=120)
    operatorId: str | None = Field(default=None, pattern=r"^op_[a-z0-9][a-z0-9-]{0,48}$")


class MerchantCreate(BaseModel):
    displayName: str = Field(min_length=1, max_length=120)
    merchantId: str | None = Field(default=None, pattern=r"^mer_[a-z0-9][a-z0-9-]{0,48}$")
    settlementAccountReference: str | None = Field(default=None, max_length=120)


class KeyIssue(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    # Which tenant the new key speaks for. Exactly one must be given.
    operatorId: str | None = None
    merchantId: str | None = None
    scopes: list[str] | None = None


class ChallengeRequest(BaseModel):
    # Same shape the public /agents/register endpoint validates, so an id that
    # works on one path works on the other.
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class EnrollRequest(BaseModel):
    challengeId: str
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    publicKey: str = Field(min_length=40, max_length=100)
    algorithm: str = "ed25519"
    # Signature over the challenge nonce, proving possession of the private
    # half. Without this the "enrollment" would be trust-on-first-use wearing
    # an operator's name.
    challengeSignature: str
    validUntil: str | None = None
    rotate: bool = False


class ConsentCreate(BaseModel):
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    # 0 means "no limit configured for this dimension" — see
    # ledger.create_consent. Negative values are refused outright.
    perRequestLimitPaise: int = Field(default=0, ge=0)
    dailyLimitPaise: int = Field(default=0, ge=0)
    totalLimitPaise: int = Field(default=0, ge=0)
    validFrom: str | None = None
    validUntil: str | None = None
    merchantIds: list[str] | None = None


class StatusChange(BaseModel):
    status: str = Field(pattern=r"^(active|suspended|revoked|closed)$")


# ------------------------------------------------------------------- routes
#
# Every handler takes `request` so it can reach the ledger from app state.
# Passing the ledger through `request.app.state` rather than importing it from
# main.py keeps this module importable on its own, which is what lets the
# tests build a router against a throwaway database.


def _ledger(request: Request) -> Any:
    return request.app.state.ledger


def _principal(request: Request, authorization: str | None) -> Principal:
    return authenticate(_ledger(request), authorization)


@router.post("/operators")
def create_operator(
    body: OperatorCreate, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Creates an operator. Gated by the bootstrap token, not an API key."""
    try:
        require_bootstrap(authorization)
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    operator_id = body.operatorId or f"op_{uuid.uuid4().hex[:16]}"
    try:
        created = ledger.create_operator(
            operator_id=operator_id, display_name=body.displayName
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"error": "conflict", "message": str(exc)})

    ledger.log_event("operator_created", status="ok", operatorId=operator_id)
    return JSONResponse(status_code=201, content=created)


@router.post("/merchants")
def create_merchant(
    body: MerchantCreate, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Creates a publisher. Gated by the bootstrap token."""
    try:
        require_bootstrap(authorization)
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    merchant_id = body.merchantId or f"mer_{uuid.uuid4().hex[:16]}"
    try:
        created = ledger.create_merchant(
            merchant_id=merchant_id,
            display_name=body.displayName,
            settlement_account_reference=body.settlementAccountReference,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"error": "conflict", "message": str(exc)})

    ledger.log_event("merchant_created", status="ok", merchantId=merchant_id)
    return JSONResponse(status_code=201, content=created)


@router.post("/keys")
def issue_key(
    body: KeyIssue, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Issues a scoped API key.

    Two ways in, and they authorise different things:

      * **Bootstrap token** — may issue a key for any tenant. This is how the
        first key for a new operator is created, since that operator has no
        key yet to authenticate with.
      * **An existing key with `keys:write`** — may issue further keys **for
        its own tenant only**. The tenant is taken from the authenticated
        principal, so a caller cannot mint a credential for somebody else.

    The plaintext is returned **once**, here, and never again. Only its hash is
    stored.
    """
    ledger = _ledger(request)
    presented = (authorization or "").split(None, 1)
    is_bootstrap = (
        len(presented) == 2
        and bool(bootstrap_token())
        and hmac.compare_digest(presented[1].strip(), bootstrap_token())
    )

    if is_bootstrap:
        operator_id, merchant_id = body.operatorId, body.merchantId
    else:
        try:
            principal = _principal(request, authorization)
            principal.require(identity.SCOPE_KEYS_WRITE)
        except AuthError as exc:
            return auth_error_response(exc)
        # Ignore whatever the body asked for. A key may only ever beget a key
        # for its own tenant; honouring the body here would be a privilege
        # escalation with a JSON field as the exploit.
        operator_id = principal.operator_id
        merchant_id = principal.merchant_id

    if bool(operator_id) == bool(merchant_id):
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_tenant",
                "message": "Give exactly one of operatorId or merchantId.",
            },
        )

    if operator_id and ledger.get_operator(operator_id) is None:
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_operator", "message": f"no operator {operator_id}"},
        )
    if merchant_id and ledger.get_merchant(merchant_id) is None:
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_merchant", "message": f"no merchant {merchant_id}"},
        )

    default_scopes = (
        identity.OPERATOR_DEFAULT_SCOPES if operator_id else identity.MERCHANT_DEFAULT_SCOPES
    )
    try:
        scopes = identity.normalise_scopes(
            body.scopes if body.scopes is not None else default_scopes
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400, content={"error": "unknown_scope", "message": str(exc)}
        )

    plaintext = identity.generate_api_key()
    record = ledger.create_api_credential(
        credential_id=f"cred_{uuid.uuid4().hex[:16]}",
        operator_id=operator_id,
        merchant_id=merchant_id,
        label=body.label,
        key_prefix=identity.key_display_prefix(plaintext),
        key_hash=identity.hash_api_key(plaintext),
        scopes=" ".join(scopes),
    )

    # The event records that a key was issued and to whom. It must never
    # record the key, or the audit trail becomes the credential store the
    # hashing was meant to avoid.
    ledger.log_event(
        "api_key_issued",
        status="ok",
        credentialId=record["credentialId"],
        operatorId=operator_id,
        merchantId=merchant_id,
        scopes=" ".join(scopes),
    )

    return JSONResponse(
        status_code=201,
        content={
            **record,
            "apiKey": plaintext,
            "warning": "Store this key now. Only its hash is kept; it cannot be shown again.",
        },
    )


@router.get("/keys")
def list_keys(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Lists this tenant's keys. Never returns hashes or plaintext."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_KEYS_WRITE)
    except AuthError as exc:
        return auth_error_response(exc)

    return JSONResponse(
        status_code=200,
        content={"credentials": _ledger(request).list_api_credentials(
            tenant_id=principal.tenant_id
        )},
    )


@router.post("/keys/{credential_id}/revoke")
def revoke_key(
    credential_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Revokes one of this tenant's keys, immediately."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_KEYS_WRITE)
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    if not ledger.revoke_api_credential(
        credential_id=credential_id, tenant_id=principal.tenant_id
    ):
        # 404 rather than 403 for another tenant's credential id. Confirming
        # that an id exists but belongs to someone else is an oracle for
        # enumerating other tenants' credentials.
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "No such active credential."},
        )

    ledger.log_event(
        "api_key_revoked", status="ok", credentialId=credential_id,
        tenantId=principal.tenant_id,
    )
    return JSONResponse(status_code=200, content={"credentialId": credential_id,
                                                  "status": "revoked"})


@router.post("/agents/challenge")
def agent_challenge(
    body: ChallengeRequest, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Issues a nonce the agent must sign to prove it holds the private key."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    challenge = _ledger(request).create_enrollment_challenge(
        challenge_id=f"chal_{uuid.uuid4().hex[:16]}",
        operator_id=operator_id,
        agent_id=body.agentId,
        nonce=secrets.token_hex(32),
        ttl_seconds=ENROLLMENT_CHALLENGE_TTL_SECONDS,
    )
    return JSONResponse(
        status_code=201,
        content={
            **challenge,
            "signingInstruction": (
                "Sign the canonical JSON of {\"challenge\": <nonce>} with the agent's "
                "Ed25519 private key and return the signature base64-encoded as "
                "`challengeSignature`. Canonical JSON means sorted keys and no incidental "
                "whitespace — the same serialisation used for payment acceptances, so an "
                "agent needs one signing routine rather than two."
            ),
        },
    )


@router.post("/agents/enroll")
def enroll_agent(
    body: EnrollRequest, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Binds a signing key to an agent, under this operator.

    This is what replaces trust-on-first-use. Three things must hold, and each
    closes a different hole:

      1. The caller is an **authenticated operator** — so an id cannot be
         claimed by whoever asks first.
      2. The challenge is **valid, unexpired, unconsumed, and this operator's**
         — so a nonce cannot be replayed or borrowed.
      3. The **signature over the nonce verifies** against the public key being
         enrolled — so an operator cannot bind a key it does not hold, which
         would let it later repudiate its own agent's acceptances.
    """
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    claimed = ledger.consume_enrollment_challenge(
        challenge_id=body.challengeId, operator_id=operator_id
    )
    if claimed is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_challenge",
                "message": "Challenge is unknown, expired, already used, or not yours.",
            },
        )
    if claimed["agent_id"] != body.agentId:
        return JSONResponse(
            status_code=400,
            content={
                "error": "challenge_agent_mismatch",
                "message": "This challenge was issued for a different agent id.",
            },
        )

    # Proof of possession. Imported here rather than at module scope only
    # because payment_verifier pulls in the whole signing stack, and this
    # module is otherwise dependency-light.
    from payment_verifier import verify_ed25519

    challenge_payload = {"challenge": claimed["nonce"]}
    if not verify_ed25519(challenge_payload, body.challengeSignature, body.publicKey):
        ledger.log_event(
            "agent_enrollment_rejected",
            agent_id=body.agentId,
            status="rejected",
            reason="challenge_signature_invalid",
            operatorId=operator_id,
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "challenge_signature_invalid",
                "message": "The signature does not verify against the public key being enrolled.",
            },
        )

    try:
        enrolled = ledger.enroll_agent_credential(
            credential_id=f"agck_{uuid.uuid4().hex[:16]}",
            agent_id=body.agentId,
            operator_id=operator_id,
            public_key=body.publicKey,
            algorithm=body.algorithm,
            valid_until=body.validUntil,
            rotate=body.rotate,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"error": "conflict", "message": str(exc)})

    ledger.log_event(
        "agent_enrolled",
        agent_id=body.agentId,
        status="ok",
        operatorId=operator_id,
        credentialId=enrolled["credentialId"],
        rotated=enrolled["rotated"],
    )
    return JSONResponse(status_code=201, content=enrolled)


@router.get("/agents")
def list_agents(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)
    return JSONResponse(
        status_code=200,
        content={"agents": _ledger(request).list_agents_for_operator(operator_id)},
    )


@router.get("/agents/{agent_id}/credentials")
def agent_credentials(
    agent_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Full signing-key history for one of this operator's agents.

    Includes superseded and revoked credentials on purpose: that history is
    what lets an old acceptance be verified against the key that signed it
    after a rotation.
    """
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    agent = ledger.get_agent(agent_id)
    if agent is None or agent["operator_id"] != operator_id:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "message": "No such agent."}
        )
    return JSONResponse(
        status_code=200,
        content={"agentId": agent_id, "credentials": ledger.list_agent_credentials(agent_id)},
    )


@router.post("/agents/{agent_id}/credentials/{credential_id}/revoke")
def revoke_agent_credential(
    agent_id: str,
    credential_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Revokes one signing credential. Past signatures stay verifiable."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    agent = ledger.get_agent(agent_id)
    if agent is None or agent["operator_id"] != operator_id:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "message": "No such agent."}
        )
    if not ledger.revoke_agent_credential(credential_id=credential_id, agent_id=agent_id):
        return JSONResponse(
            status_code=404, content={"error": "not_found", "message": "No such credential."}
        )

    ledger.log_event(
        "agent_credential_revoked",
        agent_id=agent_id,
        status="ok",
        credentialId=credential_id,
        operatorId=operator_id,
    )
    return JSONResponse(
        status_code=200,
        content={
            "credentialId": credential_id,
            "status": "revoked",
            "note": (
                "New authorization is refused immediately. Signatures produced while this "
                "credential was valid remain verifiable against it."
            ),
        },
    )


@router.post("/agents/{agent_id}/status")
def set_agent_status(
    agent_id: str,
    body: StatusChange,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Suspends or resumes one of this operator's agents.

    The authenticated equivalent of `FROZEN_AGENTS`. Safe to expose *because*
    it is authenticated and operator-scoped — the same endpoint without a
    control plane would have let anyone disable anyone's agent.
    """
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_AGENTS_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    if not ledger.set_agent_status(
        agent_id=agent_id, status=body.status, operator_id=operator_id
    ):
        return JSONResponse(
            status_code=404, content={"error": "not_found", "message": "No such agent."}
        )

    ledger.log_event(
        "agent_status_changed",
        agent_id=agent_id,
        status="ok",
        newStatus=body.status,
        operatorId=operator_id,
    )
    return JSONResponse(status_code=200, content={"agentId": agent_id, "status": body.status})


@router.post("/consents")
def create_consent(
    body: ConsentCreate, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Authorises an agent to spend, under limits, until revoked."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_CONSENT_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    ledger = _ledger(request)
    agent = ledger.get_agent(body.agentId)
    if agent is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "unknown_agent",
                "message": f"agent {body.agentId} is not enrolled",
            },
        )
    if agent["operator_id"] is not None and agent["operator_id"] != operator_id:
        return JSONResponse(
            status_code=403,
            content={
                "error": "not_your_agent",
                "message": "That agent belongs to another operator.",
            },
        )

    for merchant_id in body.merchantIds or []:
        if ledger.get_merchant(merchant_id) is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "unknown_merchant",
                    "message": f"no merchant {merchant_id}",
                },
            )

    created = ledger.create_consent(
        consent_id=f"con_{uuid.uuid4().hex[:16]}",
        operator_id=operator_id,
        agent_id=body.agentId,
        per_request_limit_paise=body.perRequestLimitPaise,
        daily_limit_paise=body.dailyLimitPaise,
        total_limit_paise=body.totalLimitPaise,
        valid_from=body.validFrom,
        valid_until=body.validUntil,
        merchant_ids=body.merchantIds,
    )
    ledger.log_event(
        "consent_created",
        agent_id=body.agentId,
        status="ok",
        consentId=created["consentId"],
        operatorId=operator_id,
        dailyLimitPaise=body.dailyLimitPaise,
        totalLimitPaise=body.totalLimitPaise,
    )
    return JSONResponse(status_code=201, content=created)


@router.get("/consents")
def list_consents(
    request: Request,
    agentId: str | None = None,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """This operator's consents. `agentId` narrows; it never widens."""
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_CONSENT_READ)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    return JSONResponse(
        status_code=200,
        content={
            "consents": _ledger(request).list_consents(
                operator_id=operator_id, agent_id=agentId
            )
        },
    )


@router.post("/consents/{consent_id}/status")
def set_consent_status(
    consent_id: str,
    body: StatusChange,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Suspends, resumes, or revokes a consent.

    Revocation is terminal and takes effect on the next authorization. It does
    not unwind commitments already booked: the agent was authorised when it
    spent, and the debt is real. Withdrawing permission going forward and
    erasing what was already owed are different operations, and only the first
    one is this.
    """
    try:
        principal = _principal(request, authorization)
        principal.require(identity.SCOPE_CONSENT_WRITE)
        operator_id = principal.require_operator()
    except AuthError as exc:
        return auth_error_response(exc)

    if body.status == "closed":
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_status",
                "message": "A consent is active, suspended, or revoked.",
            },
        )

    ledger = _ledger(request)
    if not ledger.set_consent_status(
        consent_id=consent_id, operator_id=operator_id, status=body.status
    ):
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": "No such active consent for this operator.",
            },
        )

    ledger.log_event(
        "consent_status_changed",
        status="ok",
        consentId=consent_id,
        newStatus=body.status,
        operatorId=operator_id,
    )
    return JSONResponse(status_code=200, content={"consentId": consent_id, "status": body.status})


@router.get("/whoami")
def whoami(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """What this key is and what it may do.

    Exists so an integrator can check a key's scopes without discovering them
    by being refused — the same reasoning that puts spend limits on
    `/supported`.
    """
    try:
        principal = _principal(request, authorization)
    except AuthError as exc:
        return auth_error_response(exc)

    return JSONResponse(
        status_code=200,
        content={
            "credentialId": principal.credential_id,
            "label": principal.label,
            "tenantKind": principal.tenant_kind,
            "tenantId": principal.tenant_id,
            "scopes": list(principal.scopes),
        },
    )


__all__ = [
    "router",
    "authenticate",
    "auth_error_response",
    "bootstrap_token",
    "AuthError",
    "Principal",
]
