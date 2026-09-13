"""Supplier domain tools.

Serves three tools declared in schemas/supplier_tools.json:
    list_suppliers(country?)             -> all suppliers, optionally by country
    get_supplier(supplier_id)            -> one supplier
    find_suppliers_for_product(product_id) -> who can supply this, best rated first
"""

from __future__ import annotations

from typing import Any, Mapping

from boto3.dynamodb.conditions import Attr

from agentcore_tools import ToolError, dispatch, require, table


def list_suppliers(args: Mapping[str, Any]) -> dict:
    """Return all suppliers, optionally filtered to one country."""
    suppliers = table("SUPPLIERS_TABLE")
    country = args.get("country")

    if country:
        response = suppliers.scan(FilterExpression=Attr("country").eq(country))
    else:
        response = suppliers.scan()

    items = response.get("Items", [])
    return {"count": len(items), "suppliers": items}


def get_supplier(args: Mapping[str, Any]) -> dict:
    """Return a single supplier by key."""
    supplier_id = require(args, "supplier_id")
    response = table("SUPPLIERS_TABLE").get_item(Key={"supplier_id": supplier_id})

    item = response.get("Item")
    if item is None:
        raise ToolError(f"No supplier found with supplier_id {supplier_id!r}.")

    return {"supplier": item}


def find_suppliers_for_product(args: Mapping[str, Any]) -> dict:
    """Return suppliers able to supply a product, best performance rating first.

    Each supplier record carries a `products` list. Sorting here rather than in
    the model keeps the ranking deterministic — the agent should not be the
    thing deciding what "best" means.
    """
    product_id = require(args, "product_id")

    items = table("SUPPLIERS_TABLE").scan().get("Items", [])
    matches = [item for item in items if product_id in (item.get("products") or [])]

    if not matches:
        raise ToolError(
            f"No supplier in the register lists {product_id!r} among the products "
            "they supply."
        )

    matches.sort(key=lambda item: item.get("performance_rating", 0), reverse=True)

    return {
        "product_id": product_id,
        "count": len(matches),
        "suppliers": matches,
        "note": "Ordered by performance rating, highest first.",
    }


TOOLS = {
    "list_suppliers": list_suppliers,
    "get_supplier": get_supplier,
    "find_suppliers_for_product": find_suppliers_for_product,
}


def lambda_handler(event, context):
    return dispatch(event, context, TOOLS)
