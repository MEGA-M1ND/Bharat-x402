"""CI check: an agent can pay in INR and unlock the resource.

Run against live services (see .github/workflows/ci.yml). Deliberately a
standalone script rather than a pytest case — it needs two servers running,
which is a different kind of test from the unit suite in test_full_flow.py.

Exits non-zero with a readable message on failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime

import httpx

RESOURCE = os.getenv("RESOURCE_URL", "http://localhost:3402/premium/market-report")
FACILITATOR = os.getenv("FACILITATOR_URL", "http://localhost:8402")
SECRET = os.getenv("X402_HMAC_SECRET", "dev-only-shared-secret-change-me")
AGENT_ID = "agent-ci-smoke"


def sign(body: dict) -> str:
    """HMAC-SHA256 over canonical JSON — must match the facilitator exactly."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # 1 — unpaid request is refused with an offer.
    unpaid = httpx.get(RESOURCE, timeout=15)
    if unpaid.status_code != 402:
        fail(f"expected 402 for an unpaid request, got {unpaid.status_code}")

    required = json.loads(base64.b64decode(unpaid.headers["payment-required"]))
    accepted = required["accepts"][0]
    print(f"  402 offer: {accepted['extra']['humanAmount']} ({accepted['amount']} paise)")

    # 2 — ask the facilitator to quote us.
    quote = httpx.post(
        f"{FACILITATOR}/offer",
        json={
            "agentId": AGENT_ID,
            "resourceId": "market-report-2026-08",
            "amountPaise": int(accepted["amount"]),
            "payTo": accepted["payTo"],
            "resourceUrl": RESOURCE,
        },
        timeout=15,
    )
    if quote.status_code != 200:
        fail(f"/offer returned {quote.status_code}: {quote.text[:200]}")
    quoted = quote.json()
    print(f"  offer issued: {quoted['offer']['offerId']}")

    # 3 — sign acceptance of the quote.
    accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    commitment = dict(quoted["commitmentTemplate"])
    commitment["acceptedAt"] = accepted_at

    payload = {
        "offerId": quoted["offer"]["offerId"],
        "agentId": AGENT_ID,
        "acceptedAt": accepted_at,
        "agentSignature": sign(commitment),
    }
    header = base64.b64encode(
        json.dumps({"x402Version": 2, "accepted": accepted, "payload": payload}).encode()
    ).decode()

    # 4 — retry with payment.
    paid = httpx.get(RESOURCE, headers={"X-PAYMENT": header}, timeout=20)
    if paid.status_code != 200:
        fail(f"expected 200 for a paid request, got {paid.status_code}: {paid.text[:300]}")

    content = paid.json()
    if "findings" not in content:
        fail(f"paid response is missing the gated content: {list(content)}")
    print(f"  content unlocked: {content['title']}")

    # 5 — a settlement receipt must come back, under either header name.
    raw_receipt = paid.headers.get("payment-response") or paid.headers.get("x-payment-response")
    if not raw_receipt:
        fail("paid response carried no settlement receipt header")

    receipt = json.loads(base64.b64decode(raw_receipt))
    if not receipt.get("success"):
        fail(f"settlement did not succeed: {receipt}")
    if not receipt.get("transaction", "").startswith("cmt_"):
        fail(f"expected a deferred commitment id, got {receipt.get('transaction')!r}")
    print(f"  settled: {receipt['transaction']} ({receipt['extra']['settlementMode']})")

    # 6 — a forged signature must not unlock anything.
    forged_payload = dict(payload, agentSignature="0" * 64)
    forged_header = base64.b64encode(
        json.dumps({"x402Version": 2, "accepted": accepted, "payload": forged_payload}).encode()
    ).decode()
    forged = httpx.get(RESOURCE, headers={"X-PAYMENT": forged_header}, timeout=20)
    if forged.status_code == 200:
        fail("a forged signature unlocked the resource")
    print(f"  forged signature correctly rejected ({forged.status_code})")

    # 7 — an offer is single-use, so replaying it must not unlock a second fetch
    #     for free. The facilitator returns the original commitment rather than
    #     creating a second debt, so the agent is charged exactly once.
    replay = httpx.get(RESOURCE, headers={"X-PAYMENT": header}, timeout=20)
    replay_receipt = replay.headers.get("payment-response") or replay.headers.get(
        "x-payment-response"
    )
    if replay.status_code == 200 and replay_receipt:
        decoded = json.loads(base64.b64decode(replay_receipt))
        if decoded.get("transaction") != receipt["transaction"]:
            fail(
                "replaying a spent offer created a second commitment "
                f"({decoded.get('transaction')} != {receipt['transaction']})"
            )
        print("  replay returned the original commitment — charged once, not twice")

    print("PASS: paid flow")


if __name__ == "__main__":
    main()
