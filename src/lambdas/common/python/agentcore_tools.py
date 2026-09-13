"""Helpers shared by the four domain tool handlers.

The AgentCore Gateway invokes a Lambda target once per tool call. The tool
arguments arrive as the event body, and the *name* of the tool being called
arrives out of band, in the Lambda client context:

    context.client_context.custom["bedrockAgentCoreToolName"]

The gateway namespaces that name with the target it belongs to, so a handler
sees ``inventory-target___list_products`` rather than ``list_products``. Each
handler therefore registers a plain ``{tool_name: callable}`` mapping and lets
``dispatch`` do the unwrapping.
"""

from __future__ import annotations

import decimal
import json
import logging
import os
from typing import Any, Callable, Mapping

import boto3

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
TOOL_NAME_SEPARATOR = "___"

_dynamodb = boto3.resource("dynamodb")


class ToolError(Exception):
    """Raised for input the caller can fix, e.g. a missing required argument."""


def table(env_var: str):
    """Return the DynamoDB table named by an environment variable."""
    name = os.environ[env_var]
    return _dynamodb.Table(name)


def tool_name(context) -> str:
    """Extract the bare tool name from the Lambda client context."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    raw = custom.get(TOOL_NAME_KEY)
    if not raw:
        raise ToolError(
            "No tool name in client context. This function is meant to be "
            "invoked by an AgentCore Gateway target, not called directly."
        )
    # Strip the "<target-name>___" prefix the gateway prepends.
    return raw.split(TOOL_NAME_SEPARATOR)[-1]


def jsonable(value: Any) -> Any:
    """Convert DynamoDB types into something json.dumps accepts.

    DynamoDB returns numbers as ``decimal.Decimal``, which the JSON encoder
    rejects. Integral values become ``int`` so the model sees ``500`` rather
    than ``500.0``.
    """
    if isinstance(value, decimal.Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(jsonable(item) for item in value)
    return value


def dispatch(event: Mapping[str, Any], context, tools: Mapping[str, Callable]) -> dict:
    """Route one gateway tool call to its handler and serialise the result.

    Returns the shape the gateway expects: a JSON string body plus a status
    code. Errors are returned rather than raised so the model receives a
    message it can act on instead of an opaque invocation failure.
    """
    try:
        name = tool_name(context)
        handler = tools.get(name)
        if handler is None:
            raise ToolError(
                f"Unknown tool {name!r}. This handler serves: {', '.join(sorted(tools))}."
            )

        LOG.info("Invoking tool %s with %s", name, json.dumps(event, default=str))
        result = handler(event or {})
        return _ok(result)

    except ToolError as exc:
        LOG.warning("Tool error: %s", exc)
        return _error(str(exc), status=400)
    except Exception as exc:  # noqa: BLE001 - surface the message to the model
        LOG.exception("Unhandled tool failure")
        return _error(f"Tool execution failed: {exc}", status=500)


def _ok(result: Any) -> dict:
    return {
        "statusCode": 200,
        "body": json.dumps(jsonable(result), default=str),
    }


def _error(message: str, status: int) -> dict:
    return {
        "statusCode": status,
        "body": json.dumps({"error": message}),
    }


def require(args: Mapping[str, Any], key: str) -> str:
    """Fetch a required argument or raise a message the model can recover from."""
    value = args.get(key)
    if value in (None, ""):
        raise ToolError(f"The {key!r} argument is required.")
    return value
