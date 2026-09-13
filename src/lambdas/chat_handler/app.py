"""Chat handler — the only public entry point to the agent.

Responsibilities, in order:
    1. accept a message from the browser
    2. exchange the Cognito client credentials for a short-lived access token
    3. invoke the orchestrator runtime with that token
    4. return plain JSON the UI can render

Why this function has to exist
------------------------------
The browser cannot do step 2 or 3 itself. Step 2 needs the app client secret,
which must never be shipped to a client. Step 3 needs an endpoint that speaks
CORS, which AgentCore does not. Everything here is about keeping credentials
server-side and presenting a boring HTTP contract to the UI.

Why HTTPS rather than boto3
---------------------------
The orchestrator runtime uses a CUSTOM_JWT authorizer. AWS documents that an
OAuth-protected runtime cannot be invoked through the SDK, because the SDK
signs requests with SigV4 and a JWT authorizer will not accept that. So the
call is made directly against the InvokeAgentRuntime HTTPS endpoint with a
bearer token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ORCHESTRATOR_RUNTIME_ARN = os.environ["ORCHESTRATOR_RUNTIME_ARN"]
COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"].rstrip("/")
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
REGION = os.environ.get("AWS_REGION", "us-east-1")

QUALIFIER = "DEFAULT"
EXPIRY_MARGIN_SECONDS = 60

# API Gateway cuts the integration off at 30s, so never wait longer than that.
INVOKE_TIMEOUT_SECONDS = 25

_cognito = boto3.client("cognito-idp")

# Cached across invocations for as long as the container lives. Lambda may run
# several containers concurrently; each simply mints its own token.
_lock = threading.Lock()
_token: str | None = None
_token_expires_at = 0.0
_client_secret: str | None = None


def lambda_handler(event, _context):
    route = (event.get("requestContext", {}).get("http", {}) or {}).get("path", "")
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "")

    if method == "GET" and route.endswith("/health"):
        return _response(200, {"status": "ok"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be JSON."})

    message = (body.get("message") or body.get("prompt") or "").strip()
    if not message:
        return _response(400, {"error": "A 'message' field is required."})

    # The session id groups turns into one conversation; the actor id is who
    # the memories belong to. The UI generates both and keeps them in browser
    # storage. A production system would take the actor from an authenticated
    # user identity rather than trusting the client.
    session_id = body.get("session_id") or "default-session"
    actor_id = body.get("actor_id") or "anonymous"

    LOG.info("actor=%s session=%s message=%r", actor_id, session_id, message)

    try:
        result = _invoke_orchestrator(
            {"prompt": message, "session_id": session_id, "actor_id": actor_id}
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        LOG.error("Runtime returned HTTP %s: %s", exc.code, detail)
        return _response(502, {"error": _explain(exc.code, detail)})
    except urllib.error.URLError as exc:
        LOG.exception("Could not reach the agent runtime")
        return _response(504, {"error": f"The agent did not respond: {exc.reason}"})
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Unexpected failure")
        return _response(500, {"error": f"Unexpected failure: {exc}"})

    return _response(200, {"reply": result, "session_id": session_id})


def _invoke_orchestrator(payload: dict) -> str:
    """POST to the runtime's invocation endpoint with a bearer token."""
    encoded_arn = urllib.parse.quote(ORCHESTRATOR_RUNTIME_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier={QUALIFIER}"
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_get_token()}",
            # Lets AgentCore correlate turns into one runtime session.
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": payload["session_id"],
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=INVOKE_TIMEOUT_SECONDS) as response:
        raw = response.read().decode()

    return _extract_text(raw)


def _extract_text(raw: str) -> str:
    """Pull the answer out of the runtime response, tolerantly."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("result", "response", "output", "text", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return json.dumps(parsed)


def _get_token() -> str:
    """Return a cached M2M token, minting a new one shortly before expiry."""
    global _token, _token_expires_at

    with _lock:
        if _token and time.time() < _token_expires_at:
            return _token

        secret = _get_client_secret()
        basic = base64.b64encode(f"{COGNITO_CLIENT_ID}:{secret}".encode()).decode()

        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": "supplychain/read supplychain/write",
            }
        ).encode()

        request = urllib.request.Request(
            f"{COGNITO_DOMAIN}/oauth2/token",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())

        _token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        _token_expires_at = time.time() + max(expires_in - EXPIRY_MARGIN_SECONDS, 30)

        LOG.info("Minted access token valid for %ss", expires_in)
        return _token


def _get_client_secret() -> str:
    """Read the app client secret from Cognito rather than from config."""
    global _client_secret

    if _client_secret is not None:
        return _client_secret

    response = _cognito.describe_user_pool_client(
        UserPoolId=COGNITO_USER_POOL_ID, ClientId=COGNITO_CLIENT_ID
    )
    secret = response["UserPoolClient"].get("ClientSecret")

    if not secret:
        raise RuntimeError(
            "The Cognito app client has no secret, so client_credentials cannot "
            "be used. Recreate the client with GenerateSecret enabled."
        )

    _client_secret = secret
    return _client_secret


def _explain(status: int, detail: str) -> str:
    """Turn a runtime failure into something actionable in the UI.

    These three are the failures this system actually produces, and each one
    was seen at least once while building it.
    """
    if status == 404:
        return (
            "The agent endpoint was not found. This usually means the runtime "
            "is mid-deployment and its DEFAULT endpoint has not finished "
            "rolling to the new version — try again shortly."
        )
    if status in (401, 403):
        return (
            "The agent rejected the request. The Cognito client is probably "
            "not in the runtime's allowed clients list, or the token is "
            "missing the supplychain scopes."
        )
    return f"The agent returned an error (HTTP {status}). {detail[:300]}"


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # API Gateway adds CORS headers for the configured origins, but
            # setting them here too keeps direct Function URL testing working.
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        },
        "body": json.dumps(body),
    }
