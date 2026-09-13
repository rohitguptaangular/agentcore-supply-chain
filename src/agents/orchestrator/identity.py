"""M2M token handling for gateway and A2A calls.

The secret is read from Cognito at runtime rather than passed in as an env var,
since env vars are visible to anyone with console access. Tokens are cached
until just before expiry.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

import boto3

LOG = logging.getLogger(__name__)

# Refresh this many seconds before actual expiry, so a token can never expire
# in flight between being handed out and being used.
EXPIRY_MARGIN_SECONDS = 60

_lock = threading.Lock()
_cached_token: str | None = None
_expires_at: float = 0.0
_client_secret: str | None = None


def get_access_token() -> str:
    """Return a valid M2M access token, minting a new one only when needed."""
    global _cached_token, _expires_at

    with _lock:
        if _cached_token and time.time() < _expires_at:
            return _cached_token

        token, expires_in = _request_token()
        _cached_token = token
        _expires_at = time.time() + max(expires_in - EXPIRY_MARGIN_SECONDS, 30)
        LOG.info("Minted new access token, valid for %ss", expires_in)
        return _cached_token


def _request_token() -> tuple[str, int]:
    """POST the client_credentials grant to the Cognito token endpoint."""
    domain = os.environ["COGNITO_DOMAIN"].rstrip("/")
    client_id = os.environ["COGNITO_CLIENT_ID"]
    secret = _get_client_secret()

    # Credentials go in the Authorization header rather than the body. Both are
    # permitted by the spec; the header form keeps them out of any middlebox
    # that logs request bodies.
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()

    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            # Scopes must match what the app client was granted, or Cognito
            # answers invalid_scope rather than issuing a narrower token.
            "scope": "supplychain/read supplychain/write",
        }
    ).encode()

    request = urllib.request.Request(
        f"{domain}/oauth2/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        import json

        payload = json.loads(response.read())

    return payload["access_token"], int(payload.get("expires_in", 3600))


def _get_client_secret() -> str:
    """Read the app client secret from Cognito, once per container."""
    global _client_secret

    if _client_secret is not None:
        return _client_secret

    cognito = boto3.client("cognito-idp")
    response = cognito.describe_user_pool_client(
        UserPoolId=os.environ["COGNITO_USER_POOL_ID"],
        ClientId=os.environ["COGNITO_CLIENT_ID"],
    )

    secret = response["UserPoolClient"].get("ClientSecret")
    if not secret:
        # The exact failure the lab began with: an app client created without
        # a secret cannot perform client_credentials at all.
        raise RuntimeError(
            "The Cognito app client has no secret, so the client_credentials "
            "grant is impossible. Recreate it with GenerateSecret enabled."
        )

    _client_secret = secret
    return _client_secret
