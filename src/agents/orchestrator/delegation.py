"""A2A delegation to the knowledge base specialist.

Exposed to the model as a normal tool, so it selects it the same way it selects
a database tool.

Uses raw HTTPS rather than boto3: the specialist has a CUSTOM_JWT authorizer,
and an OAuth-protected runtime can't be invoked through the SDK (the SDK signs
with SigV4, which a JWT authorizer rejects). Leaving the specialist on IAM auth
and using boto3 would work too — this keeps one identity model across every hop
instead.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from strands import tool

import identity

LOG = logging.getLogger(__name__)

SPECIALIST_ARN = os.environ.get("KB_SPECIALIST_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Callers address an endpoint, never the runtime itself. DEFAULT is the
# endpoint every component in this project uses.
QUALIFIER = "DEFAULT"

REQUEST_TIMEOUT_SECONDS = 60


def available() -> bool:
    """The agent must degrade gracefully when the knowledge base is disabled."""
    return bool(SPECIALIST_ARN)


@tool
def search_company_documents(query: str) -> str:
    """Search internal procedures, manuals and policy documents.

    Use this for questions about how the company does something — receiving
    procedures, supplier onboarding rules, quality control processes, returns
    policy. It searches written documentation, not live data.

    Do not use it for current quantities, shipment status, supplier records or
    inspection results; those come from the inventory, logistics, supplier and
    quality tools.

    Args:
        query: The question to answer from company documentation, phrased as
            the user asked it.

    Returns:
        Relevant passages with the documents they came from.
    """
    if not available():
        return (
            "The company document search is not available in this deployment. "
            "Tell the user you can only answer from live supply chain data."
        )

    LOG.info("Delegating to KB specialist: %s", query)

    try:
        response = _invoke_specialist({"prompt": query})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        LOG.error("Specialist returned HTTP %s: %s", exc.code, detail)
        # Returned rather than raised: the model can tell the user this one
        # source failed and still answer from the tools it does have.
        return f"The document search failed (HTTP {exc.code}). {detail[:200]}"
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Specialist delegation failed")
        return f"The document search failed: {exc}"

    return response


def _invoke_specialist(payload: dict) -> str:
    """POST to the specialist runtime's invocation endpoint with a bearer token."""
    # The ARN contains characters that are not URL-safe, so it is encoded into
    # the path. This is the documented HTTPS form of InvokeAgentRuntime and the
    # first thing to check if delegation starts failing.
    encoded_arn = urllib.parse.quote(SPECIALIST_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier={QUALIFIER}"
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {identity.get_access_token()}",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read().decode()

    return _extract_text(body)


def _extract_text(body: str) -> str:
    """Pull readable text out of the specialist's response.

    The specialist returns JSON, but being tolerant here costs nothing and
    avoids a brittle failure if its response shape ever changes.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("result", "response", "output", "text", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return json.dumps(parsed)
