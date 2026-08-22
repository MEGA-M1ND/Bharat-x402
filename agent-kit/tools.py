"""The agent-facing tool surface, defined once.

Five functions an autonomous agent needs to buy things over x402: see what is
for sale, look before buying, buy, check what it has spent, and read the
settlement economics.

---------------------------------------------------------------------------
WHY THIS IS A SEPARATE FILE FROM BOTH THINGS THAT USE IT
---------------------------------------------------------------------------
There are two surfaces onto these tools and they must not drift apart:

  * `mcp_server.py` exposes them over MCP, so any MCP client — Claude Desktop,
    an Agent Studio agent, anything else — can drive the facilitator.
  * `researcher.py` hands the same functions to the Anthropic SDK's tool
    runner, so a Claude agent can drive it directly without an MCP hop.

Both wrap *these* functions rather than reimplementing the calls. This project
has already been bitten once by the same logic existing in two places — a
hand-rolled SQL splitter that was fixed in one copy and not the other, which
real Postgres then caught in CI — and a tool surface is exactly the kind of
thing that grows a second copy quietly.

---------------------------------------------------------------------------
THESE DOCSTRINGS ARE THE PROMPT
---------------------------------------------------------------------------
The Anthropic SDK builds each tool's JSON schema and description from the
signature and docstring below, and the MCP server does the same. So these are
not developer comments — they are the text the model reads when deciding what
to call and what the result means. They are written for that audience: what
the tool does, what it costs, and what a refusal means, in the fewest words
that are still unambiguous.
"""

from __future__ import annotations

from x402_client import BudgetExceeded, PaymentRefused, X402Client, _rupees

# Set by configure(); every tool below reads it. A module global rather than a
# parameter because the tool signatures are the model's API — threading a
# client handle through them would put an argument in front of the model that
# it has no business choosing.
_client: X402Client | None = None


def configure(client: X402Client) -> None:
    """Binds the tool surface to a configured client."""
    global _client
    _client = client


def _require_client() -> X402Client:
    if _client is None:
        raise RuntimeError("tools.configure(client) has not been called")
    return _client


def list_resources() -> str:
    """List the publisher's resources and what each one costs.

    Use this first, to see what is available before deciding what to buy.

    Returns:
        One line per resource: its key, price, and a short description.
    """
    client = _require_client()
    lines = []
    for entry in client.resources():
        lines.append(
            f"- {entry['key']}: {entry['price']} — {entry['title']}. {entry['description']}"
        )
    lines.append(
        f"\nBudget remaining: {_rupees(client.remaining_paise())} "
        f"of {_rupees(client.budget_paise)}."
    )
    return "\n".join(lines)


def preview_resource(resource: str) -> str:
    """Look at a paid resource without buying it.

    Costs nothing. Asking for the resource without payment returns HTTP 402,
    and that refusal carries the price plus the publisher's own summary of
    what is behind the paywall — enough to judge whether it is worth buying.

    Args:
        resource: Resource key, from list_resources.

    Returns:
        The price and the publisher's preview, or an error description.
    """
    client = _require_client()
    try:
        quoted = client.quote(resource)
    except (PaymentRefused, ValueError) as exc:
        return f"Could not preview {resource}: {exc}"

    preview = quoted.get("preview") or {}
    parts = [
        f"{resource} costs {_rupees(quoted['amountPaise'])}.",
        f"Title: {preview.get('title', 'unknown')}",
        f"Publisher: {preview.get('publisher', 'unknown')}",
        f"Summary: {preview.get('summary', '(none offered)')}",
        f"Budget remaining: {_rupees(client.remaining_paise())}.",
    ]
    return "\n".join(parts)


