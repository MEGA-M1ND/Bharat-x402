"""Bharat x402 — demo crawler agent.

Simulates an AI crawler that wants a publisher's paywalled market report and is
willing to pay ₹5 for it. It walks the full x402 negotiation:

    1. GET the resource with no payment          -> 402 + an INR offer
    2. ask the facilitator to quote it           -> a signed, expiring offer
    3. sign its acceptance of that quote         -> the payment proof
    4. GET again with the proof attached         -> 200 + the content
    5. read the settlement receipt               -> a deferred commitment

Steps 2 and 3 are the part with no equivalent in stock x402. An agent paying in
USDC already holds a wallet and can sign a transfer authorisation unaided. An
agent paying in rupees has no such instrument, so it asks the facilitator to
quote it and signs its acceptance of the quote instead.

The terminal output is deliberately verbose. Someone evaluating this repo
should be able to read a single run and understand the whole protocol without
running anything themselves — so every step prints what it sent, what came
back, and why that matters.

Before any of that, once per identity, the agent registers the public half of
its Ed25519 keypair with the facilitator. The private half is generated here
and never sent anywhere — so the facilitator can check this agent's
commitments and cannot manufacture one, which is what makes the proof worth
anything in a dispute. See facilitator/payment_verifier.py for why a shared
secret could not do that regardless of how strong the MAC was.

Usage:
    python crawler_agent.py                          # one fetch, narrated
    python crawler_agent.py --count 8                # simulate a day of traffic
    python crawler_agent.py --agent-id agent-gptbot  # a different crawler
    python crawler_agent.py --count 5 --quiet        # one line per fetch
    python crawler_agent.py --legacy-hmac            # the old shared-secret proof
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import sys
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
from dotenv import load_dotenv

load_dotenv()

DEFAULT_RESOURCE = os.getenv("RESOURCE_URL", "http://localhost:3402/premium/market-report")
DEFAULT_FACILITATOR = os.getenv("FACILITATOR_URL", "http://localhost:8402")
DEFAULT_AGENT_ID = os.getenv("AGENT_ID", "agent-perplexity-bot")

# Only used by --legacy-hmac, which exists to demonstrate the pre-keypair
# proof the facilitator still accepts from unregistered agents. The normal
# path never touches this.
HMAC_SECRET = os.getenv("X402_HMAC_SECRET", "dev-only-shared-secret-change-me")

# Where this agent keeps its private key. One file per agent id, so
# `--count 8` spreading traffic over several crawler identities gives each a
# genuinely distinct key rather than one key wearing several names.
#
# Gitignored, and that is not a formality: the whole value of the keypair is
# that this file exists in exactly one place. A private key committed to a
# repository is a shared secret with extra steps.
KEY_DIR = Path(os.getenv("AGENT_KEY_DIR", Path(__file__).resolve().parent / ".keys"))

# Crawler identities used by --count when no --agent-id is given, so a simulated
# day of traffic looks like several different bots rather than one very
# enthusiastic one.
SAMPLE_AGENTS = [
    "agent-perplexity-bot",
    "agent-gptbot",
    "agent-claude-web",
    "agent-gemini-crawler",
    "agent-bytespider",
]


# ---------------------------------------------------------------------------
# Terminal formatting
# ---------------------------------------------------------------------------

# Windows consoles still default to cp1252, which cannot encode ₹ or box-drawing
# characters. Ask for UTF-8; if the stream refuses, fall back to ASCII rather
# than crashing halfway through a demo.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    UNICODE_OK = True
except (AttributeError, OSError):  # pragma: no cover - depends on the terminal
    UNICODE_OK = False

if UNICODE_OK:
    try:
        "₹ │ ═ →".encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):  # pragma: no cover
        UNICODE_OK = False

RUPEE = "₹" if UNICODE_OK else "Rs."
ARROW = "->"
HEAVY = "=" if not UNICODE_OK else "═"
LIGHT = "-" if not UNICODE_OK else "─"

# ANSI colour, off when piped to a file or when NO_COLOR is set.
COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def c(text: str, code: str) -> str:
    """Wraps text in an ANSI colour, or returns it unchanged."""
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(text: str) -> str:
    return c(text, "1")


def dim(text: str) -> str:
    return c(text, "2")


def green(text: str) -> str:
    return c(text, "32")


def yellow(text: str) -> str:
    return c(text, "33")


def cyan(text: str) -> str:
    return c(text, "36")


def red(text: str) -> str:
    return c(text, "31")


def rule(char: str = LIGHT, width: int = 74) -> str:
    return dim(char * width)


def paise_to_rupees(paise: int | str) -> str:
    """Formats integer paise for humans: 500 -> '₹5.00'."""
    value = int(paise)
    return f"{RUPEE}{value // 100}.{value % 100:02d}"


# ---------------------------------------------------------------------------
# The agent's signing key
# ---------------------------------------------------------------------------


def load_or_create_key(agent_id: str) -> tuple[str, str]:
    """Loads this agent's Ed25519 keypair, generating one on first run.

    Persisted rather than regenerated per process, because the identity has
    to outlive the run: the facilitator binds `agent_id` to the first key it
    sees (trust-on-first-use), so an agent that generated a fresh key every
    time would be refused the second time it registered.

    Args:
        agent_id: Identity the key belongs to.

    Returns:
        `(private_key_b64, public_key_b64)`.
    """
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key_path = KEY_DIR / f"{agent_id}.key"

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
        # Owner-only where the filesystem honours it. A no-op on Windows,
        # which is why it is not the only thing keeping the key private —
        # the directory is gitignored regardless.
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


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class PaymentRefused(Exception):
    """The publisher would not serve the resource even after payment."""


class X402Agent:
    """An AI crawler that can pay for what it fetches.

    Args:
        agent_id: Identity presented to the facilitator; the ledger groups by it.
        resource_url: The paywalled resource to fetch.
        facilitator_url: Where to get quoted. Discovered from the 402 when the
            publisher advertises it, so this is only a fallback.
        secret: Shared HMAC secret, used only under `legacy_hmac`.
        legacy_hmac: Sign with the old shared secret instead of this agent's
            keypair. Kept so the downgrade path the facilitator still accepts
            from unregistered agents can actually be exercised and seen.
        verbose: Narrate every step.
    """

    def __init__(
        self,
        *,
        agent_id: str = DEFAULT_AGENT_ID,
        resource_url: str = DEFAULT_RESOURCE,
        facilitator_url: str = DEFAULT_FACILITATOR,
        secret: str = HMAC_SECRET,
        legacy_hmac: bool = False,
        verbose: bool = True,
    ) -> None:
        self.agent_id = agent_id
        self.resource_url = resource_url
        self.facilitator_url = facilitator_url.rstrip("/")
        self.secret = secret
        self.legacy_hmac = legacy_hmac
        self.verbose = verbose
        self.client = httpx.Client(timeout=30.0)
        self.registered = False

        # The private half stays in this process and this process only. The
        # facilitator never sees it, which is the entire difference between
        # this and the shared-secret scheme it replaced.
        if legacy_hmac:
            self.private_key = self.public_key = None
        else:
            self.private_key, self.public_key = load_or_create_key(agent_id)

    # -- helpers -----------------------------------------------------------

    def say(self, text: str = "") -> None:
        """Prints only when narrating."""
        if self.verbose:
            print(text)

    def step(self, number: int, total: int, title: str) -> None:
        """Prints a numbered step header."""
        self.say(f"\n{bold(f'[{number}/{total}]')} {bold(title)}")

    def sign(self, body: dict) -> str:
        """Signs the canonical JSON of `body` with this agent's private key.

        Canonical means sorted keys and no incidental whitespace. Both sides
        must build the identical string or the signature will not match — this
        is the single most common way a signature scheme goes wrong, which is
        why the facilitator hands back a `commitmentTemplate` rather than
        making clients guess.

        Ed25519 by default. Under `--legacy-hmac` this falls back to the old
        shared-secret MAC, which the facilitator still accepts from agents
        that have not registered a key.
        """
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))

        if self.legacy_hmac:
            return hmac.new(self.secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.private_key))
        return base64.b64encode(private.sign(canonical.encode())).decode("ascii")

    def register(self) -> None:
        """Announces this agent's public key to the facilitator, once.

        Idempotent server-side, so re-running the agent is not an error. Not
        counted as one of the five negotiation steps because it is not part
        of a fetch — an agent registers once in its life and then pays
        forever, the same way a wallet address is not re-derived per
        transaction.
        """
        if self.legacy_hmac or self.registered:
            return

        response = self.client.post(
            f"{self.facilitator_url}/agents/register",
            json={
                "agentId": self.agent_id,
                "publicKey": self.public_key,
                "algorithm": "ed25519",
            },
        )

        if response.status_code == 409:
            # This id is bound to a different key — almost always a stale or
            # deleted local key file against a facilitator that still
            # remembers the old one. Say so plainly rather than failing later
            # with a confusing invalid_signature at settlement time.
            raise PaymentRefused(
                f"{self.agent_id} is registered with a different public key. "
                f"Delete {KEY_DIR / f'{self.agent_id}.key'} to start over with a fresh "
                "identity, or use --agent-id to pick a different one."
            )
        if response.status_code != 200:
            raise PaymentRefused(f"key registration failed: {response.text[:300]}")

        self.registered = True
        body = response.json()

        self.say()
        self.say(dim(f"  key   {self.agent_id}"))
        self.say(
            dim(f"        public  {self.public_key[:24]}…  "
                f"({'registered now' if body.get('created') else 'already on file'})")
        )
        self.say(dim("        private key never leaves this process — the facilitator"))
        self.say(dim("        holds only the public half and cannot forge a commitment."))

    # -- the negotiation ---------------------------------------------------

    def request_unpaid(self) -> dict:
        """Step 1: ask for the resource without paying, and read the price.

        Returns:
            The decoded x402 `PaymentRequired` object.

        Raises:
            PaymentRefused: If the server did not answer with a 402.
        """
        self.step(1, 5, "Request the resource with no payment attached")
        self.say(dim(f"      GET {self.resource_url}"))

        response = self.client.get(self.resource_url)

        if response.status_code == 200:
            self.say(yellow("      This resource is not paywalled — nothing to pay for."))
            raise PaymentRefused("resource was served without payment")

        if response.status_code != 402:
            raise PaymentRefused(
                f"expected HTTP 402, got {response.status_code}: {response.text[:200]}"
            )

        self.say(f"      {ARROW} {yellow('HTTP 402 Payment Required')}")

        # The machine-readable offer travels base64-encoded in a header. The
        # response body is whatever the publisher wants it to be.
        header = response.headers.get("payment-required")
        if not header:
            raise PaymentRefused("402 carried no PAYMENT-REQUIRED header — cannot pay blind")

        required = json.loads(base64.b64decode(header))
        offer = required["accepts"][0]

        self.say()
        self.say(dim("      accepts[0], decoded from the PAYMENT-REQUIRED header:"))

        # Padded before colouring: ANSI escape codes count toward len(), so
        # aligning coloured strings directly produces ragged columns.
        def field(name: str, value: str, note: str = "") -> None:
            self.say(f"        {name:<9} {cyan(value.ljust(22))}{dim(note)}")

        field("scheme", offer["scheme"], "not 'exact' — this is not an EVM transfer")
        field("network", offer["network"], "not a blockchain, just a settlement rail")
        field(
            "amount",
            f"{offer['amount']} paise",
            f"= {paise_to_rupees(offer['amount'])}",
        )
        field("payTo", offer["payTo"], "the publisher's merchant account")
        field(
            "settles",
            f"{offer['extra'].get('settlementMode', '?')} / "
            f"{offer['extra'].get('settlementRail', '?')}",
            "batched, not per-request",
        )

        return required

    def get_quote(self, offer_requirements: dict) -> dict:
        """Step 2: ask the facilitator to quote this fetch.

        Args:
            offer_requirements: The `accepts[0]` entry from the 402.

        Returns:
            The facilitator's quote response.
        """
        facilitator = offer_requirements["extra"].get("facilitatorUrl", self.facilitator_url)
        endpoint = f"{facilitator.rstrip('/')}/offer"

        self.step(2, 5, "Ask the facilitator to quote this fetch")
        self.say(dim(f"      POST {endpoint}"))
        self.say(dim("      An agent paying in USDC would skip this — it holds a wallet and"))
        self.say(dim("      signs a transfer itself. Paying in rupees, it has to be quoted."))

        response = self.client.post(
            endpoint,
            json={
                "agentId": self.agent_id,
                "resourceId": self._resource_id(offer_requirements),
                "amountPaise": int(offer_requirements["amount"]),
                "payTo": offer_requirements["payTo"],
                "scheme": offer_requirements["scheme"],
                "network": offer_requirements["network"],
                "resourceUrl": self.resource_url,
            },
        )

        if response.status_code != 200:
            raise PaymentRefused(f"facilitator refused to quote: {response.text[:300]}")

        quote = response.json()
        offer = quote["offer"]

        self.say(f"      {ARROW} {green(offer['offerId'])}  ({quote['humanAmount']})")
        self.say(dim(f"        issued {offer['issuedAt']}, expires {offer['expiresAt']}"))
        self.say(dim("        single-use, and bound to this agent id"))

        return quote

    def sign_acceptance(self, quote: dict) -> dict:
        """Step 3: sign acceptance of the quote.

        Returns:
            The x402 payment payload the publisher will forward for verification.
        """
        self.step(3, 5, "Sign acceptance of the quote")

        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

        # The facilitator handed back the exact object to sign, with acceptedAt
        # left as a placeholder. Filling in a template beats reconstructing the
        # field set from documentation and getting one key wrong.
        commitment = dict(quote["commitmentTemplate"])
        commitment["acceptedAt"] = accepted_at

        signature = self.sign(commitment)

        algorithm = "HMAC-SHA256" if self.legacy_hmac else "Ed25519"
        self.say(dim(f"      {algorithm} over the canonical JSON of:"))
        preview = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
        self.say(dim(f"        {preview[:88]}…" if len(preview) > 88 else f"        {preview}"))
        length = dim(f"({len(signature)} chars)")
        self.say(f"      {ARROW} {green(signature[:32])}{dim('…')}  {length}")

        if self.legacy_hmac:
            self.say(yellow("      Legacy path: signed with the secret the facilitator also"))
            self.say(yellow("      holds, so it could have produced this itself. Accepted"))
            self.say(yellow("      only from agents with no registered key — an agent that"))
            self.say(yellow("      has one is refused, because a downgrade would undo it."))
        else:
            self.say(dim("      Signed with this agent's own private key. The facilitator"))
            self.say(dim("      can verify this and cannot produce it — so the commitment"))
            self.say(dim("      is evidence in a dispute, not just a checksum."))

        return {
            "offerId": quote["offer"]["offerId"],
            "agentId": self.agent_id,
            "acceptedAt": accepted_at,
            "agentSignature": signature,
        }

    def request_paid(self, required: dict, payload: dict) -> httpx.Response:
        """Step 4: retry the original request with the payment proof attached.

        Returns:
            The publisher's response.
        """
        self.step(4, 5, "Retry the request with the payment attached")

        envelope = {
            "x402Version": required["x402Version"],
            "accepted": required["accepts"][0],
            "payload": payload,
        }
        header = base64.b64encode(json.dumps(envelope).encode()).decode()

        self.say(dim(f"      GET {self.resource_url}"))
        self.say(dim(f"      X-PAYMENT: {header[:56]}…  ({len(header)} bytes of base64)"))

        response = self.client.get(self.resource_url, headers={"X-PAYMENT": header})

        if response.status_code == 200:
            self.say(f"      {ARROW} {green('HTTP 200 OK')}")
            return response

        # Payment was presented and refused. The reason is in the header the
        # publisher sent back, so report it rather than a bare status code.
        reason = "unknown"
        retry_header = response.headers.get("payment-required")
        if retry_header:
            reason = json.loads(base64.b64decode(retry_header)).get("error", "unknown")
        self.say(f"      {ARROW} {red(f'HTTP {response.status_code}')} — {reason}")
        raise PaymentRefused(reason)

    def read_receipt(self, response: httpx.Response) -> dict | None:
        """Step 5: read the settlement receipt.

        Returns:
            The decoded settlement response, or None if the publisher sent none.
        """
        self.step(5, 5, "Read the settlement receipt")

        raw = response.headers.get("payment-response") or response.headers.get(
            "x-payment-response"
        )
        if not raw:
            self.say(dim("      No receipt header — the publisher did not report settlement."))
            return None

        receipt = json.loads(base64.b64decode(raw))
        extra = receipt.get("extra", {})

        self.say(dim("      decoded from the PAYMENT-RESPONSE header:"))
        self.say(f"        success       {green(str(receipt.get('success')))}")
        self.say(f"        transaction   {cyan(receipt.get('transaction', '—'))}")
        self.say(f"        mode          {extra.get('settlementMode', 'unknown')}")
        if extra.get("note"):
            self.say(dim(f"        {extra['note']}"))

        return receipt

    # -- orchestration -----------------------------------------------------

    def fetch(self) -> dict:
        """Runs the whole negotiation once.

        Returns:
            `{"content": ..., "receipt": ..., "amountPaise": ...}`.
        """
        if self.verbose:
            print()
            print(rule(HEAVY))
            print(f"  {bold('Bharat x402')} {dim('|')} AI crawler agent")
            print(f"  {self.agent_id} {dim(ARROW)} {self.resource_url}")
            print(rule(HEAVY))

        # One-time identity setup, not part of the negotiation.
        self.register()

        required = self.request_unpaid()
        requirements = required["accepts"][0]

        quote = self.get_quote(requirements)
        payload = self.sign_acceptance(quote)
        response = self.request_paid(required, payload)
        receipt = self.read_receipt(response)

        content = response.json()
        amount = int(requirements["amount"])

        if self.verbose:
            self._print_content(content)
            self._print_footer(amount, receipt)

        return {"content": content, "receipt": receipt, "amountPaise": amount}

    def _resource_id(self, requirements: dict) -> str:
        """Best-effort resource id.

        The publisher may name it in `extra`; otherwise fall back to the last
        path segment, which is what the demo resource happens to use.
        """
        advertised = requirements["extra"].get("resourceId")
        return advertised or self.resource_url.rstrip("/").split("/")[-1]

    def _print_content(self, content: dict) -> None:
        """Prints the unlocked resource."""
        print()
        print(rule())
        print(f"  {bold(green('CONTENT UNLOCKED'))}")
        print(rule())
        print(f"  {bold(content.get('title', 'untitled'))}")
        print(dim(f"  {content.get('publisher', '')} · {content.get('generatedAt', '')}"))
        print()
        summary = content.get("summary", "")
        for line in _wrap(summary, 70):
            print(f"  {line}")
        if content.get("findings"):
            print()
            for finding in content["findings"]:
                wrapped = _wrap(finding, 68)
                print(f"  • {wrapped[0]}")
                for extra_line in wrapped[1:]:
                    print(f"    {extra_line}")

    def _print_footer(self, amount_paise: int, receipt: dict | None) -> None:
        """Prints the closing summary — the part that makes the point."""
        print()
        print(rule())
        deferred = (receipt or {}).get("extra", {}).get("settlementMode") == "deferred"
        print(f"  Paid {bold(paise_to_rupees(amount_paise))} in 4 HTTP requests.")
        if deferred:
            print(dim("  No rupees have moved yet. This fetch is one line in a batch"))
            print(dim("  that becomes a single Razorpay Payment Link at end of day —"))
            print(dim("  which is the only way a charge this small is collectable at all."))
        print(rule())
        print()

    def close(self) -> None:
        self.client.close()


def _wrap(text: str, width: int) -> list[str]:
    """Wraps text to a width without importing textwrap for one call."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulated AI crawler that pays for content in INR over x402.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[-1],
    )
    parser.add_argument("--url", default=DEFAULT_RESOURCE, help="Resource to fetch.")
    parser.add_argument(
        "--facilitator",
        default=DEFAULT_FACILITATOR,
        help=(
            "Fallback facilitator URL. Normally unused: the agent takes the facilitator "
            "from the 402 itself, which is how x402 is meant to work — the publisher "
            "chooses who settles its payments, not the client."
        ),
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Agent identity. Omit with --count to spread traffic across sample crawlers.",
    )
    parser.add_argument(
        "--count", type=int, default=1, help="How many fetches to perform (default 1)."
    )
    parser.add_argument(
        "--quiet", action="store_true", help="One line per fetch instead of full narration."
    )
    parser.add_argument(
        "--legacy-hmac",
        action="store_true",
        help=(
            "Sign with the old shared secret instead of this agent's keypair. Exercises "
            "the downgrade path the facilitator still accepts from unregistered agents; "
            "fails if the facilitator has ALLOW_HMAC_FALLBACK turned off."
        ),
    )
    args = parser.parse_args()

    verbose = not args.quiet and args.count == 1
    total_paise = 0
    failures = 0

    if args.count > 1 and not args.quiet:
        print()
        print(rule(HEAVY))
        print(f"  {bold('Bharat x402')} {dim('|')} simulating {args.count} agent fetches")
        print(rule(HEAVY))

    for index in range(args.count):
        agent_id = args.agent_id or (
            DEFAULT_AGENT_ID if args.count == 1 else random.choice(SAMPLE_AGENTS)
        )
        agent = X402Agent(
            agent_id=agent_id,
            resource_url=args.url,
            facilitator_url=args.facilitator,
            legacy_hmac=args.legacy_hmac,
            verbose=verbose,
        )
        try:
            result = agent.fetch()
            total_paise += result["amountPaise"]
            if not verbose:
                txn = (result["receipt"] or {}).get("transaction", "—")
                print(
                    f"  {index + 1:>3}. {agent_id:<24} "
                    f"{paise_to_rupees(result['amountPaise']):>8}  {green('paid')}  {dim(txn)}"
                )
        except PaymentRefused as exc:
            failures += 1
            print(f"  {index + 1:>3}. {agent_id:<24} {red('refused')}  {exc}")
        except httpx.HTTPError as exc:
            failures += 1
            print(f"  {index + 1:>3}. {agent_id:<24} {red('unreachable')}  {exc}")
            print(dim("       Are both services running? See docs/demo-script.md."))
        finally:
            agent.close()

    if args.count > 1:
        print()
        print(rule())
        print(
            f"  {args.count - failures}/{args.count} fetches paid, "
            f"{bold(paise_to_rupees(total_paise))} committed."
        )
        print(dim("  Run reporting/daily_summary.py to see what the publisher gets."))
        print(rule())
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
