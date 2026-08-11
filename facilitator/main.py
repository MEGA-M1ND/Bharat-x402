"""Bharat x402 — INR facilitator.

This service is the new piece. In stock x402 the facilitator verifies an
EIP-3009 signature and settles USDC on an EVM chain. This one verifies an
HMAC-signed INR commitment and settles through Razorpay instead.

It speaks the *real* x402 facilitator contract:

    GET  /supported   what schemes/networks this facilitator handles
    POST /verify      is this payment payload good?
    POST /settle      make it final

plus two endpoints specific to the INR problem:

    POST /offer        quote a signed payment offer to an agent
    POST /settle-batch aggregate a day of commitments into one Razorpay charge

Phase 1 implements /supported, which is what the resource server calls at
startup to discover us. /verify, /settle, /offer and /settle-batch arrive in
Phase 2.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

load_dotenv()

PORT = int(os.getenv("PORT", "8402"))

# CAIP-style network id. `razorpay` is the namespace — an off-chain settlement
# rail rather than a blockchain — and `inr-test` the reference.
X402_NETWORK = os.getenv("X402_NETWORK", "razorpay:inr-test")

# Our scheme name. Stock x402 uses "exact" for EVM transfers.
X402_SCHEME = "razorpay-inr"

# Protocol version implemented by @x402/core 2.x.
X402_VERSION = 2

# deferred: book each request as a commitment, charge one batch later.
# per_request: hit Razorpay on every single request.
SETTLEMENT_MODE = os.getenv("SETTLEMENT_MODE", "deferred")

app = FastAPI(
    title="Bharat x402 Facilitator",
    description="INR settlement facilitator for the x402 agent payment protocol.",
    version="0.2.0",
)


@app.get("/health")
def health() -> dict:
    return {"service": "facilitator", "status": "ok", "settlementMode": SETTLEMENT_MODE}


@app.get("/supported")
def supported() -> dict:
    """Advertise what this facilitator can settle.

    This is the standard x402 discovery endpoint. The resource server calls it
    at startup and refuses to price a route whose scheme/network we do not
    claim here — which is the mechanism that makes x402 facilitator-agnostic in
    practice, not just on paper.

    `extra` is passed through to the client inside the 402 body, so agents learn
    the settlement mode from the facilitator actually running, rather than from
    something hardcoded in the publisher's config.
    """
    return {
        "kinds": [
            {
                "x402Version": X402_VERSION,
                "scheme": X402_SCHEME,
                "network": X402_NETWORK,
                "extra": {
                    "currency": "INR",
                    "decimals": 2,  # amounts travel as integer paise
                    "settlementRail": "razorpay",
                    "settlementMode": SETTLEMENT_MODE,
                    # Named honestly: this is not the EVM signature scheme.
                    "proofScheme": "hmac-sha256",
                },
            }
        ],
        # No x402 protocol extensions (bazaar, sign-in-with-x, ...) implemented.
        "extensions": [],
        # For EVM facilitators this maps network family to signer addresses. Our
        # equivalent is the merchant account rupees land in.
        "signers": {"razorpay:*": ["acc_BharatX402TestFacilitator"]},
    }


@app.post("/verify")
def verify_not_implemented() -> JSONResponse:
    """Placeholder until Phase 2.

    Returns the schema-correct failure shape rather than a bare 500, so the
    resource server surfaces a real reason instead of a transport error.
    """
    return JSONResponse(
        status_code=501,
        content={
            "isValid": False,
            "invalidReason": "unexpected_verify_error",
            "invalidMessage": "Verification lands in Phase 2.",
        },
    )


@app.post("/settle")
def settle_not_implemented() -> JSONResponse:
    """Placeholder until Phase 2."""
    return JSONResponse(
        status_code=501,
        content={
            "success": False,
            "errorReason": "unexpected_settle_error",
            "errorMessage": "Settlement lands in Phase 2.",
            "transaction": "",
            "network": X402_NETWORK,
        },
    )


@app.get("/")
def root() -> dict:
    return {
        "service": "Bharat x402 INR facilitator",
        "scheme": X402_SCHEME,
        "network": X402_NETWORK,
        "settlementMode": SETTLEMENT_MODE,
        "x402Endpoints": ["/supported", "/verify", "/settle"],
        "inrEndpoints": ["/offer", "/settle-batch"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
