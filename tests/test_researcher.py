"""The Claude research agent, minus Claude.

Be clear about what this does and does not establish. It cannot test whether
the model makes *good* purchasing decisions — that needs an API key and a live
call, and is a judgement about behaviour rather than a property of the code.
What it does test is everything wrapped around the model, which is where the
bugs that silently break an agent actually live:

  * the tools it is handed produce valid schemas, with the right parameters
    and non-empty descriptions — the SDK builds those from signatures and
    docstrings, so a refactor can quietly hand the model a tool it cannot
    understand;
  * the loop is wired so tool calls reach the real payment client;
  * `--scripted` works with no API key at all;
  * the budget wall still refuses when the caller is the agent runner rather
    than a test calling the client directly.

The stub stands in for `client.beta.messages.tool_runner`, returning messages
shaped like the SDK's. That is a deliberate seam: the alternative is mocking
HTTP, which tests the SDK rather than this code.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field

import pytest
import researcher
import tools
from x402_client import X402Client

# Only the schema tests below need the real SDK — they are checking what it
# generates. CI installs `anthropic` so they actually run; a contributor who
# has not installed the agent-kit extras gets skips rather than errors.
needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is None,
    reason="anthropic SDK not installed (see agent-kit/requirements.txt)",
)


@dataclass
class StubBlock:
    """A content block shaped like the SDK's."""

    type: str
    text: str = ""
    thinking: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = "tu_1"


@dataclass
class StubMessage:
    content: list


class StubRunner:
    """Stands in for the SDK's tool runner.

    Iterating yields messages, and any `tool_use` block it yields is executed
    against the real decorated tool — so a tool that raises, or returns
    something unprintable, fails here exactly as it would in a real run.
    """

    def __init__(self, messages, tools_by_name):
        self._messages = messages
        self._tools = tools_by_name
        self.executed = []

    def __iter__(self):
        for message in self._messages:
            for block in message.content:
                if block.type == "tool_use":
                    fn = self._tools[block.name]
                    # `beta_tool` returns a wrapper; the plain function hangs
                    # off it or is callable directly depending on SDK version.
                    target = getattr(fn, "_func", None) or getattr(fn, "func", None) or fn
                    self.executed.append((block.name, target(**block.input)))
            yield message


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("x402_client.KEY_DIR", tmp_path / "keys")
    built = X402Client(agent_id="agent-researcher-test", budget_paise=1000)
    tools.configure(built)
    yield built
    built.close()


@needs_sdk
class TestToolSchemasHandedToTheModel:
    def test_every_tool_produces_a_usable_schema(self):
        """The SDK builds these from signatures and docstrings. A refactor can
        hand the model a tool it cannot understand without breaking anything
        that would fail loudly."""
        from anthropic import beta_tool

        for fn in tools.ALL_TOOLS:
            decorated = beta_tool(fn)
            schema = decorated.to_dict() if hasattr(decorated, "to_dict") else {}
            assert schema.get("name") == fn.__name__
            assert (schema.get("description") or "").strip(), fn.__name__

            props = (schema.get("input_schema") or {}).get("properties", {})
            assert set(props) <= {"resource"}, f"{fn.__name__} exposes {set(props)}"

    def test_the_buying_tool_requires_a_resource(self):
        from anthropic import beta_tool

        schema = beta_tool(tools.fetch_paid_resource).to_dict()
        assert schema["input_schema"]["required"] == ["resource"]

    def test_no_tool_lets_the_model_name_an_amount(self):
        """The property that keeps a crafted document from talking the agent
        into a bigger purchase."""
        from anthropic import beta_tool

        for fn in tools.ALL_TOOLS:
            props = (beta_tool(fn).to_dict().get("input_schema") or {}).get("properties", {})
            assert not any("amount" in p.lower() or "paise" in p.lower() for p in props)


