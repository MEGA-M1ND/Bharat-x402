"""Razorpay integration, plus the fee arithmetic that justifies batching.

Two jobs:

  1. Create Payment Links through Razorpay's official Python SDK (test mode
     only — see the guard in `__init__`), with an offline mock so the demo runs
     with no account at all.
  2. Model what settlement actually costs, per-request versus batched. That
     model is the argument this project makes, so it is written to be checked
     rather than to flatter the conclusion.

--------------------------------------------------------------------------
A HONEST NOTE ON WHERE THE SAVING COMES FROM
--------------------------------------------------------------------------
It is tempting to claim batching slashes gateway fees. On a *pure percentage*
fee it does not, and anyone at a payments company will spot that immediately:
2% of a hundred ₹5 charges and 2% of one ₹500 charge are the same ₹10.

The real barriers to per-request INR settlement are these, in order of how
much they actually bite:

  1. **The gateway minimum.** Razorpay will not process an order below ₹1.00.
     A ₹0.50 API call is not expensive to settle individually — it is
     *impossible*. Batching is what makes sub-rupee pricing exist at all, and
     sub-rupee is exactly where agent API pricing wants to sit.

  2. **Checkout has a human in it.** A Payment Link is a hosted page somebody
     opens and pays on. You cannot put that in the path of an HTTP request an
     agent makes 10,000 times a day. One link per agent per day is a shape
     that works; one per request is not, at any fee.

  3. **Fixed per-transaction cost.** Any flat component of a fee multiplies by
     N. So does the operational cost: N webhooks, N reconciliation rows, N
     entries in a dispute report.

  4. **Percentage fees.** Genuinely neutral to batching. Included in the model
     below so the comparison is complete, not because it is the argument.

`estimate_settlement_cost` reports all four, including how many commitments
were below the gateway minimum and therefore unchargeable on their own.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

import razorpay


class RazorpayConfigError(Exception):
    """The facilitator is configured in a way that is unsafe to run."""


def _is_rate_limited(exc: Exception) -> bool:
    """Whether an SDK error is Razorpay's 429.

    The Python SDK collapses HTTP status into a small set of exception classes
    and puts the detail in the message, so there is no status code to read —
    matching on the text is the available option.
    """
    text = str(exc).lower()
    return "too many requests" in text or "429" in text or "rate limit" in text


@dataclass
class FeeModel:
    """Cost parameters for the settlement comparison.

    Defaults reflect Razorpay's publicly listed standard pricing at the time of
    writing (2% per transaction, plus 18% GST on the fee). They are
    configurable because real merchant rates are negotiated, and because the
    point of the model is to let someone plug in *their* numbers rather than
    trust these.

    Attributes:
        percent_bps: Percentage fee in basis points. 200 = 2.00%.
        fixed_paise: Flat per-transaction component, in paise.
        gst_bps: Tax applied to the fee itself. 1800 = 18%.
        minimum_charge_paise: Smallest amount the gateway will accept.
            Razorpay's documented floor is ₹1.00.
    """

    percent_bps: int = 200
    fixed_paise: int = 0
    gst_bps: int = 1800
    minimum_charge_paise: int = 100

    @classmethod
    def from_env(cls) -> FeeModel:
        """Builds a fee model from environment variables."""
        return cls(
            percent_bps=int(os.getenv("FEE_PERCENT_BPS", "200")),
            fixed_paise=int(os.getenv("FEE_FIXED_PAISE", "0")),
            gst_bps=int(os.getenv("FEE_GST_BPS", "1800")),
            minimum_charge_paise=int(os.getenv("RAZORPAY_MIN_CHARGE_PAISE", "100")),
        )

    def fee_for(self, amount_paise: int) -> int:
        """Total cost of taking one payment of `amount_paise`, including tax.

        Integer arithmetic throughout — floats have no business anywhere near a
        money calculation.

        Args:
            amount_paise: Transaction value in paise.

        Returns:
            Fee in paise, rounded to the nearest paisa.
        """
        base = (amount_paise * self.percent_bps + 5_000) // 10_000 + self.fixed_paise
        gst = (base * self.gst_bps + 5_000) // 10_000
        return base + gst


def estimate_settlement_cost(amounts_paise: list[int], model: FeeModel) -> dict:
    """Compares settling a list of charges individually versus as one batch.

    The comparison has to be careful about something easy to get wrong. A
    charge below the gateway minimum has no per-request fee — it has no
    per-request *path*. Quoting a hypothetical fee for it would imply the
    option exists. So amounts are split first, and the headline figure is the
    revenue per-request settlement could not have collected at all.

    (Getting this wrong in the obvious direction produces a flattering but
    false "batching saves N% of fees" claim. On a pure percentage fee it saves
    approximately nothing, and anyone who works in payments will know that.)

    Args:
        amounts_paise: The individual commitment amounts.
        model: Fee parameters.

    Returns:
        Both scenarios, the fee difference over the comparable subset, and how
        much revenue is only reachable by batching.
    """
    total = sum(amounts_paise)
    count = len(amounts_paise)

    chargeable = [a for a in amounts_paise if a >= model.minimum_charge_paise]
    unchargeable = [a for a in amounts_paise if a < model.minimum_charge_paise]

    # Only meaningful over amounts that could actually have been charged alone.
    per_request_fee = sum(model.fee_for(a) for a in chargeable)
    batched_fee = model.fee_for(total) if total else 0

    # Fee comparison restricted to the comparable subset, so we are not
    # crediting batching with money that had no alternative route.
    comparable_total = sum(chargeable)
    comparable_batched_fee = model.fee_for(comparable_total) if comparable_total else 0
    fee_delta = per_request_fee - comparable_batched_fee

    return {
        "commitmentCount": count,
        "totalPaise": total,
        # --- the argument -------------------------------------------------
        # Revenue that per-request settlement could not have collected at any
        # price, because each charge is under the gateway floor.
        "revenueUnreachablePerRequestPaise": sum(unchargeable),
        "belowGatewayMinimum": len(unchargeable),
        "gatewayMinimumPaise": model.minimum_charge_paise,
        # N gateway round-trips collapse to 1, whatever the fee schedule says.
        # Each avoided call is also an avoided webhook and reconciliation row.
        "gatewayCallsSaved": max(count - 1, 0),
        # --- fees, over the comparable subset only -------------------------
        "chargeableCount": len(chargeable),
        "perRequestFeePaise": per_request_fee,
        "batchedFeePaise": batched_fee,
        "comparableFeeDeltaPaise": fee_delta,
        "feeModel": {
            "percentBps": model.percent_bps,
            "fixedPaise": model.fixed_paise,
            "gstBps": model.gst_bps,
        },
        "note": (
            "A pure percentage fee is close to neutral on batching — 2% of many small "
            "charges is 2% of one large one, give or take rounding. Batching matters "
            f"because {len(unchargeable)} of {count} charges here fall under the "
            f"{format_paise(model.minimum_charge_paise)} gateway minimum and have no "
            "per-request path at all, because any fixed fee component multiplies by N, "
            "and because hosted checkout cannot sit in the path of a machine-to-machine "
            "request."
        ),
    }


def format_paise(paise: int) -> str:
    """Formats integer paise as a rupee string, e.g. 4750 -> '₹47.50'."""
    return f"₹{paise // 100}.{paise % 100:02d}"


class RazorpayGateway:
    """Creates Payment Links, for real or in mock mode.

    Args:
        key_id: Razorpay key id. Must be a test key.
        key_secret: Razorpay key secret.
        mock: Force mock mode. Also implied when credentials are absent.

    Raises:
        RazorpayConfigError: If handed a live key.
    """

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        mock: bool | None = None,
    ) -> None:
        key_id = (key_id or "").strip()
        key_secret = (key_secret or "").strip()

        # Hard stop on live credentials. This is a portfolio demo that creates
        # payment links in a loop; it has no business holding a key that can
        # move real money, and a typo in an env file should not be all that
        # stands between the two.
        if key_id.startswith("rzp_live_"):
            raise RazorpayConfigError(
                "Refusing to start with a live Razorpay key. This project is a demo and "
                "is only ever meant to run against rzp_test_ credentials. If you genuinely "
                "want live mode you will have to remove this guard deliberately."
            )

        env_mock = os.getenv("MOCK_RAZORPAY", "true").lower() in ("1", "true", "yes")
        has_credentials = bool(key_id and key_secret)

        self.mock = env_mock if mock is None else mock
        if not has_credentials:
            # No keys is not an error — it is the default way to run this repo.
            self.mock = True

        self.key_id = key_id
        self._client = None

        if not self.mock:
            if not key_id.startswith("rzp_test_"):
                raise RazorpayConfigError(
                    f"Expected a test-mode key starting with 'rzp_test_', got '{key_id[:12]}…'."
                )
            self._client = razorpay.Client(auth=(key_id, key_secret))
            # Identifies our traffic in Razorpay's logs. Good manners, and it
            # makes the demo's calls obvious in a dashboard.
            self._client.set_app_details({"title": "bharat-x402", "version": "0.3.0"})

    @property
    def mode(self) -> str:
        """Human-readable mode, for the ledger and reports."""
        return "mock" if self.mock else "razorpay_test"

    def check_credentials(self) -> tuple[bool, str]:
        """Confirms the configured keys actually authenticate.

        Called at startup rather than left to be discovered at settlement time.
        Without this, bad credentials surface only when the first batch runs —
        which in deferred mode is hours after the service came up looking
        healthy, with a day of commitments already booked against a gateway
        that was never going to accept them.

        Deliberately non-fatal. `/verify` and `/settle` do not touch Razorpay,
        so a facilitator with broken credentials can still accept payments and
        keep an accurate ledger; only `/settle-batch` is affected, and those
        commitments stay pending until someone fixes the keys. Refusing to
        start would turn a settlement problem into an outage.

        Returns:
            `(ok, message)` — message explains the failure when ok is False.
        """
        if self.mock:
            return True, "mock mode — no credentials needed"

        try:
            # Cheapest authenticated read available. `count=1` keeps the
            # response tiny; we care only about the status code.
            self._client.payment_link.all({"count": 1})
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never raised
            detail = str(exc)
            if "Authentication failed" in detail or "401" in detail:
                return False, (
                    "Razorpay rejected these credentials. The key id and secret must come "
                    "from the same key pair — Razorpay shows a secret only once, when it is "
                    "generated, so a regenerated key leaves the old secret dead while the old "
                    "key id still looks plausible. Re-copy both from the dashboard."
                )
            return False, f"Could not reach Razorpay: {detail}"

        return True, "credentials accepted"

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        description: str,
        reference_id: str,
        agent_id: str,
        notes: dict | None = None,
    ) -> dict:
        """Creates a Razorpay Payment Link for a settled batch.

        Args:
            amount_paise: Total to charge, in paise. Razorpay takes paise
                directly, which is why the whole project stores paise.
            description: Shown to whoever opens the link.
            reference_id: Our batch id. Razorpay enforces uniqueness on this,
                which gives us idempotency against double-charging a batch for
                free.
            agent_id: The paying agent, recorded in notes.
            notes: Extra key/value metadata. Razorpay allows up to 15 string pairs.

        Returns:
            `{"id", "short_url", "status", "mode"}`.

        Raises:
            RazorpayConfigError: If the amount is below the gateway minimum.
        """
        model = FeeModel.from_env()
        if amount_paise < model.minimum_charge_paise:
            raise RazorpayConfigError(
                f"Cannot charge {format_paise(amount_paise)} — below Razorpay's "
                f"{format_paise(model.minimum_charge_paise)} minimum. This is precisely the "
                "constraint that makes per-request settlement of micro-priced agent traffic "
                "impossible and deferred batching necessary."
            )

        if self.mock:
            # Shaped like a real response so nothing downstream needs to know.
            link_id = f"plink_MOCK{secrets.token_hex(7)}"
            return {
                "id": link_id,
                "short_url": f"https://rzp.io/i/mock-{link_id[-8:]}",
                "status": "created",
                "mode": self.mode,
                "amount": amount_paise,
                "currency": "INR",
            }

        return self._create_with_retry(
            amount_paise=amount_paise,
            description=description,
            reference_id=reference_id,
            agent_id=agent_id,
            notes=notes,
        )

    def _create_with_retry(
        self,
        *,
        amount_paise: int,
        description: str,
        reference_id: str,
        agent_id: str,
        notes: dict | None,
        attempts: int = 4,
    ) -> dict:
        """Creates a Payment Link, backing off on rate limits.

        Razorpay rate-limits its API, and a settlement run creates one link per
        paying agent back to back — so a publisher with fifty crawlers hits 429
        partway through the loop. Found by running this against the real test
        API, not by reading the docs.

        Retrying only on 429 is deliberate. A rejected amount or a bad
        reference id will fail identically every time; retrying those would
        just delay the error report.

        (Worth noting the rate limit is itself another argument for batching.
        Per-request settlement of agent traffic would not merely be expensive —
        it would exceed the gateway's request budget.)

        Args:
            amount_paise: Total to charge.
            description: Shown to the payer.
            reference_id: Our batch id; Razorpay enforces uniqueness on it.
            agent_id: Paying agent, recorded in notes.
            notes: Extra metadata.
            attempts: How many times to try before giving up.

        Returns:
            The created link.

        Raises:
            Exception: The last error, if every attempt failed.
        """
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": description[:2048],
            "reference_id": reference_id,
            # No notifications: the payer here is an automated agent, not a
            # person with an inbox.
            #
            # In production the link would not exist at all — it would be a
            # UPI Reserve Pay (SBMD) mandate, which is the rail Razorpay's own
            # agentic-payments product uses: funds are blocked up front under
            # a consent with spend limits, and the agent debits within those
            # limits without a checkout page or a PIN prompt per transaction.
            # Specifically *not* UPI Autopay, which is fixed-schedule
            # recurring collection and a different instrument.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "agent_id": agent_id,
                "settlement": "x402-deferred-batch",
                **{k: str(v) for k, v in (notes or {}).items()},
            },
        }

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                link = self._client.payment_link.create(payload)
            except Exception as exc:  # noqa: BLE001 - inspected, then re-raised below
                last_error = exc
                if not _is_rate_limited(exc) or attempt == attempts - 1:
                    raise
                # 1s, 2s, 4s. Razorpay's window is short; this clears it
                # without making a settlement run feel hung.
                time.sleep(2**attempt)
                continue

            return {
                "id": link["id"],
                "short_url": link.get("short_url"),
                "status": link.get("status", "created"),
                "mode": self.mode,
                "amount": link.get("amount", amount_paise),
                "currency": link.get("currency", "INR"),
            }

        raise last_error  # pragma: no cover - loop always returns or raises
