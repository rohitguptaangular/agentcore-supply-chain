"""Logistics domain tools.

Serves three tools declared in schemas/logistics_tools.json:
    list_shipments(status?)  -> all shipments, optionally by status
    get_shipment(shipment_id) -> one shipment, enriched with its route
    get_route(route_id)       -> one route

This handler owns two tables — shipments and routes — because they are one
bounded concept: a shipment travels on a route, and the agent almost always
wants both together.
"""

from __future__ import annotations

from typing import Any, Mapping

from boto3.dynamodb.conditions import Attr

from agentcore_tools import ToolError, dispatch, require, table


def list_shipments(args: Mapping[str, Any]) -> dict:
    """Return all shipments, optionally filtered by status."""
    shipments = table("SHIPMENTS_TABLE")
    status = args.get("status")

    if status:
        response = shipments.scan(FilterExpression=Attr("status").eq(status.upper()))
    else:
        response = shipments.scan()

    items = response.get("Items", [])
    return {"count": len(items), "shipments": items}


def get_shipment(args: Mapping[str, Any]) -> dict:
    """Return one shipment, joined to its route when it has one.

    The join happens here rather than leaving the agent to make a second tool
    call. One round trip instead of two is fewer model turns, less latency and
    a cheaper conversation.
    """
    shipment_id = require(args, "shipment_id")
    response = table("SHIPMENTS_TABLE").get_item(Key={"shipment_id": shipment_id})

    shipment = response.get("Item")
    if shipment is None:
        raise ToolError(f"No shipment found with shipment_id {shipment_id!r}.")

    result: dict[str, Any] = {"shipment": shipment}

    route_id = shipment.get("route_id")
    if route_id:
        route = table("ROUTES_TABLE").get_item(Key={"route_id": route_id}).get("Item")
        if route:
            result["route"] = route

    return result


def get_route(args: Mapping[str, Any]) -> dict:
    """Return a single shipping route by key."""
    route_id = require(args, "route_id")
    response = table("ROUTES_TABLE").get_item(Key={"route_id": route_id})

    item = response.get("Item")
    if item is None:
        raise ToolError(f"No route found with route_id {route_id!r}.")

    return {"route": item}


TOOLS = {
    "list_shipments": list_shipments,
    "get_shipment": get_shipment,
    "get_route": get_route,
}


def lambda_handler(event, context):
    return dispatch(event, context, TOOLS)
