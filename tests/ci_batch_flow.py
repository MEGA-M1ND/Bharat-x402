"""CI check: batch settlement produces one charge and the money reconciles.

Generates traffic from several agents, settles, and asserts the batch totals
equal the sum of the commitments they cover. A settlement system that returns
200 but loses a rupee is worse than one that errors, so the assertion is on
the arithmetic, not the status code.

Run against live services (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import UTC, datetime

import httpx

FACILITATOR = os.getenv("FACILITATOR_URL", "http://localhost:8402")
SECRET = os.getenv("X402_HMAC_SECRET", "dev-only-shared-secret-change-me")

# Unique per run. Settlement is global — it sweeps every agent that owes — so
# a test that hardcoded agent ids would pass on a fresh ledger and fail on a
# reused one. Fresh identities keep the assertions exact whatever else is in
# the database.
RUN = secrets.token_hex(3)
AGENTS = [f"agent-ci-alpha-{RUN}", f"agent-ci-beta-{RUN}", f"agent-ci-gamma-{RUN}"]
FETCHES_PER_AGENT = 3
PRICE_PAISE = 500
PAY_TO = "acc_BharatNewsNetwork"


def sign(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def buy_once(agent_id: str) -> str:
    """Runs one offer -> settle cycle directly against the facilitator.

    Returns:
        The commitment id recorded.
    """
    quote = httpx.post(
        f"{FACILITATOR}/offer",
        json={
            "agentId": agent_id,
            "resourceId": "market-report-2026-08",
            "amountPaise": PRICE_PAISE,
            "payTo": PAY_TO,
        },
        timeout=15,
    ).json()

    offer = quote["offer"]
    accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    commitment = dict(quote["commitmentTemplate"])
    commitment["acceptedAt"] = accepted_at

    requirements = {
        "scheme": offer["scheme"],
        "network": offer["network"],
        "asset": offer["asset"],
        "amount": str(offer["amountPaise"]),
        "payTo": offer["payTo"],
        "maxTimeoutSeconds": 300,
        "extra": {},
    }
    envelope = {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "accepted": requirements,
            "payload": {
                "offerId": offer["offerId"],
                "agentId": agent_id,
                "acceptedAt": accepted_at,
                "agentSignature": sign(commitment),
            },
        },
        "paymentRequirements": requirements,
    }

    settled = httpx.post(f"{FACILITATOR}/settle", json=envelope, timeout=20).json()
    if not settled.get("success"):
        fail(f"settle failed for {agent_id}: {settled}")
    return settled["transaction"]


def main() -> None:
    expected_total = 0
    for agent in AGENTS:
        for _ in range(FETCHES_PER_AGENT):
            buy_once(agent)
            expected_total += PRICE_PAISE
    print(f"  generated {len(AGENTS) * FETCHES_PER_AGENT} commitments worth {expected_total} paise")

    def ours(batches: list[dict]) -> list[dict]:
        return [b for b in batches if b["agentId"] in AGENTS]

    # Dry run first: it must compute the same totals without charging anything.
    dry = httpx.post(f"{FACILITATOR}/settle-batch", json={"dryRun": True}, timeout=30).json()
    dry_total = sum(b["totalPaise"] for b in ours(dry["batches"]))
    if dry_total != expected_total:
        fail(f"dry run totalled {dry_total} paise, expected {expected_total}")
    if any(b.get("paymentLinkId") for b in dry["batches"]):
        fail("dry run created a payment link — it must not charge anything")
    print(f"  dry run reconciles: {dry_total} paise across {len(ours(dry['batches']))} agents")

    # Real batch.
    result = httpx.post(f"{FACILITATOR}/settle-batch", json={}, timeout=30).json()
    batches = ours(result["batches"])
    if not batches:
        fail("settle-batch produced no batches")

    settled_total = 0
    for batch in batches:
        if batch["status"] != "created":
            fail(f"batch {batch.get('batchId')} status is {batch['status']}: {batch.get('error')}")
        if not batch.get("paymentLinkId"):
            fail(f"batch {batch['batchId']} has no payment link")
        settled_total += batch["totalPaise"]

    if settled_total != expected_total:
        fail(f"batches total {settled_total} paise but commitments were {expected_total}")

    # One Razorpay charge per agent, not one per request — the whole idea.
    if len(batches) != len(AGENTS):
        fail(f"expected {len(AGENTS)} batches (one per agent), got {len(batches)}")

    calls_saved = result["aggregate"]["gatewayCallsSaved"]
    print(
        f"  settled {expected_total} paise across {len(batches)} payment links "
        f"({calls_saved} gateway calls avoided)"
    )

    # Settling again must be a no-op — our commitments are no longer pending.
    again = httpx.post(f"{FACILITATOR}/settle-batch", json={}, timeout=30).json()
    if ours(again["batches"]):
        fail(f"re-running settle-batch charged again: {ours(again['batches'])}")
    print("  re-running settle-batch is a no-op — no double charge")

    # The ledger's own summary must agree with what the endpoint reported.
    summary = httpx.get(f"{FACILITATOR}/ledger/summary", timeout=15).json()
    if summary["totalPaise"] < expected_total:
        fail(f"ledger summary shows {summary['totalPaise']} paise, expected >= {expected_total}")
    print(f"  ledger agrees: {summary['requests']} requests, {summary['totalPaise']} paise")

    print("PASS: batch settlement")


if __name__ == "__main__":
    main()
