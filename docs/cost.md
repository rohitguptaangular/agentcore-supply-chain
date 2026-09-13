# Cost

Rates are us-east-1, correct as of September 2026. Check the pricing pages
before trusting them for anything that matters.

## The short version

**On the defaults, nothing in this stack bills by the hour.** A day of building
costs a couple of dollars, essentially all of it model tokens, and leaving it
deployed overnight costs pennies.

That is entirely down to `VECTOR_STORE=s3`. Two flags can change it.

## What bills by the hour, and only if you ask for it

| Flag | Resource | Rate | 8 hours | 30 days |
|---|---|---|---|---|
| *(default)* | S3 Vectors | storage + requests, no hourly charge | ~$0 | ~$0 |
| `VECTOR_STORE=opensearch` | OpenSearch Serverless, classic, 2 OCU floor | ~$0.24/OCU-hr | $3.84 | ~$350 |
| | Same with default redundancy (4 OCU) | | $7.68 | ~$700 |
| `ENABLE_VPC=true` | NAT gateway | $0.045/hr + $0.045/GB | $0.40 | ~$33 |
| | 4 interface endpoints × 2 AZs | $0.01/hr per ENI | $0.64 | ~$58 |

S3 Vectors is priced on stored vectors and requests rather than provisioned
compute. Four markdown documents is a rounding error — well under a cent a
month — and AWS rates the index at up to 2 billion vectors, so this stays cheap
long past the point this demo stops being a demo.

The trade-off is query latency: AWS documents S3 Vectors at 100ms or more
against OpenSearch's single-digit milliseconds. Since every answer also
involves a model call taking several seconds, it isn't noticeable here. It
would matter for a high-QPS search product.

If you do switch to OpenSearch and your account offers **NextGen collections**,
use one — AWS documents no minimum OCU and scale-to-zero after ten minutes
idle. Classic collections hold the 2 OCU floor whether or not anything queries
them.

## What bills per request

| Service | Rate | Realistic day of testing |
|---|---|---|
| AgentCore Runtime | $0.0895/vCPU-hr + $0.00945/GB-hr, **only while a session is active** | a few cents |
| AgentCore Gateway | $0.005 per 1,000 invocations | <$0.01 |
| AgentCore Memory | $0.25/1k events, $0.75/1k records/month, $0.50/1k retrievals | <$0.10 |
| AgentCore Identity | no charge via Runtime or Gateway | $0 |
| Nova Pro | ~$0.80 / $3.20 per million in/out tokens | ~$0.20 |
| Nova Lite | ~$0.06 / $0.24 per million | pennies |
| Titan Embeddings V2 | $0.02 per million tokens | <$0.01 |
| Bedrock Guardrails | per text unit | pennies |
| Lambda, DynamoDB, API Gateway, S3, CloudFront | on-demand | <$0.50 combined |

The whole right-hand column adds up to well under a dollar for a day of use.

Worth noting that AgentCore Runtime bills CPU only while it's actually
computing — it doesn't charge CPU while waiting on a model or a tool. Memory is
billed for the life of the session.

## Keeping it cheap

**Stay on `VECTOR_STORE=s3`** unless you specifically want to demonstrate the
OpenSearch path. Switch to `opensearch` for an afternoon, take your
screenshots, switch back. Note that switching replaces the knowledge base, so
`make seed` has to run again afterwards.

**Leave `ENABLE_VPC=false` unless you're demonstrating the networking.** It's
~$0.13/hr for NAT and endpoints, and nothing else about the system behaves
differently. An hour of it costs 13 cents, which is a cheap screenshot.

**Use `ENABLE_KB=false` if you only care about the tool path.** Most debugging —
auth, gateway, tools, memory, guardrails — doesn't involve the knowledge base
at all.

**Destroy it when you stop anyway.** On the defaults you could leave it up, but
the habit is worth more than the pennies saved.

**Set a billing alarm before the first deploy.** $20 is a sensible tripwire for
this stack.

## Teardown order

`make destroy` handles all of this, but if you're removing things by hand,
delete in cost order:

1. OpenSearch Serverless collection — the overwhelming majority of the bill
2. NAT gateway, then the interface endpoints
3. Agent runtimes, gateway and targets, memory resource
4. Knowledge base, guardrail
5. Lambdas, DynamoDB tables, API Gateway, CloudFront, S3 buckets, Cognito

Buckets must be empty before CloudFormation will delete them. That's the
failure people hit.

The artifacts bucket created by `make bootstrap` is deliberately **not**
deleted with the stack, so redeploying doesn't re-upload everything. It holds a
few megabytes. Remove it manually when you're finished for good.

## Checking what you've actually spent

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-09-01,End=2026-09-30 \
  --granularity DAILY \
  --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].Groups[?Metrics.UnblendedCost.Amount>`0.01`]'
```

Cost Explorer lags about 24 hours, so same-day spend won't show up.
