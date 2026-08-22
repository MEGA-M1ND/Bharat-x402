"""The agent kit — the budget wall, and the tool surface an LLM reads.

The tests that matter here are the ones about the spending limit. Once a
language model is the thing choosing what to buy, "it was told not to
overspend" stops being a control: the model reads the documents it purchases,
and a document is an input an attacker can write. So the limit has to live
somewhere a tool call cannot reach, and that is what most of this file checks.

The x402 negotiation itself is covered end to end by the integration tests in
test_full_flow.py; these tests stub the network deliberately, so a failure
here means the budget logic is wrong rather than that a service was down.
"""

from __future__ import annotations

import pytest
from x402_client import BudgetExceeded, PaymentRefused, X402Client, _rupees


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client with a ₹10 budget whose keypair lives in a throwaway directory."""
    monkeypatch.setenv("AGENT_KEY_DIR", str(tmp_path / "keys"))
    import x402_client

    monkeypatch.setattr(x402_client, "KEY_DIR", tmp_path / "keys")
    built = X402Client(agent_id="agent-under-test", budget_paise=1000)
    yield built
    built.close()


def _priced(client, resource: str, paise: int) -> None:
    """Makes `resource` quote at `paise`, without touching the network."""
    client.quote = lambda r, _p=paise, _r=resource: {  # type: ignore[method-assign]
        "resource": r,
        "path": f"/premium/{r}",
        "url": f"http://localhost:3402/premium/{r}",
        "amountPaise": _p,
        "humanAmount": _rupees(_p),
        "preview": {"title": "stub"},
        "required": {"x402Version": 2},
        "requirements": {"payTo": "acc_x", "scheme": "razorpay-inr", "network": "n", "extra": {}},
    }


class TestBudgetWall:
    def test_over_budget_purchase_is_refused(self, client):
        _priced(client, "expensive", 5000)
        with pytest.raises(BudgetExceeded):
            client.pay_and_fetch("expensive")

    def test_a_refused_purchase_charges_nothing(self, client):
        """The refusal has to happen before the offer is even requested, so a
        rejected purchase leaves no consumed offer and no ledger row."""
        _priced(client, "expensive", 5000)

        # Any attempt to register or pay would go through the HTTP client;
        # replacing it with something that explodes proves none of that ran.
        def explode(*_args, **_kwargs):
            raise AssertionError("network was touched on a refused purchase")

        client._http.post = explode  # type: ignore[method-assign]
        client._http.get = explode  # type: ignore[method-assign]

        with pytest.raises(BudgetExceeded):
            client.pay_and_fetch("expensive")

        assert client.spent_paise == 0
        assert client.purchases == []

    def test_remaining_budget_never_goes_negative(self, client):
        client.spent_paise = 1200
        assert client.remaining_paise() == 0

    def test_the_message_says_what_is_left(self, client):
        """The agent re-plans from this string, so it has to carry the numbers."""
        _priced(client, "expensive", 5000)
        with pytest.raises(BudgetExceeded) as excinfo:
            client.pay_and_fetch("expensive")

        message = str(excinfo.value)
        assert "₹50.00" in message
        assert "₹10.00" in message

    def test_budget_is_not_reachable_from_a_tool_argument(self):
        """The tools an LLM can call take a resource key and nothing else.

        No amount, no budget, no account. That is the property that keeps a
        crafted document from talking the agent into a bigger purchase — the
        model can choose *what* to buy and the publisher decides what it costs.
        """
        import inspect

        import tools

        for fn in tools.ALL_TOOLS:
            params = set(inspect.signature(fn).parameters)
            assert params <= {"resource"}, f"{fn.__name__} exposes {params} to the model"


class TestToolSurface:
    def test_every_tool_has_a_docstring(self):
        """These docstrings are the tool descriptions the model reads — the
        SDK and the MCP server both build the schema from them. A missing one
        degrades the agent silently rather than failing loudly."""
        import tools

        for fn in tools.ALL_TOOLS:
            assert fn.__doc__ and len(fn.__doc__.strip()) > 40, fn.__name__

    def test_tools_are_unconfigured_until_bound(self, monkeypatch):
        import tools

        monkeypatch.setattr(tools, "_client", None)
        with pytest.raises(RuntimeError):
            tools.list_resources()

    def test_refusal_is_returned_as_text_not_raised(self, client, monkeypatch):
        """The agent should be able to read a refusal and choose something
        cheaper. An exception would just end the run."""
        import tools

        tools.configure(client)
        _priced(client, "expensive", 5000)

        result = tools.fetch_paid_resource("expensive")
        assert result.startswith("REFUSED")
        assert "Nothing was charged" in result

    def test_unknown_resource_is_reported_not_raised(self, client, monkeypatch):
        import tools

        tools.configure(client)

        def unknown(_resource):
            raise ValueError("unknown resource 'nope'")

        monkeypatch.setattr(client, "quote", unknown)
        assert "Could not buy" in tools.fetch_paid_resource("nope")

    def test_payment_failure_is_reported_not_raised(self, client, monkeypatch):
        import tools

        tools.configure(client)

        def refused(_resource):
            raise PaymentRefused("publisher said no")

        monkeypatch.setattr(client, "quote", refused)
        assert "Could not buy" in tools.fetch_paid_resource("market-report")


class TestSpendAccounting:
    def test_spend_is_counted_from_purchases_not_intentions(self, client):
        """`spent_paise` moves only when the publisher actually served content."""
        assert client.spend_summary()["spentPaise"] == 0

        _priced(client, "cheap", 50)
        client.spent_paise = 50  # what pay_and_fetch does after a 200

        summary = client.spend_summary()
        assert summary["spentPaise"] == 50
        assert summary["remainingPaise"] == 950

    def test_rupee_formatting_is_exact(self):
        """Money assertions on integer paise — a payment system that is
        approximately right is wrong."""
        assert _rupees(0) == "₹0.00"
        assert _rupees(50) == "₹0.50"
        assert _rupees(500) == "₹5.00"
        assert _rupees(1005) == "₹10.05"
