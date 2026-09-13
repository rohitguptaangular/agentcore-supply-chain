"""Inventory domain tools.

Serves three tools declared in schemas/inventory_tools.json:
    list_products(category?)  -> everything in stock, optionally filtered
    get_product(product_id)   -> one product
    check_reorder()           -> products at or below their reorder point

The gateway invokes this function once per tool call and names the tool in the
Lambda client context, so the module exposes a TOOLS mapping and lets the
shared dispatcher route the call.
"""

from __future__ import annotations

from typing import Any, Mapping

from boto3.dynamodb.conditions import Attr

from agentcore_tools import ToolError, dispatch, require, table


def list_products(args: Mapping[str, Any]) -> dict:
    """Return every product, optionally narrowed to one category.

    A scan is the right call here: the demo dataset is a handful of rows and
    the agent genuinely wants "everything". On a real catalogue this would be
    a paginated query against a category index.
    """
    inventory = table("INVENTORY_TABLE")
    category = args.get("category")

    if category:
        response = inventory.scan(FilterExpression=Attr("category").eq(category))
    else:
        response = inventory.scan()

    items = response.get("Items", [])
    return {
        "count": len(items),
        "products": items,
    }


def get_product(args: Mapping[str, Any]) -> dict:
    """Return a single product by key."""
    product_id = require(args, "product_id")
    response = table("INVENTORY_TABLE").get_item(Key={"product_id": product_id})

    item = response.get("Item")
    if item is None:
        # Raised rather than returned as null: the model needs to be told the
        # id was wrong so it can ask the user, instead of reporting "no stock".
        raise ToolError(f"No product found with product_id {product_id!r}.")

    return {"product": item}


def check_reorder(_args: Mapping[str, Any]) -> dict:
    """Return products whose quantity has fallen to or below the reorder point.

    DynamoDB cannot compare two attributes of the same item server-side, so the
    comparison happens here. Fine at demo scale; a production system would
    maintain a `needs_reorder` flag written at update time and query an index.
    """
    items = table("INVENTORY_TABLE").scan().get("Items", [])

    low_stock = [
        item
        for item in items
        if item.get("quantity") is not None
        and item.get("reorder_point") is not None
        and item["quantity"] <= item["reorder_point"]
    ]

    return {
        "count": len(low_stock),
        "products": low_stock,
        "note": "Products at or below their reorder point.",
    }


TOOLS = {
    "list_products": list_products,
    "get_product": get_product,
    "check_reorder": check_reorder,
}


def lambda_handler(event, context):
    return dispatch(event, context, TOOLS)
