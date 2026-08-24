"""Spending controls — what the facilitator will not let an agent do.

---------------------------------------------------------------------------
WHY THE FACILITATOR NEEDS THESE EVEN THOUGH THE AGENT HAS A BUDGET
---------------------------------------------------------------------------
`agent-kit/x402_client.py` enforces a budget before it will buy anything, and
that is the right place for an agent's *own* discipline. It is worth nothing
here. A facilitator that relies on clients policing themselves has no controls
at all — the client is the party whose behaviour is in question, and anyone can
write a different one.

Before this module the only limit was `MAX_OFFER_PAISE`, a ceiling on a
*single* quote. An agent could be quoted ₹1,000 ten thousand times and nothing
in the service would object. "No transaction may exceed X" is not a spending
limit; it is a transaction-size limit, and the difference is the entire
problem.

Three controls, in the order they actually bite:

  1. **A daily cap on committed spend, per agent.** The real liability. This is
     the one that was missing.
  2. **A quote rate limit, per agent.** An offer is a cheap write that costs
     the facilitator a row and the agent nothing. Without this, flooding
     `/offer` is free.
  3. **A freeze list.** The operator's stop button for one agent, or for
     everything.

---------------------------------------------------------------------------
WHY THE STOP BUTTON IS CONFIG AND NOT AN ENDPOINT
---------------------------------------------------------------------------
The obvious design is `POST /agents/{id}/freeze`. This deliberately does not
have one, because this service has no authentication: an unauthenticated
freeze endpoint lets any caller disable any agent, which converts a safety
control into a denial-of-service primitive. That is strictly worse than having
no endpoint.

So freezing is an environment variable — operator-controlled, no new attack
surface, and honest about the fact that a real deployment would put this
behind an authenticated admin plane rather than pretend one exists here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Stands in for "no cap" so the SQL guard in `Ledger.create_commitment` has one
# code path instead of two. Comfortably above any plausible rupee total and
# well inside a signed 64-bit column in both engines.
UNLIMITED_PAISE = 2**62


class LimitExceeded(Exception):
    """A request was refused by a spending control.

    Carries a machine-readable `reason` so `/verify` and `/settle` can report
    it in the x402 error shape, and `detail` so the caller learns what the
    limit actually is rather than only that it exists — an agent that knows it
    has ₹3.50 left can re-plan, whereas one told "refused" can only retry.
    """

    def __init__(self, reason: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class SpendPolicy:
    """The limits this facilitator applies, per agent per settlement date.

    Attributes:
        daily_cap_paise: Most an agent may commit in one settlement date.
            0 disables the cap.
        offer_rate_per_minute: Most quotes an agent may request per minute.
            0 disables the limit.
        frozen_agents: Agent ids refused outright.
        accept_payments: Global off switch. False refuses every agent, for
            draining the service before maintenance.
    """

    daily_cap_paise: int = 50_000  # ₹500
    offer_rate_per_minute: int = 60
    frozen_agents: frozenset[str] = frozenset()
    accept_payments: bool = True

    @classmethod
    def from_env(cls) -> SpendPolicy:
        """Builds the policy from environment variables."""
        frozen = {
            entry.strip()
            for entry in (os.getenv("FROZEN_AGENTS") or "").split(",")
            if entry.strip()
        }
        return cls(
            daily_cap_paise=int(os.getenv("AGENT_DAILY_CAP_PAISE", "50000")),
            offer_rate_per_minute=int(os.getenv("AGENT_OFFER_RATE_PER_MINUTE", "60")),
            frozen_agents=frozenset(frozen),
            accept_payments=(os.getenv("ACCEPT_PAYMENTS", "true").strip().lower()
                             in ("1", "true", "yes")),
        )

    @property
    def cap_for_sql(self) -> int:
        """The cap as the SQL guard wants it, with 0 meaning no limit."""
        return self.daily_cap_paise if self.daily_cap_paise > 0 else UNLIMITED_PAISE

    def check_admission(self, agent_id: str) -> None:
        """Refuses an agent that is frozen, or everything if payments are off.

        Checked before anything else and before any database read: a frozen
        agent should not be able to cost the service a query.

        Raises:
            LimitExceeded: If this agent may not transact at all.
        """
        if not self.accept_payments:
            raise LimitExceeded(
                "payments_suspended",
                "This facilitator is not accepting payments right now.",
            )
        if agent_id in self.frozen_agents:
            raise LimitExceeded(
                "agent_frozen",
                f"Agent {agent_id} is frozen and cannot transact.",
                agentId=agent_id,
            )

    def check_offer_rate(self, agent_id: str, recent_offers: int) -> None:
        """Refuses an agent asking for quotes too fast.

        Args:
            agent_id: Who is asking.
            recent_offers: Quotes this agent has been issued in the last
                minute, already counted by the caller.

        Raises:
            LimitExceeded: If the agent is over its quote rate.
        """
        if self.offer_rate_per_minute <= 0:
            return
        if recent_offers >= self.offer_rate_per_minute:
            raise LimitExceeded(
                "offer_rate_exceeded",
                f"Agent {agent_id} has requested {recent_offers} quotes in the last "
                f"minute, at or above the limit of {self.offer_rate_per_minute}. "
                "Retry shortly.",
                agentId=agent_id,
                limitPerMinute=self.offer_rate_per_minute,
                recentOffers=recent_offers,
            )

    def check_daily_cap(self, agent_id: str, committed_paise: int, amount_paise: int) -> None:
        """Refuses a payment that would take an agent over its daily cap.

        Advisory only — the binding check is the conditional INSERT in
        `Ledger.create_commitment`, which is atomic against concurrent
        settlements. This exists to refuse early, at quote time, so an agent
        finds out before it signs anything rather than after.

        Args:
            agent_id: Who is spending.
            committed_paise: What they have already committed today.
            amount_paise: What this request would add.

        Raises:
            LimitExceeded: If the total would breach the cap.
        """
        if self.daily_cap_paise <= 0:
            return
        if committed_paise + amount_paise > self.daily_cap_paise:
            remaining = max(self.daily_cap_paise - committed_paise, 0)
            raise LimitExceeded(
                "daily_cap_exceeded",
                f"Agent {agent_id} has committed {committed_paise} paise today and this "
                f"request adds {amount_paise}, over the daily cap of "
                f"{self.daily_cap_paise} paise. {remaining} paise remain.",
                agentId=agent_id,
                dailyCapPaise=self.daily_cap_paise,
                committedPaise=committed_paise,
                remainingPaise=remaining,
            )

    def describe(self) -> dict:
        """The policy as advertised on `/supported`, so clients can plan.

        A limit a client cannot discover is a limit it can only find out about
        by being refused.
        """
        return {
            "dailyCapPaise": self.daily_cap_paise or None,
            "offerRatePerMinute": self.offer_rate_per_minute or None,
            "acceptingPayments": self.accept_payments,
        }
