"""Bharat x402 — INR facilitator (Phase 0 skeleton).

This service is the new piece. In stock x402 the facilitator verifies an EIP-3009
signature and settles USDC on an EVM chain. This one verifies an HMAC-signed INR
commitment and settles through Razorpay instead.

Phase 0 only proves the service boots; the x402 endpoints land in Phase 2.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

PORT = int(os.getenv("PORT", "8402"))

app = FastAPI(
    title="Bharat x402 Facilitator",
    description="INR settlement facilitator for the x402 agent payment protocol.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    return {"service": "facilitator", "status": "ok"}


@app.get("/")
def root() -> dict:
    return {
        "service": "Bharat x402 INR facilitator",
        "note": "Phase 0 skeleton — x402 endpoints arrive in Phase 2.",
        "routes": ["/health", "/supported", "/verify", "/settle", "/offer", "/settle-batch"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