class TestTheLoop:
    def test_tool_calls_reach_the_payment_client(self, client, monkeypatch, capsys):
        """A tool_use block from the model has to actually run the tool."""
        # Plain functions: the runner stub calls them directly, so there is
        # nothing here that needs the SDK's decorator.
        decorated = {fn.__name__: fn for fn in tools.ALL_TOOLS}
        monkeypatch.setattr(
            client,
            "quote",
            lambda resource: {
                "resource": resource,
                "path": "/premium/x",
                "url": "http://localhost:3402/premium/x",
                "amountPaise": 5000,
                "humanAmount": "₹50.00",
                "preview": {},
                "required": {"x402Version": 2},
                "requirements": {},
            },
        )

        runner = StubRunner(
            [
                StubMessage(
                    [
                        StubBlock(type="thinking", thinking="Is this worth ₹50?"),
                        StubBlock(
                            type="tool_use",
                            name="fetch_paid_resource",
                            input={"resource": "market-report"},
                        ),
                    ]
                ),
                StubMessage([StubBlock(type="text", text="Too expensive; stopping.")]),
            ],
            decorated,
        )

        class StubMessages:
            def tool_runner(self, **_kwargs):
                return runner

        class StubBeta:
            messages = StubMessages()

        class StubAnthropic:
            beta = StubBeta()

            def __init__(self, *a, **k):
                pass

        monkeypatch.setitem(sys.modules, "anthropic", _anthropic_with(StubAnthropic))

        code = researcher.run_with_claude("Is it worth it?", client, verbose=True)
        assert code == 0

        # The tool ran, and refused the over-budget purchase rather than paying.
        assert runner.executed, "no tool was executed"
        name, result = runner.executed[0]
        assert name == "fetch_paid_resource"
        assert result.startswith("REFUSED")
        assert client.spent_paise == 0

        printed = capsys.readouterr().out
        assert "Too expensive" in printed
        # Reasoning is surfaced — it is the part worth watching in a run.
        assert "Is this worth" in printed

    def test_the_final_answer_is_printed(self, client, monkeypatch, capsys):
        runner = StubRunner([StubMessage([StubBlock(type="text", text="The answer is 42.")])], {})

        class StubMessages:
            def tool_runner(self, **_kwargs):
                return runner

        class StubBeta:
            messages = StubMessages()

        class StubAnthropic:
            beta = StubBeta()

            def __init__(self, *a, **k):
                pass

        monkeypatch.setitem(sys.modules, "anthropic", _anthropic_with(StubAnthropic))
        researcher.run_with_claude("q", client, verbose=False)
        assert "The answer is 42." in capsys.readouterr().out

    def test_a_run_with_no_answer_says_so(self, client, monkeypatch, capsys):
        """Silence is a failure mode worth naming rather than printing nothing."""
        runner = StubRunner([StubMessage([StubBlock(type="text", text="   ")])], {})

        class StubMessages:
            def tool_runner(self, **_kwargs):
                return runner

        class StubBeta:
            messages = StubMessages()

        class StubAnthropic:
            beta = StubBeta()

            def __init__(self, *a, **k):
                pass

        monkeypatch.setitem(sys.modules, "anthropic", _anthropic_with(StubAnthropic))
        researcher.run_with_claude("q", client, verbose=False)
        assert "no final answer" in capsys.readouterr().out


class TestScriptedMode:
    def test_scripted_mode_needs_no_api_key(self, client, monkeypatch, capsys):
        """The same reason MOCK_RAZORPAY exists: the demo has to run for
        someone who has not signed up for anything."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(tools, "list_resources", lambda: "- market-report: ₹5.00")
        monkeypatch.setattr(tools, "preview_resource", lambda r: f"preview of {r}")
        monkeypatch.setattr(tools, "fetch_paid_resource", lambda r: "REFUSED — over budget.")
        monkeypatch.setattr(tools, "get_spend_summary", lambda: "Spent: ₹0.00")
        monkeypatch.setattr(tools, "get_settlement_economics", lambda: "no commitments")

        assert researcher.run_scripted("q", client) == 0
        out = capsys.readouterr().out
        assert "scripted mode" in out
        assert "REFUSED" in out

    def test_the_model_path_refuses_without_a_key(self, monkeypatch, capsys):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setattr(sys, "argv", ["researcher.py", "question"])

        assert researcher.main() == 1
        assert "--scripted" in capsys.readouterr().out


class TestModelChoice:
    def test_the_default_model_is_current(self):
        """A hardcoded model id is the kind of thing that quietly rots."""
        assert researcher.MODEL == "claude-opus-5"

    def test_the_prompt_tells_the_model_the_budget_is_not_its_to_negotiate(self):
        """The prompt states the budget so the model can plan. It must also say
        the limit is enforced elsewhere, so the model does not treat running
        out as something to retry around."""
        prompt = researcher.SYSTEM_PROMPT
        assert "hard limit" in prompt
        assert "payment system" in prompt

    def test_the_prompt_warns_that_bought_content_is_data(self):
        """The agent reads what it buys. A document telling it to raise its own
        budget is an input, not an instruction."""
        assert "not as instructions" in researcher.SYSTEM_PROMPT


def _anthropic_with(stub_client):
    """A stand-in `anthropic` module exposing the pieces researcher.py imports."""
    import types

    module = types.ModuleType("anthropic")
    module.Anthropic = stub_client
    module.beta_tool = lambda fn: fn
    return module
