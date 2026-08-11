"""Bharat x402 — demo crawler agent (Phase 0 skeleton).

Simulates an AI crawler that hits a paywalled resource, receives HTTP 402,
pays in INR through the Razorpay facilitator, and retries. Full negotiation
loop lands in Phase 3.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

RESOURCE_URL = os.getenv("RESOURCE_URL", "http://localhost:3402/premium/market-report")


def main() -> None:
    print("[agent] Phase 0 skeleton — probing services")
    for name, url in (
        ("resource-server", "http://localhost:3402/health"),
        ("facilitator", "http://localhost:8402/health"),
    ):
        try:
            resp = httpx.get(url, timeout=5.0)
            print(f"[agent] {name}: {resp.status_code} {resp.text}")
        except httpx.HTTPError as exc:
            print(f"[agent] {name}: unreachable ({exc})")


if __name__ == "__main__":
    main()
