"""Supply chain orchestrator agent.

This is the entry point AgentCore Runtime executes. The @app.entrypoint
decorator is what turns an ordinary function into a runtime: the SDK serves
POST /invocations and GET /ping around it, which is the contract AgentCore
requires of any hosted agent.

What one invocation does
------------------------
    1. read the prompt, actor and session from the payload
    2. recall long-term memories for this actor
    3. mint a Cognito token and open an MCP session to the gateway
    4. list the tools the gateway currently exposes
    5. let the model reason and call tools until it has an answer
    6. record the exchange in memory
    7. return the text

Step 4 is the part worth understanding. The tools are not compiled in. They are
discovered at invocation time over MCP, so adding a gateway target makes a new
capability available to this agent with no redeploy.
"""

from __future__ import annotations

import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

import delegation
import identity
import memory

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOG = logging.getLogger("orchestrator")

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-pro-v1:0")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """\
You are the supply chain assistant for an industrial parts distributor.

You have two kinds of source and they are not interchangeable:

1. Live operational tools backed by the company's databases — inventory,
   suppliers, shipments and routes, quality inspections, compliance records and
   standards. Use these for anything about current state: quantities, statuses,
   dates, records, identifiers.

2. A company document search covering written procedures, manuals and policies.
   Use this for questions about how something is done or what the rules are.

Always call a tool rather than answering from general knowledge. If you do not
know a required identifier, look it up first — for example list products to
find a product id before checking its quality history.

When a tool returns an error, read it: it usually names the valid options or
the missing argument. Use that to recover rather than telling the user the
request failed.

Be concise and concrete. Prefer short structured answers over prose. Quantities,
identifiers and dates should be reported exactly as the tools return them, never
estimated or rounded. If a tool gives you nothing, say so plainly instead of
filling the gap with plausible detail.
"""


def _build_model() -> BedrockModel:
    """Configure the model, attaching the guardrail when one is deployed.

    The guardrail is applied at the model call. That means it covers every turn
    of the agent loop — including tool results being summarised back to the
    user — rather than only the final response.
    """
    kwargs = {
        "model_id": MODEL_ID,
        # Low but not zero: this is a factual assistant, and the little
        # variation left keeps phrasing natural.
        "temperature": 0.2,
    }

    if GUARDRAIL_ID:
        kwargs["guardrail_id"] = GUARDRAIL_ID
        kwargs["guardrail_version"] = GUARDRAIL_VERSION
        LOG.info("Guardrail %s (%s) attached", GUARDRAIL_ID, GUARDRAIL_VERSION)
    else:
        LOG.warning("No GUARDRAIL_ID set — responses are unfiltered")

    return BedrockModel(**kwargs)


def _gateway_client() -> MCPClient:
    """Open an MCP client against the AgentCore Gateway.

    The token is fetched inside the factory rather than captured once, because
    MCPClient calls the factory when it establishes the transport. Reading it
    at connection time means a long-lived container never reconnects with a
    token that expired while it was idle.
    """
    return MCPClient(
        lambda: streamablehttp_client(
            url=GATEWAY_URL,
            headers={"Authorization": f"Bearer {identity.get_access_token()}"},
        )
    )


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    """Handle one user message."""
    prompt = (payload or {}).get("prompt", "").strip()
    if not prompt:
        return {"result": "Ask me about inventory, suppliers, shipments or quality."}

    # actor_id identifies the person and is the key memories are filed under.
    # session_id scopes one conversation. Falling back to a shared anonymous
    # actor is fine for a demo; a real deployment would derive this from the
    # authenticated principal rather than trusting the payload.
    actor_id = (payload or {}).get("actor_id") or "anonymous"
    session_id = (
        (payload or {}).get("session_id")
        or getattr(context, "session_id", None)
        or "default-session"
    )

    LOG.info("actor=%s session=%s prompt=%r", actor_id, session_id, prompt)

    recalled = memory.recall(actor_id, session_id, prompt)
    system_prompt = SYSTEM_PROMPT + memory.as_prompt_section(recalled)

    # Tools available without the gateway: delegation to the KB specialist is a
    # local Python function, so it works even if tool discovery fails.
    local_tools = [delegation.search_company_documents] if delegation.available() else []

    if not GATEWAY_URL:
        LOG.warning("No GATEWAY_URL set — running with no operational tools")
        agent = Agent(model=_build_model(), tools=local_tools, system_prompt=system_prompt)
        answer = str(agent(prompt))
    else:
        client = _gateway_client()
        # The MCP session lives for the duration of the turn. Everything that
        # uses gateway tools has to happen inside this block — the tool objects
        # are bound to the open transport.
        with client:
            discovered = client.list_tools_sync()
            LOG.info("Discovered %s gateway tools", len(discovered))

            agent = Agent(
                model=_build_model(),
                tools=list(discovered) + local_tools,
                system_prompt=system_prompt,
            )
            answer = str(agent(prompt))

    memory.remember(actor_id, session_id, prompt, answer)

    return {
        "result": answer,
        "session_id": session_id,
        "memories_used": len(recalled),
    }


if __name__ == "__main__":
    # Starts the HTTP server AgentCore Runtime health-checks and invokes.
    app.run()
