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

**This file is the narration, not the protocol.** The negotiation lives in
`agent-kit/x402_client.py` — the same walk the MCP server and the Claude agent
run — and each step below delegates to it. It used to hold its own copy, which
is precisely how the two would have drifted: a fix applied to one and not the
other. This project has already been bitten by that once, with a hand-rolled
SQL splitter that real Postgres caught in CI.

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
import os
import random
import sys
from pathlib import Path

import httpx

# The negotiation itself lives in agent-kit/x402_client.py — the same walk
# the MCP server and the Claude agent use. This file is the narration on
# top of it. Keeping a second copy here is exactly how the two would drift.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-kit"))

from dotenv import load_dotenv
from x402_client import PaymentRefused as ClientPaymentRefused  # noqa: E402
from x402_client import X402Client  # noqa: E402

load_dotenv()

DEFAULT_RESOURCE = os.getenv("RESOURCE_URL", "http://localhost:3402/premium/market-report")
DEFAULT_FACILITATOR = os.getenv("FACILITATOR_URL", "http://localhost:8402")
DEFAULT_AGENT_ID = os.getenv("AGENT_ID", "agent-perplexity-bot")

# Only used by --legacy-hmac, which exists to demonstrate the pre-keypair
# proof the facilitator still accepts from unregistered agents. The normal
# path never touches this.
HMAC_SECRET = os.getenv("X402_HMAC_SECRET", "dev-only-shared-secret-change-me")

# Key storage lives in agent-kit/x402_client.py, which owns the keypair now.
# AGENT_KEY_DIR still selects it — one file per agent id, so `--count 8`
# spreading traffic over several crawler identities gives each a genuinely
# distinct key rather than one key wearing several names.

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
        self.legacy_hmac = legacy_hmac
        self.verbose = verbose

        # The negotiation, the keypair, and the signing all live in the shared
        # client. This class narrates it.
        #
        # `budget_paise` is set absurdly high rather than made optional: this
        # is a demo agent that pays what it is told, and the budget wall is a
        # headline feature of the client that should not grow an "off" switch
        # just to accommodate a caller that does not want it.
        self._x402 = X402Client(
            agent_id=agent_id,
            budget_paise=10**12,
            facilitator_url=self.facilitator_url,
            legacy_hmac=legacy_hmac,
            hmac_secret=secret,
        )

    # -- helpers -----------------------------------------------------------

    def say(self, text: str = "") -> None:
        """Prints only when narrating."""
        if self.verbose:
            print(text)

    def step(self, number: int, total: int, title: str) -> None:
        """Prints a numbered step header."""
        self.say(f"\n{bold(f'[{number}/{total}]')} {bold(title)}")

    def register(self) -> None:
        """Announces this agent's public key to the facilitator, once.

        Idempotent server-side, so re-running the agent is not an error. Not
        counted as one of the five negotiation steps because it is not part
        of a fetch — an agent registers once in its life and then pays
        forever, the same way a wallet address is not re-derived per
        transaction.
        """
        try:
            body = self._x402.register(self.facilitator_url)
        except ClientPaymentRefused as exc:
            raise PaymentRefused(str(exc)) from exc

        if body is None:
            return

        self.say()
        self.say(dim(f"  key   {self.agent_id}"))
        created = "registered now" if body.get("created") else "already on file"
        self.say(dim(f"        public  {self._x402.public_key[:24]}…  ({created})"))
        self.say(dim("        private key never leaves this process — the facilitator"))
        self.say(dim("        holds only the public half and cannot forge a commitment."))

    # -- the negotiation ---------------------------------------------------

    def request_unpaid(self) -> dict:
        """Step 1: ask for the resource without paying, and read the price.

        Returns:
            The client's quote dict, carrying the decoded 402 and the
            requirements the rest of the walk needs.

        Raises:
            PaymentRefused: If the server did not answer with a 402.
        """
        self.step(1, 5, "Request the resource with no payment attached")
        self.say(dim(f"      GET {self.resource_url}"))

        try:
            quoted = self._x402.quote_url(self.resource_url)
        except ClientPaymentRefused as exc:
            if "not paywalled" in str(exc):
                self.say(yellow("      This resource is not paywalled — nothing to pay for."))
                raise PaymentRefused("resource was served without payment") from exc
            raise PaymentRefused(str(exc)) from exc

        self.say(f"      {ARROW} {yellow('HTTP 402 Payment Required')}")

        offer = quoted["requirements"]
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

        return quoted

    def get_quote(self, quoted: dict) -> dict:
        """Step 2: ask the facilitator to quote this fetch.

        Returns:
            The facilitator's quote response.
        """
        endpoint = f"{self._x402.facilitator_for(quoted)}/offer"

        self.step(2, 5, "Ask the facilitator to quote this fetch")
        self.say(dim(f"      POST {endpoint}"))
        self.say(dim("      An agent paying in USDC would skip this — it holds a wallet and"))
        self.say(dim("      signs a transfer itself. Paying in rupees, it has to be quoted."))

        try:
            quote = self._x402.request_offer(quoted)
        except ClientPaymentRefused as exc:
            raise PaymentRefused(str(exc)) from exc

        offer = quote["offer"]
        self.say(f"      {ARROW} {green(offer['offerId'])}  ({quote['humanAmount']})")
        self.say(dim(f"        issued {offer['issuedAt']}, expires {offer['expiresAt']}"))
        self.say(dim("        single-use, and bound to this agent id"))

        return quote

    def sign_acceptance(self, quote: dict) -> dict:
        """Step 3: sign acceptance of the quote.

        Returns:
            The signed payload the publisher will forward for verification.
        """
        self.step(3, 5, "Sign acceptance of the quote")

        signed = self._x402.sign_acceptance(quote)
        signature = signed["signature"]
        preview = signed["canonicalJson"]

        algorithm = "HMAC-SHA256" if self.legacy_hmac else "Ed25519"
        self.say(dim(f"      {algorithm} over the canonical JSON of:"))
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

        return signed

    def request_paid(self, quoted: dict, signed: dict) -> httpx.Response:
        """Step 4: retry the original request with the payment proof attached.

        Returns:
            The publisher's response.
        """
        self.step(4, 5, "Retry the request with the payment attached")

        header = self._x402.payment_header(quoted, signed["payload"])

        self.say(dim(f"      GET {self.resource_url}"))
        self.say(dim(f"      X-PAYMENT: {header[:56]}…  ({len(header)} bytes of base64)"))

        try:
            response = self._x402.request_paid(quoted, header)
        except ClientPaymentRefused as exc:
            # Payment was presented and refused. The client surfaces the reason
            # the publisher put in its retry header, so show that rather than a
            # bare status code.
            self.say(f"      {ARROW} {red('HTTP 402')} — {exc}")
            raise PaymentRefused(str(exc)) from exc

        self.say(f"      {ARROW} {green('HTTP 200 OK')}")
        return response

    def read_receipt(self, response: httpx.Response) -> dict | None:
        """Step 5: read the settlement receipt.

        Returns:
            The decoded settlement response, or None if the publisher sent none.
        """
        self.step(5, 5, "Read the settlement receipt")

        receipt = self._x402.read_receipt(response)
        if not receipt:
            self.say(dim("      No receipt header — the publisher did not report settlement."))
            return None

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

        quoted = self.request_unpaid()
        quote = self.get_quote(quoted)
        signed = self.sign_acceptance(quote)
        response = self.request_paid(quoted, signed)
        receipt = self.read_receipt(response)

        content = response.json()
        amount = quoted["amountPaise"]

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
        """Releases the shared client's HTTP connections."""
        self._x402.close()


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
