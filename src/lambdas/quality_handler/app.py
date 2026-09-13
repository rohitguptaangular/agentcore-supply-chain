"""Quality domain tools.

Serves three tools declared in schemas/quality_tools.json:
    check_quality(product_id | batch_id)   -> inspection records
    get_compliance(entity_id, entity_type?) -> certifications and audit status
    get_standards(product_category)         -> tolerances and governing spec

This handler owns three tables. Every tool here requires an argument — there is
deliberately no "list every inspection ever" tool, because that is not a
question anyone usefully asks and it would return an unbounded result set to
the model.
"""

from __future__ import annotations

from typing import Any, Mapping

from boto3.dynamodb.conditions import Attr, Key

from agentcore_tools import ToolError, dispatch, require, table


def check_quality(args: Mapping[str, Any]) -> dict:
    """Return inspection records for a product or a batch.

    product_id is served by the product-index GSI, which is why that index
    exists: inspections are keyed by inspection_id, but nobody ever knows an
    inspection id — they know the product.

    batch_id falls back to a filtered scan. Add a second GSI if batch lookups
    ever become a hot path.
    """
    inspections = table("INSPECTION_TABLE")
    product_id = args.get("product_id")
    batch_id = args.get("batch_id")

    if not product_id and not batch_id:
        raise ToolError(
            "Provide either a product_id or a batch_id to look up inspections."
        )

    if product_id:
        response = inspections.query(
            IndexName="product-index",
            KeyConditionExpression=Key("product_id").eq(product_id),
        )
        subject = {"product_id": product_id}
    else:
        response = inspections.scan(FilterExpression=Attr("batch_id").eq(batch_id))
        subject = {"batch_id": batch_id}

    items = response.get("Items", [])
    if not items:
        raise ToolError(f"No inspection records found for {subject}.")

    # Most recent first — "recent inspections" is the common question.
    items.sort(key=lambda item: item.get("inspection_date", ""), reverse=True)

    return {**subject, "count": len(items), "inspections": items}


def get_compliance(args: Mapping[str, Any]) -> dict:
    """Return the compliance record for a supplier, product or facility."""
    entity_id = require(args, "entity_id")
    entity_type = args.get("entity_type")

    response = table("COMPLIANCE_TABLE").get_item(Key={"entity_id": entity_id})
    item = response.get("Item")

    if item is None:
        raise ToolError(f"No compliance record found for entity_id {entity_id!r}.")

    # entity_type is optional in the schema, so treat a mismatch as a warning
    # the model can relay rather than a hard failure.
    if entity_type and item.get("entity_type") != entity_type.upper():
        return {
            "compliance": item,
            "warning": (
                f"Requested entity_type {entity_type!r} but {entity_id!r} is "
                f"recorded as {item.get('entity_type')!r}."
            ),
        }

    return {"compliance": item}


def get_standards(args: Mapping[str, Any]) -> dict:
    """Return the quality standards governing a product category."""
    product_category = require(args, "product_category")

    response = table("STANDARDS_TABLE").get_item(
        Key={"product_category": product_category}
    )
    item = response.get("Item")

    if item is None:
        # List what does exist so the model can suggest a valid category
        # instead of dead-ending the conversation.
        available = [
            row.get("product_category")
            for row in table("STANDARDS_TABLE").scan().get("Items", [])
        ]
        raise ToolError(
            f"No standards recorded for category {product_category!r}. "
            f"Known categories: {', '.join(sorted(filter(None, available))) or 'none'}."
        )

    return {"standards": item}


TOOLS = {
    "check_quality": check_quality,
    "get_compliance": get_compliance,
    "get_standards": get_standards,
}


def lambda_handler(event, context):
    return dispatch(event, context, TOOLS)
