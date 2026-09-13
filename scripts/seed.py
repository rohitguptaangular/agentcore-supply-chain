#!/usr/bin/env python3
"""Load demo data and index the knowledge base documents.

Three things happen here, and the third is the one people forget:

    1. write dummy rows into the seven DynamoDB tables
    2. upload the markdown documents to the knowledge base bucket
    3. start an ingestion job so those documents are chunked, embedded and
       written to the vector index

Uploading documents does nothing on its own. Until an ingestion job runs, the
knowledge base has a data source pointing at files it has never read. That is
exactly what the console's "Sync" button does.

Usage:
    # against a deployed stack, resolving everything from its outputs
    python3 scripts/seed.py --stack supplychain --region us-east-1

    # against resources built by hand in the console
    python3 scripts/seed.py --project sc --bucket sc-knowledge-123456789012-us-east-1 \\
        --knowledge-base-id ABCD1234 --data-source-id EFGH5678
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = ROOT / "seed" / "data" / "seed.json"
DOCUMENTS_DIR = ROOT / "seed" / "documents"

# Logical name in seed.json -> suffix of the DynamoDB table name.
TABLES = [
    "inventory",
    "suppliers",
    "shipments",
    "routes",
    "inspection",
    "compliance",
    "standards",
]

INGESTION_POLL_SECONDS = 15
INGESTION_MAX_WAIT_SECONDS = 900


def main() -> int:
    args = parse_args()

    if args.stack:
        cloudformation = boto3.client("cloudformation", region_name=args.region)
        outputs, parameters = describe_stack(cloudformation, args.stack)

        project = parameters.get("ProjectName", args.stack)
        bucket = outputs.get("KnowledgeBucket")
        knowledge_base_id = outputs.get("KnowledgeBaseId")
        data_source_id = outputs.get("DataSourceId")
        print(f"Stack {args.stack} (project {project}) in {args.region}\n")
    else:
        # Resources built by hand in the console rather than by CloudFormation.
        project = args.project
        bucket = args.bucket
        knowledge_base_id = args.knowledge_base_id
        data_source_id = args.data_source_id
        print(f"Project prefix {project} in {args.region}\n")

    seed_tables(args.region, project)

    if not bucket:
        print("\nNo knowledge bucket given — skipping documents. Done.")
        return 0

    upload_documents(args.region, bucket)

    if args.skip_ingestion:
        print("\n--skip-ingestion set. Run a sync from the console when ready.")
        return 0

    if not knowledge_base_id or not data_source_id:
        print(
            "\nNo knowledge base id or data source id given — documents are "
            "uploaded but not indexed. Sync from the Bedrock console, or rerun "
            "with --knowledge-base-id and --data-source-id."
        )
        return 0

    return start_ingestion(args.region, knowledge_base_id, data_source_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stack",
        help="CloudFormation stack to read resource names from. Omit when the "
        "resources were created by hand, and pass --project instead.",
    )
    parser.add_argument(
        "--project",
        help="Table name prefix, e.g. 'sc' for sc-inventory. Used without --stack.",
    )
    parser.add_argument("--bucket", help="Knowledge base document bucket.")
    parser.add_argument("--knowledge-base-id")
    parser.add_argument("--data-source-id")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--skip-ingestion",
        action="store_true",
        help="Upload documents but do not start an ingestion job.",
    )

    args = parser.parse_args()

    if not args.stack and not args.project:
        parser.error("pass either --stack or --project")

    return args


def describe_stack(client, stack: str) -> tuple[dict, dict]:
    try:
        described = client.describe_stacks(StackName=stack)["Stacks"][0]
    except ClientError as exc:
        sys.exit(f"Could not read stack {stack}: {exc}")

    outputs = {
        item["OutputKey"]: item["OutputValue"] for item in described.get("Outputs", [])
    }
    parameters = {
        item["ParameterKey"]: item["ParameterValue"]
        for item in described.get("Parameters", [])
    }
    return outputs, parameters


def seed_tables(region: str, project: str) -> None:
    """Write every table's rows with batch_writer.

    batch_writer batches requests and retries unprocessed items automatically,
    which is what you want when loading a few dozen rows.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)

    # parse_float=Decimal because DynamoDB rejects Python floats — it stores
    # numbers as exact decimals, and boto3 will not silently convert.
    data = json.loads(SEED_FILE.read_text(), parse_float=Decimal)

    for logical in TABLES:
        items = data.get(logical, [])
        if not items:
            print(f"  {logical}: nothing to load")
            continue

        table_name = f"{project}-{logical}"
        table = dynamodb.Table(table_name)

        try:
            with table.batch_writer() as batch:
                for item in items:
                    batch.put_item(Item=item)
        except ClientError as exc:
            sys.exit(f"Could not write to {table_name}: {exc}")

        print(f"  {table_name}: {len(items)} items")


def upload_documents(region: str, bucket: str) -> None:
    """Copy the markdown documents into the knowledge base bucket."""
    s3 = boto3.client("s3", region_name=region)
    documents = sorted(DOCUMENTS_DIR.glob("*.md"))

    if not documents:
        sys.exit(f"No documents found in {DOCUMENTS_DIR}")

    print(f"\nUploading {len(documents)} documents to s3://{bucket}/")
    for document in documents:
        s3.upload_file(
            str(document),
            bucket,
            document.name,
            ExtraArgs={"ContentType": "text/markdown"},
        )
        print(f"  {document.name}")


def start_ingestion(region: str, knowledge_base_id: str, data_source_id: str) -> int:
    """Kick off chunking and embedding, then wait for it to finish."""
    client = boto3.client("bedrock-agent", region_name=region)

    print(f"\nStarting ingestion job for knowledge base {knowledge_base_id}")
    response = client.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id
    )
    job_id = response["ingestionJob"]["ingestionJobId"]

    deadline = time.time() + INGESTION_MAX_WAIT_SECONDS

    while time.time() < deadline:
        job = client.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )["ingestionJob"]

        status = job["status"]
        print(f"  status: {status}")

        if status == "COMPLETE":
            stats = job.get("statistics", {})
            indexed = stats.get("numberOfDocumentsScanned", 0)
            failed = stats.get("numberOfDocumentsFailed", 0)
            print(f"\nIngestion complete. Scanned {indexed}, failed {failed}.")
            if indexed == 0:
                # Almost always a bucket prefix mismatch or a permissions gap
                # on the knowledge base role.
                print(
                    "WARNING: nothing was indexed. Check the data source prefix "
                    "and the knowledge base role's S3 permissions."
                )
                return 1
            return 0

        if status == "FAILED":
            print(f"\nIngestion failed: {job.get('failureReasons')}")
            return 1

        time.sleep(INGESTION_POLL_SECONDS)

    print("\nTimed out waiting for ingestion. It may still be running.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
