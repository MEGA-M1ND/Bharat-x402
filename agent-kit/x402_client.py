"""A reusable x402 client — the negotiation, with no opinion about who drives it.

`demo-agent/crawler_agent.py` narrates the same protocol to a terminal. This is
the same walk with the narration removed and a budget added, so it can sit
underneath something that is *deciding* rather than demonstrating: the MCP
server in `mcp_server.py`, and the Claude-powered agent in `researcher.py`.

---------------------------------------------------------------------------
THE BUDGET IS ENFORCED HERE, NOT IN A PROMPT
---------------------------------------------------------------------------
This is the design decision worth arguing about, because it is the one that
matters once an LLM is the thing spending money.

The obvious way to give an agent a spending limit is to write it in the system
prompt — "you have ₹50, do not exceed it". That is not a control. It is a
request. Prompts can be talked around, and a model that miscounts, or reads a
crafted instruction inside a *fetched document*, will cheerfully spend past a
limit it was only asked to respect.

So `X402Client` refuses over-budget purchases itself, before any HTTP happens.
The model can ask for anything; what it can actually spend is decided by code
it cannot reach. The prompt still states the budget — the model needs it to
plan well — but the prompt is advice and this class is the wall.

Two consequences fall out of that, both deliberate:

  * A refusal is returned as data, not raised. The agent should be able to see
    "that would exceed your budget, you have ₹3.50 left" and re-plan, which is
    the behaviour you want. An exception would just end the run.
  * `spent_paise` counts what was *committed*, not what the model believes it
    spent. The ledger is the arbiter.

This mirrors the shape Razorpay's own agentic-payments product describes —
consent with pre-set spending limits, enforced by the rail rather than by the
agent's good intentions.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

DEFAULT_RESOURCE_BASE = os.getenv("RESOURCE_BASE_URL", "http://localhost:3402")
DEFAULT_FACILITATOR = os.getenv("FACILITATOR_URL", "http://localhost:8402")

# Same store the CLI agent uses, and gitignored for the same reason: the
# private half of the keypair is the one thing in this project that must exist
# in exactly one place.
KEY_DIR = Path(os.getenv("AGENT_KEY_DIR", Path(__file__).resolve().parent / ".keys"))


class PaymentRefused(Exception):
    """The publisher would not serve the resource, even after payment."""


class BudgetExceeded(Exception):
    """A purchase was refused because it would breach the agent's budget.

    Raised only by `pay_and_fetch`; the tool layer in `tools.py` catches it and
    returns it to the model as a readable result rather than an error, so the
    agent can re-plan instead of crashing.
    """


@dataclass
class Purchase:
    """One completed paid fetch."""

    resource: str
    amount_paise: int
    commitment_id: str
    content: dict


@dataclass
class X402Client:
    """Walks the x402 negotiation on behalf of something that decides.

    Args:
        agent_id: Identity presented to the facilitator; the ledger groups by it.
        budget_paise: Hard ceiling on total spend for this client's lifetime.
            Enforced here — see the module docstring.
        resource_base: Publisher's base URL.
        facilitator_url: Fallback facilitator. Normally unused: the facilitator
            is read from the 402 itself, which is how x402 is meant to work —
            the publisher chooses who settles its payments, not the client.
        timeout: HTTP timeout in seconds.
    """

    agent_id: str = "agent-claude-researcher"
    budget_paise: int = 5000
    resource_base: str = DEFAULT_RESOURCE_BASE
    facilitator_url: str = DEFAULT_FACILITATOR
    timeout: float = 30.0

    spent_paise: int = field(default=0, init=False)
    purchases: list[Purchase] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.resource_base = self.resource_base.rstrip("/")
        self.facilitator_url = self.facilitator_url.rstrip("/")
        self._http = httpx.Client(timeout=self.timeout)
        self._registered = False
        self._private_key, self.public_key = self._load_or_create_key()

    # -- identity ----------------------------------------------------------

    def _load_or_create_key(self) -> tuple[str, str]:
        """Loads this agent's Ed25519 keypair, generating one on first run.

        Persisted rather than regenerated per process because the facilitator
        binds an agent id to the first key it sees; a fresh key each run would
        be refused the second time.
        """
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        key_path = KEY_DIR / f"{self.agent_id}.key"

        if key_path.exists():
            private = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(key_path.read_text().strip())
            )
        else:
            private = Ed25519PrivateKey.generate()
            raw = private.private_bytes(
                encoding=Encoding.Raw,
                format=PrivateFormat.Raw,
                encryption_algorithm=NoEncryption(),
            )
            key_path.write_text(base64.b64encode(raw).decode("ascii"))
            try:
                key_path.chmod(0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass

        private_b64 = base64.b64encode(
            private.private_bytes(
                encoding=Encoding.Raw,
                format=PrivateFormat.Raw,
                encryption_algorithm=NoEncryption(),
            )
        ).decode("ascii")
        public_b64 = base64.b64encode(
            private.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        ).decode("ascii")
        return private_b64, public_b64

    def register(self, facilitator: str | None = None) -> None:
        """Announces this agent's public key to the facilitator, once."""
        if self._registered:
            return

        base = (facilitator or self.facilitator_url).rstrip("/")
        response = self._http.post(
            f"{base}/agents/register",
            json={
                "agentId": self.agent_id,
                "publicKey": self.public_key,
                "algorithm": "ed25519",
            },
        )
        if response.status_code == 409:
            raise PaymentRefused(
                f"{self.agent_id} is registered with a different public key. Delete "
                f"{KEY_DIR / f'{self.agent_id}.key'} or choose a different agent id."
            )
        if response.status_code != 200:
            raise PaymentRefused(f"key registration failed: {response.text[:300]}")

        self._registered = True

    def _sign(self, body: dict) -> str:
        """Ed25519 over the canonical JSON of `body`."""
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self._private_key))
        return base64.b64encode(private.sign(canonical.encode())).decode("ascii")

    # -- the negotiation ---------------------------------------------------

    def resources(self) -> list[dict]:
        """What the publisher sells, and for how much."""
        response = self._http.get(f"{self.resource_base}/api/resources")
        response.raise_for_status()
        return response.json()["resources"]

    def _path_for(self, resource: str) -> str:
        """Maps a resource key to its path, via the publisher's own listing."""
        for entry in self.resources():
            if entry["key"] == resource:
                return entry["path"]
        raise ValueError(f"unknown resource {resource!r}")

    def quote(self, resource: str) -> dict:
        """Asks unpaid and reads back the price, without buying.

        The 402 *is* the preview: it carries the price, the scheme, and the
        publisher's own summary of what is behind the paywall. An agent
        deciding whether something is worth ₹5 gets everything it needs from
        being refused once, which costs nothing.

        Returns:
            `{resource, path, amountPaise, humanAmount, preview, requirements}`.
        """
        path = self._path_for(resource)
        url = f"{self.resource_base}{path}"

        response = self._http.get(url, headers={"Accept": "application/json"})
        if response.status_code == 200:
            raise PaymentRefused(f"{resource} is not paywalled — nothing to quote.")
        if response.status_code != 402:
            raise PaymentRefused(
                f"expected HTTP 402, got {response.status_code}: {response.text[:200]}"
            )

        header = response.headers.get("payment-required")
        if not header:
            raise PaymentRefused("402 carried no PAYMENT-REQUIRED header — cannot pay blind")

        required = json.loads(base64.b64decode(header))
        requirements = required["accepts"][0]
        body = response.json() if response.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}

        return {
            "resource": resource,
            "path": path,
            "url": url,
            "amountPaise": int(requirements["amount"]),
            "humanAmount": requirements.get("extra", {}).get("humanAmount"),
            "preview": body.get("preview", {}),
            "required": required,
            "requirements": requirements,
        }

    def remaining_paise(self) -> int:
        """Budget left, in paise."""
        return max(self.budget_paise - self.spent_paise, 0)

    def pay_and_fetch(self, resource: str) -> Purchase:
        """Buys a resource, if the budget allows it.

        Raises:
            BudgetExceeded: If the price would take total spend over budget.
                Checked before any payment call, so a refused purchase costs
                nothing and books nothing.
            PaymentRefused: If the publisher would not serve it.
        """
        quoted = self.quote(resource)
        amount = quoted["amountPaise"]

        # The wall. Deliberately before the offer is even requested, so a
        # refusal leaves no consumed offer and no ledger row behind.
        if amount > self.remaining_paise():
            raise BudgetExceeded(
                f"{resource} costs {_rupees(amount)} but only "
                f"{_rupees(self.remaining_paise())} of the "
                f"{_rupees(self.budget_paise)} budget is left."
            )

        requirements = quoted["requirements"]
        facilitator = requirements.get("extra", {}).get("facilitatorUrl", self.facilitator_url)
        self.register(facilitator)

        offer_response = self._http.post(
            f"{facilitator.rstrip('/')}/offer",
            json={
                "agentId": self.agent_id,
                "resourceId": requirements.get("extra", {}).get("resourceId") or resource,
                "amountPaise": amount,
                "payTo": requirements["payTo"],
                "scheme": requirements["scheme"],
                "network": requirements["network"],
                "resourceUrl": quoted["url"],
            },
        )
        if offer_response.status_code != 200:
            raise PaymentRefused(f"facilitator refused to quote: {offer_response.text[:300]}")
        offer = offer_response.json()

        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        commitment = dict(offer["commitmentTemplate"])
        commitment["acceptedAt"] = accepted_at

        envelope = {
            "x402Version": quoted["required"]["x402Version"],
            "accepted": requirements,
            "payload": {
                "offerId": offer["offer"]["offerId"],
                "agentId": self.agent_id,
                "acceptedAt": accepted_at,
                "agentSignature": self._sign(commitment),
            },
        }
        header = base64.b64encode(json.dumps(envelope).encode()).decode()

        paid = self._http.get(
            quoted["url"], headers={"X-PAYMENT": header, "Accept": "application/json"}
        )
        if paid.status_code != 200:
            raise PaymentRefused(
                f"payment presented and refused: HTTP {paid.status_code} {paid.text[:200]}"
            )

        receipt_header = paid.headers.get("payment-response") or paid.headers.get(
            "x-payment-response"
        )
        receipt = json.loads(base64.b64decode(receipt_header)) if receipt_header else {}

        purchase = Purchase(
            resource=resource,
            amount_paise=amount,
            commitment_id=receipt.get("transaction", ""),
            content=paid.json(),
        )
        # Only counted once the publisher has actually served the content.
        self.spent_paise += amount
        self.purchases.append(purchase)
        return purchase

    # -- reporting ---------------------------------------------------------

    def spend_summary(self) -> dict:
        """What this agent has spent, from its own tally and from the ledger."""
        base = self.facilitator_url
        ledger: dict = {}
        try:
            response = self._http.get(
                f"{base}/ledger/summary", params={"agentId": self.agent_id}
            )
            if response.status_code == 200:
                ledger = response.json()
        except httpx.HTTPError:
            pass

        return {
            "agentId": self.agent_id,
            "budgetPaise": self.budget_paise,
            "spentPaise": self.spent_paise,
            "remainingPaise": self.remaining_paise(),
            "purchases": [
                {
                    "resource": p.resource,
                    "amountPaise": p.amount_paise,
                    "commitmentId": p.commitment_id,
                }
                for p in self.purchases
            ],
            # The ledger is the arbiter; the tally above is this process's view.
            "ledgerCommittedPaise": ledger.get("committedPaise"),
            "ledgerCollectedPaise": ledger.get("collectedPaise"),
        }

    def economics(self) -> dict:
        """The batching comparison for this agent's traffic."""
        response = self._http.get(
            f"{self.facilitator_url}/economics", params={"agentId": self.agent_id}
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._http.close()


def _rupees(paise: int) -> str:
    """Formats integer paise for humans: 500 -> '₹5.00'."""
    return f"₹{paise // 100}.{paise % 100:02d}"
