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
import hashlib
import hmac
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

# Only used by `legacy_hmac`. The normal path never touches it.
DEFAULT_HMAC_SECRET = os.getenv("X402_HMAC_SECRET", "dev-only-shared-secret-change-me")


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
    # Sign with the pre-keypair shared secret instead of this agent's own
    # key. Exists so the downgrade path the facilitator still accepts from
    # unregistered agents can be exercised and seen. Registration is
    # skipped under it — a legacy agent has no key to register.
    legacy_hmac: bool = False
    hmac_secret: str = DEFAULT_HMAC_SECRET

    spent_paise: int = field(default=0, init=False)
    purchases: list[Purchase] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.resource_base = self.resource_base.rstrip("/")
        self.facilitator_url = self.facilitator_url.rstrip("/")
        self._http = httpx.Client(timeout=self.timeout)
        self._registered = False
        if self.legacy_hmac:
            # No keypair to make, and nothing to register.
            self._private_key = self.public_key = None
        else:
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

    def register(self, facilitator: str | None = None) -> dict | None:
        """Announces this agent's public key to the facilitator, once.

        Returns:
            The facilitator's registration record, or None when there was
            nothing to do — already registered this run, or signing with the
            legacy shared secret, which has no key to announce. Returned
            rather than discarded so a caller that narrates itself can say
            whether the key was new or already on file.
        """
        if self._registered or self.legacy_hmac:
            return None

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
        return response.json()

    def _sign(self, body: dict) -> str:
        """Signs the canonical JSON of `body`.

        Ed25519 normally. Under `legacy_hmac` this falls back to the
        shared-secret MAC the facilitator still accepts from agents with no
        registered key — supported here rather than only in the CLI agent
        because the facilitator accepts both, and a client library for it that
        could only speak one would be an incomplete client.
        """
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))

        if self.legacy_hmac:
            return hmac.new(
                self.hmac_secret.encode(), canonical.encode(), hashlib.sha256
            ).hexdigest()

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
        return self.quote_url(f"{self.resource_base}{path}", resource=resource, path=path)

    def quote_url(self, url: str, *, resource: str | None = None, path: str | None = None) -> dict:
        """The same, for a resource named by URL rather than by key.

        Split out because a caller may already know the exact URL and have no
        reason to look it up — `demo-agent/crawler_agent.py --url` takes one
        directly, and resolving it back through the publisher's listing would
        fail for any URL that listing does not mention.
        """
        resource = resource or url.rstrip("/").rsplit("/", 1)[-1]

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
            "path": path if path is not None else url,
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

    def check_budget(self, resource: str, amount_paise: int) -> None:
        """The wall. Raises before anything is spent.

        Called before the offer is even requested, so a refusal leaves no
        consumed offer and no ledger row behind.

        Raises:
            BudgetExceeded: If the price would take total spend over budget.
        """
        if amount_paise > self.remaining_paise():
            raise BudgetExceeded(
                f"{resource} costs {_rupees(amount_paise)} but only "
                f"{_rupees(self.remaining_paise())} of the "
                f"{_rupees(self.budget_paise)} budget is left."
            )

    def facilitator_for(self, quoted: dict) -> str:
        """The facilitator the *publisher* nominated, from its own 402.

        Falling back to the configured URL only when the 402 does not say. An
        x402 client is supposed to learn where to pay from the resource it is
        buying — the publisher chooses who settles its payments, not the
        client — so the configured value is a fallback, not the default.
        """
        extra = quoted["requirements"].get("extra", {})
        return extra.get("facilitatorUrl", self.facilitator_url).rstrip("/")

    def request_offer(self, quoted: dict) -> dict:
        """Asks the facilitator to quote this fetch, and registers if needed.

        Returns:
            The facilitator's `{offer, signature, commitmentTemplate, ...}`.
        """
        requirements = quoted["requirements"]
        facilitator = self.facilitator_for(quoted)
        self.register(facilitator)

        response = self._http.post(
            f"{facilitator}/offer",
            json={
                "agentId": self.agent_id,
                "resourceId": (
                    requirements.get("extra", {}).get("resourceId") or quoted["resource"]
                ),
                "amountPaise": quoted["amountPaise"],
                "payTo": requirements["payTo"],
                "scheme": requirements["scheme"],
                "network": requirements["network"],
                "resourceUrl": quoted["url"],
            },
        )
        if response.status_code != 200:
            raise PaymentRefused(f"facilitator refused to quote: {response.text[:300]}")
        return response.json()

    def sign_acceptance(self, offer: dict) -> dict:
        """Signs acceptance of a quote.

        The facilitator hands back the exact object to sign as
        `commitmentTemplate`, with `acceptedAt` left as a placeholder. Filling
        in a template beats reconstructing the field set from documentation
        and getting one key wrong — which is the most common way a signature
        scheme fails in practice.

        Returns:
            `{"payload", "canonicalJson", "signature", "acceptedAt"}`. The
            canonical form is returned rather than kept private because a
            client that narrates itself needs to show exactly what was signed.
        """
        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        commitment = dict(offer["commitmentTemplate"])
        commitment["acceptedAt"] = accepted_at

        signature = self._sign(commitment)
        return {
            "payload": {
                "offerId": offer["offer"]["offerId"],
                "agentId": self.agent_id,
                "acceptedAt": accepted_at,
                "agentSignature": signature,
            },
            "canonicalJson": json.dumps(commitment, sort_keys=True, separators=(",", ":")),
            "signature": signature,
            "acceptedAt": accepted_at,
        }

    def payment_header(self, quoted: dict, payload: dict) -> str:
        """Base64 of the x402 payment envelope, for the X-PAYMENT header."""
        envelope = {
            "x402Version": quoted["required"]["x402Version"],
            "accepted": quoted["requirements"],
            "payload": payload,
        }
        return base64.b64encode(json.dumps(envelope).encode()).decode()

    def request_paid(self, quoted: dict, header: str) -> httpx.Response:
        """Retries the original request with the payment attached."""
        response = self._http.get(
            quoted["url"], headers={"X-PAYMENT": header, "Accept": "application/json"}
        )
        if response.status_code != 200:
            reason = "unknown"
            retry_header = response.headers.get("payment-required")
            if retry_header:
                reason = json.loads(base64.b64decode(retry_header)).get("error", "unknown")
            raise PaymentRefused(reason)
        return response

    @staticmethod
    def read_receipt(response: httpx.Response) -> dict:
        """Decodes the settlement receipt, under either header name."""
        raw = response.headers.get("payment-response") or response.headers.get(
            "x-payment-response"
        )
        return json.loads(base64.b64decode(raw)) if raw else {}

    def record_purchase(self, resource: str, amount_paise: int, receipt: dict, content: dict):
        """Books a completed purchase against the budget.

        Separate from `request_paid` so spend is only counted once the
        publisher has actually served the content — a request that failed
        after payment was presented has not cost the agent its budget.
        """
        purchase = Purchase(
            resource=resource,
            amount_paise=amount_paise,
            commitment_id=receipt.get("transaction", ""),
            content=content,
        )
        self.spent_paise += amount_paise
        self.purchases.append(purchase)
        return purchase

    def pay_and_fetch(self, resource: str) -> Purchase:
        """Buys a resource, if the budget allows it.

        The whole negotiation, composed from the steps above. A caller that
        wants to narrate each step — `demo-agent/crawler_agent.py` — calls
        them individually instead; this is the same walk with nothing between
        the steps.

        Raises:
            BudgetExceeded: If the price would take total spend over budget.
            PaymentRefused: If the publisher would not serve it.
        """
        quoted = self.quote(resource)
        self.check_budget(resource, quoted["amountPaise"])

        offer = self.request_offer(quoted)
        signed = self.sign_acceptance(offer)
        paid = self.request_paid(quoted, self.payment_header(quoted, signed["payload"]))

        return self.record_purchase(
            resource, quoted["amountPaise"], self.read_receipt(paid), paid.json()
        )

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
