"""CloudFormation custom resource that creates the knowledge base vector index.

Why this exists
---------------
Amazon Bedrock Knowledge Bases require a vector index to already exist in the
OpenSearch Serverless collection they are pointed at. CloudFormation can create
the *collection* (AWS::OpenSearchServerless::Collection) but has no resource
type for an *index*, because indexes are created over the collection's own HTTP
API rather than the AWS control plane. This Lambda closes that gap.

Contract
--------
Properties supplied by the template:
    CollectionEndpoint  https://<id>.<region>.aoss.amazonaws.com
    IndexName           name of the index to create
    VectorField         field holding the embedding
    TextField           field holding the chunk text
    MetadataField       field holding Bedrock's chunk metadata
    Dimension           embedding width (1024 for Titan Text Embeddings V2)

Every code path must POST a response to event["ResponseURL"]. A custom resource
that stays silent leaves the stack waiting for an hour before it fails, so the
top-level handler catches everything and reports FAILED rather than raising.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

SERVICE = "aoss"

# The data access policy is applied moments before this function runs and
# OpenSearch Serverless takes a little while to honour it, so a 403 immediately
# after stack creation is expected rather than fatal.
MAX_ATTEMPTS = 12
RETRY_SECONDS = 10


def lambda_handler(event: dict, context) -> None:
    LOG.info("Received %s", json.dumps(event))
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId") or f"index-{props.get('IndexName')}"

    try:
        if request_type == "Create":
            _create_index(props)
        elif request_type == "Update":
            # Index settings are immutable in place. Changing the index name or
            # dimension means a new index, which the template models as a
            # replacement, so there is nothing to do on an in-place update.
            LOG.info("Update is a no-op; index configuration is immutable.")
        elif request_type == "Delete":
            # Deliberately left alone. The collection is deleted by its own
            # CloudFormation resource, which removes the index with it, and
            # failing a delete here would block the whole stack teardown.
            LOG.info("Delete is a no-op; the collection deletion removes the index.")

        _respond(event, context, "SUCCESS", physical_id)

    except Exception as exc:  # noqa: BLE001 - never let the stack hang
        LOG.exception("Custom resource failed")
        _respond(event, context, "FAILED", physical_id, reason=str(exc))


def _create_index(props: dict[str, Any]) -> None:
    """PUT the index definition, retrying while permissions propagate."""
    endpoint = props["CollectionEndpoint"].rstrip("/")
    index_name = props["IndexName"]
    url = f"{endpoint}/{index_name}"

    body = {
        "settings": {
            # Enables approximate k-nearest-neighbour search on this index.
            "index.knn": True,
        },
        "mappings": {
            "properties": {
                props["VectorField"]: {
                    "type": "knn_vector",
                    "dimension": int(props["Dimension"]),
                    "method": {
                        # HNSW on FAISS is what Bedrock's own quick-create uses.
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                    },
                },
                props["TextField"]: {"type": "text"},
                # Metadata is stored and returned but never searched, so leaving
                # it unindexed keeps the index smaller.
                props["MetadataField"]: {"type": "text", "index": False},
            }
        },
    }

    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status, payload = _signed_request("PUT", url, body)

            if status in (200, 201):
                LOG.info("Created index %s", index_name)
                _wait_for_index_visibility(endpoint, index_name)
                return

            # Someone already made it — a stack retry, or a manual creation.
            if "resource_already_exists_exception" in payload:
                LOG.info("Index %s already exists; treating as success", index_name)
                return

            last_error = RuntimeError(f"HTTP {status}: {payload}")
            LOG.warning("Attempt %s/%s failed: %s", attempt, MAX_ATTEMPTS, last_error)

        except Exception as exc:  # noqa: BLE001 - retry transient auth failures
            last_error = exc
            LOG.warning("Attempt %s/%s raised: %s", attempt, MAX_ATTEMPTS, exc)

        time.sleep(RETRY_SECONDS)

    raise RuntimeError(f"Could not create index {index_name}: {last_error}")


def _wait_for_index_visibility(endpoint: str, index_name: str) -> None:
    """Block until the new index answers a HEAD-style request.

    Bedrock rejects a knowledge base whose index is not yet queryable, and the
    index becomes visible a few seconds after the PUT returns.
    """
    for _ in range(MAX_ATTEMPTS):
        status, _ = _signed_request("GET", f"{endpoint}/{index_name}", None)
        if status == 200:
            LOG.info("Index %s is queryable", index_name)
            return
        time.sleep(RETRY_SECONDS)

    LOG.warning("Index %s not visible yet; continuing anyway", index_name)


def _signed_request(method: str, url: str, body: dict | None) -> tuple[int, str]:
    """Issue a SigV4-signed request to OpenSearch Serverless.

    OpenSearch Serverless has no username/password — every call is signed with
    this Lambda's IAM credentials, and the collection's data access policy
    decides what that identity is allowed to do.
    """
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    region = session.region_name

    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}

    request = AWSRequest(method=method, url=url, data=payload, headers=headers)
    SigV4Auth(credentials, SERVICE, region).add_auth(request)

    urllib_request = urllib.request.Request(
        url,
        data=payload.encode() if payload else None,
        headers=dict(request.headers),
        method=method,
    )

    try:
        with urllib.request.urlopen(urllib_request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _respond(event: dict, context, status: str, physical_id: str, reason: str = "") -> None:
    """POST the outcome to the pre-signed URL CloudFormation is waiting on."""
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch log stream {context.log_stream_name}",
            "PhysicalResourceId": physical_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": {},
        }
    ).encode()

    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        headers={"Content-Type": "", "Content-Length": str(len(body))},
        method="PUT",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        LOG.info("Reported %s to CloudFormation (%s)", status, response.status)