def fetch_paid_resource(resource: str) -> str:
    """Buy a resource and return its full contents.

    This spends real money from the agent's budget. The payment is settled in
    INR over the x402 protocol: the publisher quotes a price, this agent signs
    a commitment with its own private key, and the publisher releases the
    content.

    The budget is enforced by the payment client, not by your judgement. If a
    purchase would exceed it the call is refused and nothing is charged — you
    will get a message saying so, and can choose something cheaper or stop.

    Args:
        resource: Resource key, from list_resources.

    Returns:
        The resource contents and what it cost, or an explanation of a refusal.
    """
    client = _require_client()
    try:
        purchase = client.pay_and_fetch(resource)
    except BudgetExceeded as exc:
        return f"REFUSED — {exc} Nothing was charged."
    except (PaymentRefused, ValueError) as exc:
        return f"Could not buy {resource}: {exc}"

    content = purchase.content
    body = [
        f"Bought {resource} for {_rupees(purchase.amount_paise)} "
        f"(commitment {purchase.commitment_id}).",
        f"Budget remaining: {_rupees(client.remaining_paise())}.",
        "",
        f"Title: {content.get('title', '')}",
    ]
    if content.get("summary"):
        body.append(f"Summary: {content['summary']}")
    if content.get("findings"):
        body.append("Findings:")
        body.extend(f"  - {f}" for f in content["findings"])
    # The micro-priced resource is a data lookup, not a document.
    for key in ("pair", "rate", "note"):
        if content.get(key) is not None:
            body.append(f"{key}: {content[key]}")

    return "\n".join(body)


def get_spend_summary() -> str:
    """Report what this agent has spent so far, and what is left.

    Reads the facilitator's ledger as well as this session's own tally, so the
    two can be compared. The ledger is authoritative.

    Returns:
        Budget, spend, remaining, and every purchase made this run.
    """
    client = _require_client()
    summary = client.spend_summary()

    lines = [
        f"Budget:    {_rupees(summary['budgetPaise'])}",
        f"Spent:     {_rupees(summary['spentPaise'])}",
        f"Remaining: {_rupees(summary['remainingPaise'])}",
        f"Purchases: {len(summary['purchases'])}",
    ]
    for purchase in summary["purchases"]:
        lines.append(
            f"  - {purchase['resource']}: {_rupees(purchase['amountPaise'])} "
            f"({purchase['commitmentId']})"
        )
    if summary.get("ledgerCommittedPaise") is not None:
        lines.append(
            f"Facilitator ledger: {_rupees(summary['ledgerCommittedPaise'])} committed, "
            f"{_rupees(summary.get('ledgerCollectedPaise') or 0)} collected."
        )
    return "\n".join(lines)


def get_settlement_economics() -> str:
    """Explain how this agent's purchases will actually be settled in INR.

    Every purchase is recorded as a commitment rather than charged
    immediately, and collapsed into one payment later. This reports what that
    batching does — in particular how much of the spend is made up of charges
    too small for a payment gateway to process individually.

    Returns:
        The settlement comparison for this agent's traffic.
    """
    client = _require_client()
    data = client.economics()
    econ = data.get("economics")
    if not econ:
        return "No commitments yet today, so there is nothing to settle."

    return "\n".join(
        [
            f"Commitments: {econ['commitmentCount']} totalling {_rupees(econ['totalPaise'])}",
            f"Gateway minimum: {_rupees(econ['gatewayMinimumPaise'])} per charge",
            f"Charges below that minimum: {econ['belowGatewayMinimum']}",
            f"Revenue impossible to collect per-request: "
            f"{_rupees(econ['revenueUnreachablePerRequestPaise'])}",
            f"Gateway calls saved by batching: {econ['gatewayCallsSaved']}",
            "",
            econ["note"],
        ]
    )


# The canonical order, so both surfaces expose the same tools in the same
# sequence — the model sees a stable tool list, which keeps the prompt prefix
# cacheable.
ALL_TOOLS = [
    list_resources,
    preview_resource,
    fetch_paid_resource,
    get_spend_summary,
    get_settlement_economics,
]
