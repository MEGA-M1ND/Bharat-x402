"""An MCP server that lets any agent buy things with rupees.

Exposes `tools.py` over the Model Context Protocol, so an MCP client — Claude
Desktop, an Agent Studio agent, anything that speaks MCP — can discover the
publisher's paid resources, decide what is worth buying, and pay for it in INR
over x402, without knowing anything about x402 or holding a signing key.

Run it:

    python agent-kit/mcp_server.py

Or register it with an MCP client (Claude Desktop's config, for example):

    {
      "mcpServers": {
        "bharat-x402": {
          "command": "python",
          "args": ["<repo>/agent-kit/mcp_server.py"],
          "env": {
            "AGENT_BUDGET_PAISE": "5000",
            "RESOURCE_BASE_URL": "http://localhost:3402",
            "FACILITATOR_URL": "http://localhost:8402"
          }
        }
      }
    }

---------------------------------------------------------------------------
WHAT THE MCP CLIENT DOES NOT GET
---------------------------------------------------------------------------
Deliberately, the tool surface has no `sign`, no `register`, and no way to
name an amount. The client can ask to buy a *named resource*; the price comes
from the publisher's 402 and the signature is produced here, by a private key
that never crosses the MCP boundary.

That matters because the caller is a language model. Anything the model can
put in a tool argument is something a crafted document could persuade it to
put there — so "amount" must not be one of those things, and the budget
ceiling lives in `x402_client.py` where no tool call can reach it. See that
module's docstring for the full argument.

Razorpay ships an official MCP server for its own API, which is the precedent
this follows: the payments capability is the thing being exposed to agents,
and the credentials stay behind it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Importable when run directly as a script, which is how an MCP client starts it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tools  # noqa: E402 - after the sys.path line above
from mcp.server import MCPServer  # noqa: E402
from x402_client import X402Client  # noqa: E402

server = MCPServer(
    name="bharat-x402",
    instructions=(
        "Buy paywalled content from an Indian publisher, paying in rupees over the "
        "x402 protocol. Call list_resources first to see what is available and what "
        "it costs, preview_resource to judge whether something is worth buying "
        "without paying for it, and fetch_paid_resource to buy it. Purchases are "
        "capped by a budget enforced server-side: an over-budget call is refused and "
        "charges nothing."
    ),
)


def _build_client() -> X402Client:
    """Builds the payment client from the environment."""
    return X402Client(
        agent_id=os.getenv("AGENT_ID", "agent-mcp-client"),
        budget_paise=int(os.getenv("AGENT_BUDGET_PAISE", "5000")),
        resource_base=os.getenv("RESOURCE_BASE_URL", "http://localhost:3402"),
        facilitator_url=os.getenv("FACILITATOR_URL", "http://localhost:8402"),
    )


# One tool registration per shared function. The decorator reads each
# function's signature and docstring for the schema and description, so the
# text the MCP client sees is the text in tools.py — there is no second copy
# of the descriptions to drift out of sync.
for _fn in tools.ALL_TOOLS:
    server.add_tool(_fn)


def main() -> None:
    tools.configure(_build_client())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
