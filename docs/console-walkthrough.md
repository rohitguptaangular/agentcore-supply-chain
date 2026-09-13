# Building it by hand in the console

The stack deploys in one command, which is the point. But clicking it together
once is the fastest way to understand what those templates are actually doing,
and there are a few services here where the console shows you options the
template never mentions.

This is the same architecture, built in the order CloudFormation builds it.
Each section names the template it corresponds to, so you can read the two side
by side.

Expect two to three hours. Everything below is us-east-1; keep one region
throughout or nothing will find anything else.

> Cost warning: by the time you finish section 6 you have an OpenSearch
> Serverless collection running, which is about $0.48/hour on a classic
> collection. Don't start this and walk away. See [cost.md](cost.md).

## 0. Before you start

**Bedrock → Model access.** Enable Nova Pro, Nova Lite and Titan Text
Embeddings V2. Access is per-region and takes a minute to apply. Skipping this
produces an `AccessDeniedException` much later, when you're testing the agent
and looking in the wrong place.

Pick a short prefix and use it everywhere — the walkthrough assumes `sc`.

## 1. DynamoDB tables

*Template: `infra/data.yaml`*

DynamoDB → Tables → Create table, seven times:

| Table | Partition key |
|---|---|
| `sc-inventory` | `product_id` (String) |
| `sc-suppliers` | `supplier_id` (String) |
| `sc-shipments` | `shipment_id` (String) |
| `sc-routes` | `route_id` (String) |
| `sc-inspection` | `inspection_id` (String) |
| `sc-compliance` | `entity_id` (String) |
| `sc-standards` | `product_category` (String) |

Leave everything else default — on-demand billing means an idle table costs
nothing.

On `sc-inspection`, add a global secondary index: **Indexes → Create index**,
partition key `product_id`, name `product-index`, projection All.

That index exists because the quality tool looks up inspections *by product*.
Nobody knows an inspection id. The tool signature dictated the table design,
which is the right way round.

Load the data afterwards with `python3 scripts/seed.py`, or paste items by hand
from `seed/data/seed.json` if you want to see the item editor.

## 2. S3 buckets

*Template: `infra/data.yaml` and `infra/frontend.yaml`*

Two buckets, both with Block all public access left **on**:

- `sc-knowledge-<account>-us-east-1` — knowledge base source documents
- `sc-site-<account>-us-east-1` — the chat UI

Upload the four files from `seed/documents/` to the knowledge bucket.

A third bucket is useful for tool schemas; you can reuse the knowledge bucket
with a `schemas/` prefix if you'd rather not create another. Upload the four
files from `schemas/`.

## 3. Cognito

*Template: `infra/auth.yaml`*

This is the piece everything else depends on, so get it right first.

**Create user pool.** Cognito → User pools → Create. Since no human ever signs
in, the sign-in options barely matter — pick email and move on. Name it
`sc-pool`.

**Add a domain.** Inside the pool → Domain → Create Cognito domain. Any unique
prefix. Without a domain there is no `/oauth2/token` endpoint and
machine-to-machine auth is impossible.

**Create a resource server.** App integration → Resource servers → Create.

- Identifier: `supplychain`
- Scopes: `read` and `write`

The full scope names become `supplychain/read` and `supplychain/write`.

**Create the app client.** App clients → Create app client.

- Application type: **Machine-to-machine application**
- Name: `sc-m2m-client`
- Generate a client secret: **yes**

In the newer console the scopes are *not* on the creation screen. Open the
client afterwards, find the OAuth 2.0 settings (Login pages → Edit), confirm
**Client credentials** is the grant type, and tick both custom scopes. Save.

Missing that second step is the single most common way to end up with an app
client that looks correct and can't mint a usable token.

Write down: user pool id, client id, client secret, and the domain URL. Also
construct the discovery URL, which several later steps need:

```
https://cognito-idp.us-east-1.amazonaws.com/<pool-id>/.well-known/openid-configuration
```

**Test it now**, before building anything on top:

```bash
curl -s -X POST "https://<domain>/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "<client-id>:<client-secret>" \
  -d "grant_type=client_credentials&scope=supplychain/read supplychain/write"
```

You want an `access_token`. `invalid_scope` means the scopes weren't attached.

## 4. Domain Lambda functions

*Template: `infra/tools.yaml`*

Four functions, Python 3.13, arm64. For each: Lambda → Create function, author
from scratch, then paste the code from `src/lambdas/<name>_handler/app.py`.

The handlers import a shared module, so either create a layer from
`src/lambdas/common/` (zip it so `python/agentcore_tools.py` is at the root) or
paste that file alongside each handler.

| Function | Environment variables | Permissions |
|---|---|---|
| `sc-inventory-handler` | `INVENTORY_TABLE` | read `sc-inventory` |
| `sc-supplier-handler` | `SUPPLIERS_TABLE` | read `sc-suppliers` |
| `sc-logistics-handler` | `SHIPMENTS_TABLE`, `ROUTES_TABLE` | read both |
| `sc-quality-handler` | `INSPECTION_TABLE`, `COMPLIANCE_TABLE`, `STANDARDS_TABLE` | read all three |

Give each its own role with access to only its own tables. It's tempting to
make one role for all four; the whole point of splitting the functions is that
a problem in one domain can't reach another's data.

You can't usefully test these from the Lambda console, because they read the
tool name from the client context, which only the gateway sets. They'll return
a 400 saying exactly that if you try.

## 5. AgentCore Gateway and targets

*Template: `infra/tools.yaml`*

**Create the gateway.** Bedrock AgentCore → Gateways → Create.

- Name: `scgateway` (letters and digits only)
- Protocol: MCP
- Inbound auth: **JWT**, discovery URL from section 3, allowed client = your
  M2M client id
- Role: create one, or reuse a role that can `lambda:InvokeFunction` your four
  functions and `s3:GetObject` your schema location

Copy the **Gateway URL** when it's READY — it ends in `/mcp`.

**Add four targets.** Targets → Add target, once per domain:

| Name | Lambda | Schema |
|---|---|---|
| `inventory` | `sc-inventory-handler` | `inventory_tools.json` |
| `supplier` | `sc-supplier-handler` | `supplier_tools.json` |
| `logistics` | `sc-logistics-handler` | `logistics_tools.json` |
| `quality` | `sc-quality-handler` | `quality_tools.json` |

For each: target protocol **MCP target** (not "Customised target" — only MCP
offers a Lambda target type), target type Lambda, the function ARN, tool schema
from S3, credential provider **Gateway IAM role**.

Three things to watch, all of which produce a target that reports READY and
doesn't work:

- **No hyphens in target names.** The gateway names tools `<target>___<tool>`,
  and Nova cannot handle a hyphen in a tool name. `inventory-target` gives you
  `inventory-target___list_products`, which fails at runtime with
  `Model produced invalid sequence as part of ToolUse`. See bug 5 below.
- **A target with no schema exposes no tools.** The console lets you create one
  without a schema and shows it as healthy.
- **Check the ARN is the right function.** A logistics target pointing at the
  inventory Lambda returns inventory rows to shipment questions — plausible,
  wrong, and hard to spot in a chat transcript.

The S3 schema field is a picker, not a text box. Typing a URI into it leaves the
value uncommitted and the form reports "S3 location is required" while showing
your text. Use **Browse S3**, or switch to **Define an inline schema** and paste
the file contents.

## 6. Vector store and the knowledge base

*Templates: `infra/knowledge-s3vectors.yaml` (default) or `infra/knowledge.yaml`*

Pick one of the two. S3 Vectors is cheaper and quicker to click together;
OpenSearch is what most enterprise RAG runs on and is worth doing once.

### 6a. S3 Vectors (recommended)

**Create the vector bucket.** S3 → Vector buckets → Create vector bucket.

- Name: `sc-vectors-<account>`
- Encryption: default

A vector bucket isn't an ordinary S3 bucket. It has its own API namespace
(`s3vectors`), you can't browse objects in it, and it's addressed by ARN.

**Create the index.** Open the bucket → Vector indexes → Create index.

- Name: `sc-kb-index`
- Dimension: **1024**
- Distance metric: **Cosine**
- Data type: `float32`

1024 is not arbitrary — it's the output width of Titan Text Embeddings V2. Get
this wrong and the knowledge base creates fine and fails every sync.

Cosine compares direction rather than magnitude, so a long document doesn't
score higher just for being long. It's the normal choice for text.

Then skip to **"Create the knowledge base"** below.

### 6b. OpenSearch Serverless (the alternative)

**Create the collection.** OpenSearch Service → Serverless → Collections →
Create.

- Name: `sc-kb`
- Type: **Vector search**
- Security: easiest is to let it create the encryption and network policies

Wait for Active, then copy the collection ARN.

Note what the console just did for you that CloudFormation can't: it created
the encryption policy, the network policy, the data access policy, and it will
create the vector index during knowledge base creation. That last one is why
`infra/knowledge.yaml` needs a custom resource — CloudFormation has no resource
type for an OpenSearch index.

### Create the knowledge base

Bedrock → Knowledge Bases → Create. In the current console this is
**Create Managed KB → Self-managed KB → Unstructured Vector Store KB**. That's
the option that lets you bring your own vector store; "Managed KB" would create
its own and ignore what you just built.

- Name: `sc-kb`
- IAM: create a new service role, or use an existing one
- Data source: S3, your knowledge bucket
- Embeddings: **Titan Text Embeddings V2**
- Vector store: whichever you built — the S3 vector index, or the OpenSearch
  collection

On the OpenSearch path, if you let the console create the index, note the field
names it chose — you'll need them if you rebuild this in CloudFormation. If you
create the index yourself, use the mapping in
`src/lambdas/opensearch_index/app.py`: vector field `bedrock-kb-vector`,
dimension 1024, text field `AMAZON_BEDROCK_TEXT_CHUNK`, metadata
`AMAZON_BEDROCK_METADATA`.

Either way the dimension has to match the embedding model. Titan V2 is 1024. A
mismatch creates cleanly and fails every sync.

**Sync.** Select the data source → Sync. Wait for Completed and check it
scanned more than zero documents. Zero means the role can't read the bucket or
the prefix is wrong.

**Test it here**, on the knowledge base page, before building the agent that
uses it. Ask *"what is the receiving procedure?"* — you should get passages
from the inventory procedures document. If this doesn't work, nothing
downstream will, and you'll be debugging two layers at once.

If you find yourself hunting for which IAM role belongs to the knowledge base,
open the collection's **data access policy**. The KB role has to be a principal
there, so the policy is a reliable index of which role is which.

## 7. Guardrail

*Template: `infra/guardrails.yaml`*

Bedrock → Guardrails → Create.

- Blocked input and output messages: write something specific. This text is the
  assistant's voice when a policy trips, and a generic "blocked" makes a
  working system look broken.
- Content filters: Hate, Insults, Misconduct, Violence — all HIGH, both
  directions.
- Word filters: enable the profanity list, and add `Project Meridian` as a
  blocked phrase.
- Sensitive information: PII `USERNAME` → Mask, `PASSWORD` → Block. Add a regex
  named `discount-code`, pattern `DISC-[A-Z]{3}-\d{4}`, action Mask.

Copy the guardrail id. You can leave it on DRAFT for now.

The regex is the interesting one to demo: the agent can legitimately retrieve a
discount code from the promotional pricing document, and the guardrail masks it
on the way out. Retrieval permission and disclosure permission aren't the same
thing.

## 8. Memory

*Template: `infra/agents.yaml`*

Bedrock AgentCore → Memory → Create memory.

- Name: `sc_memory` — underscores only, hyphens are rejected
- Event expiry: 30 days
- Add three **built-in** strategies (not "with override", not "self-managed"):

| Type | Name | Namespace |
|---|---|---|
| Semantic | `semantic` | `sc/user/{actorId}/semantic` |
| User preference | `preferences` | `sc/user/{actorId}/preferences` |
| Summarization | `summary` | `sc/user/{actorId}/session/{sessionId}/summary` |

The namespace field comes pre-filled with a default. Delete it and paste the
value above. `{actorId}` and `{sessionId}` stay literal — they're substituted
at runtime, and that substitution is what keeps one user's memories away from
another's.

Creation takes several minutes. Copy the memory id when it's Active.

## 9. Agent runtimes

*Template: `infra/agents.yaml`*

Zip and upload the agent code first:

```bash
cd src/agents/kb_specialist && zip -r kb_specialist.zip . && \
  aws s3 cp kb_specialist.zip s3://<your-bucket>/agents/
cd ../orchestrator && zip -r orchestrator.zip . && \
  aws s3 cp orchestrator.zip s3://<your-bucket>/agents/
```

**KB specialist first**, because the orchestrator needs its ARN.

AgentCore → Runtime → Host agent:

- Name: `sc_kb_specialist`
- Source: S3, your zip, entry point `main.py`, runtime PYTHON_3_13, protocol
  HTTP
- Role: needs `bedrock:Retrieve` on the knowledge base and `bedrock:InvokeModel`
- Inbound auth: JWT, same discovery URL and client id as the gateway
- Environment: `MODEL_ID=us.amazon.nova-lite-v1:0`,
  `KNOWLEDGE_BASE_ID=<kb-id>`

Wait for Ready. Check the Endpoints table has one named `DEFAULT`; create it if
not. Copy the runtime ARN.

**Then the orchestrator**, same process:

- Name: `sc_orchestrator`
- Role: `bedrock:InvokeModel`, `bedrock:ApplyGuardrail`, the memory actions,
  `bedrock-agentcore:InvokeAgentRuntime`, and
  `cognito-idp:DescribeUserPoolClient` on your pool
- Environment:

| Key | Value |
|---|---|
| `MODEL_ID` | `us.amazon.nova-pro-v1:0` |
| `GATEWAY_URL` | from section 5 |
| `MEMORY_ID` | from section 8 |
| `MEMORY_NAMESPACE_PREFIX` | `sc` |
| `GUARDRAIL_ID` | from section 7 |
| `GUARDRAIL_VERSION` | `DRAFT` |
| `KB_SPECIALIST_RUNTIME_ARN` | from above |
| `COGNITO_DOMAIN` | full https URL |
| `COGNITO_CLIENT_ID` | M2M client id |
| `COGNITO_USER_POOL_ID` | pool id |

That table is the whole point of the exercise. Ten values typed by hand, each
one a chance to paste the wrong thing or leave a field empty. In
`infra/agents.yaml` every one of them is a `!Ref` or `!GetAtt` to the resource
that produced it.

Each runtime update creates a new version, and the DEFAULT endpoint takes a
moment to roll onto it. Invoking during that window returns
`No endpoint or agent found with qualifier 'DEFAULT'` — wait rather than
debugging the ARN.

## 10. Chat handler and API

*Template: `infra/api.yaml`*

Create `sc-chat-handler` from `src/lambdas/chat_handler/app.py`. Python 3.13,
timeout 29 seconds.

Environment: `ORCHESTRATOR_RUNTIME_ARN`, `COGNITO_DOMAIN`,
`COGNITO_CLIENT_ID`, `COGNITO_USER_POOL_ID`.

Permissions: `bedrock-agentcore:InvokeAgentRuntime` on the orchestrator, and
`cognito-idp:DescribeUserPoolClient` on the pool.

Then API Gateway → Create API → **HTTP API**:

- Integration: the chat handler Lambda
- Routes: `POST /chat` and `GET /health`
- CORS: allow origin `*` for now, header `content-type`, methods `POST` and
  `OPTIONS`

Copy the invoke URL and test:

```bash
curl -X POST "<api-url>/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is our current inventory?","session_id":"s1","actor_id":"a1"}'
```

This is the moment everything either works or doesn't. If it fails, the
handler's error messages name the likely cause — 404 means a version roll, 401
or 403 means the client isn't on the runtime's allowed list.

## 11. Frontend

*Template: `infra/frontend.yaml`*

Copy `frontend/config.example.js` to `frontend/config.js` and set
`apiEndpoint` to your API URL.

Upload `index.html`, `app.js`, `styles.css` and `config.js` to the site bucket.

CloudFront → Create distribution:

- Origin: the site bucket, origin access **Origin access control**, create a
  new OAC
- Default root object: `index.html`
- Viewer protocol policy: Redirect HTTP to HTTPS

CloudFront gives you a bucket policy to copy — apply it to the site bucket, or
the distribution gets 403s from its own origin.

Add custom error responses mapping 403 and 404 to `/index.html` with a 200, so
the page renders rather than CloudFront's XML error.

Wait for the distribution to deploy, then open the domain name.

## 12. VPC (optional)

*Template: `infra/network.yaml`*

Only worth doing if you specifically want to see PrivateLink work. It adds
about $0.13/hour.

Create a VPC with two public and two private subnets across two AZs, an
internet gateway, and a NAT gateway in one public subnet with the private route
table pointing `0.0.0.0/0` at it.

Make sure the VPC has both **DNS resolution** and **DNS hostnames** enabled.
Private DNS on the endpoints silently does nothing without them.

One security group, inbound 443 from the VPC CIDR, all outbound.

Then four interface endpoints — `bedrock-agentcore`,
`bedrock-agentcore.gateway`, `bedrock-runtime`, `logs` — each in both private
subnets, with that security group, and **Enable private DNS name** ticked. Plus
a gateway endpoint for S3 on the private route table.

Finally, update both runtimes: network mode Public → VPC, the two private
subnets, the same security group. Wait for both to return to Ready.

There's no Cognito endpoint because PrivateLink doesn't offer one. Token
traffic goes out through the NAT gateway, which is why it exists.

## 13. Tear it down

Delete in this order, most expensive first:

1. OpenSearch Serverless collection
2. NAT gateway, then the interface endpoints, then the VPC
3. Both agent runtimes, the gateway and its targets, the memory resource
4. Knowledge base, guardrail
5. Lambda functions, DynamoDB tables, API Gateway, CloudFront distribution
6. Empty and delete the buckets
7. Cognito user pool

CloudFront takes about 15 minutes to disable before it will delete.

## Nine things that only break when you deploy

I built this by hand after writing the templates, and it found nine bugs. All
are fixed in the repo now. They are listed here because every one of them looks
like something else at first, and because the templates would have hit the same
walls.

### 1. AgentCore does not install requirements.txt

Direct code deployment runs the zip as-is. A zip containing only source fails
with `ModuleNotFoundError: No module named 'bedrock_agentcore'` — and because a
container that crashes on import never finishes booting, the invocation returns
`Runtime initialization time exceeded. Please make sure that initialization
completes in 30s`. The error names the wrong problem entirely.

Dependencies must be vendored, resolved for the runtime's platform:

    pip install -r requirements.txt --target build/ \
      --platform manylinux2014_aarch64 --python-version 3.13 --only-binary=:all:

### 2. Do not strip *.dist-info

Trimming metadata directories to shrink the zip breaks any library that reads
its own version at import: `PackageNotFoundError: No package metadata was found
for httpx2`. Only `__pycache__` and `bin/` are safe to remove.

### 3. mcp 2.x renamed streamablehttp_client

The Strands docs example uses `from mcp.client.streamable_http import
streamablehttp_client`, which is mcp 1.x. In 2.x it is `streamable_http_client`.
The modern Strands API avoids the question entirely:

    MCPClient(url=GATEWAY_URL, headers={"Authorization": f"Bearer {token}"})

Both requirements.txt files are now pinned so an unrelated rebuild cannot move
underneath the code.

### 4. Cross-region inference profiles need wildcard region in IAM

A model id beginning `us.` is an inference profile, not a model. Bedrock routes
the call to whichever US region has capacity, and **IAM evaluates the
destination ARN**. A policy scoped to `arn:aws:bedrock:us-east-1::foundation-model/*`
produces:

    not authorized to perform: bedrock:InvokeModelWithResponseStream
    on resource: arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-pro-v1:0

Grant `arn:aws:bedrock:*::foundation-model/*` plus the regional
`inference-profile/*`.

### 5. Nova cannot handle a hyphen in a tool name

This is the one worth remembering. The gateway composes tool names as
`<target>___<tool>`, so a target called `inventory-target` produces
`inventory-target___list_products`, and Nova responds with:

    modelStreamErrorException: Model produced invalid sequence as part of ToolUse

Bedrock's own `toolSpec` schema accepts hyphens, so nothing rejects it up front.
Isolated by calling `converse` directly with one tool at a time:

| tool name | result |
|---|---|
| `list_products` | works |
| `inventory___list_products` | works |
| `inventorytarget___list_products` | works |
| `inventory-list_products` | fails |
| `inventory-target___list_products` | fails |

The triple underscore is fine. The hyphen is not. **Name gateway targets
without hyphens** — `inventory`, not `inventory-target`.

### 6. create_event requires eventTimestamp

`bedrock-agentcore:CreateEvent` rejects a call without it:
`Missing required parameter in input: "eventTimestamp"`. Pass a timezone-aware
datetime.

### 7. except ClientError was too narrow to protect the turn

The memory write is wrapped in a try/except precisely so a memory failure never
costs the user their answer. But botocore raises `ParamValidationError` *before
sending the request*, and that is not a subclass of `ClientError` — so bug 6
escaped the handler and destroyed a perfectly good response.

A `try/except` naming too specific an exception is sometimes worse than none,
because it reads as protection that is not there. Code that runs after the
answer already exists should catch `Exception`.

### 8. Runtime session ids must be at least 33 characters

    Value at 'runtimeSessionId' failed to satisfy constraint:
    Member must have length greater than or equal to 33

Browser-generated ids are shorter than that. The chat handler now pads a short
id with a SHA-256 of itself — deterministic, so the same conversation always
maps to the same runtime session and context survives across turns.

### 9. CORS that works in curl and fails in every browser

The API had `AllowMethods` and `MaxAge` but no `AllowOrigins` or
`AllowHeaders`. Preflight returned `204` with no `Access-Control-Allow-Origin`
header, so browsers would block every request — while curl, which never sends
`Origin`, worked perfectly.

Always test preflight explicitly:

    curl -i -X OPTIONS "$API/chat" \
      -H "Origin: https://example.com" \
      -H "Access-Control-Request-Method: POST" \
      -H "Access-Control-Request-Headers: content-type"

### Two more worth knowing

**AgentCore creates the DEFAULT endpoint for you.** Declaring an
`AWS::BedrockAgentCore::RuntimeEndpoint` named DEFAULT collides with the
managed one.

**MCP needs a protocol version header.** Calls after `initialize` default to
`2025-03-26`, which the gateway rejects. Send
`MCP-Protocol-Version: 2025-11-25`. The Strands client handles this; anything
hand-rolled does not.

## What this exercise is for

Having done it by hand, the templates read differently. `infra/agents.yaml`
isn't an abstract configuration file any more — it's section 9, with the ten
environment variables resolved automatically instead of copied between browser
tabs.

The failure modes are worth remembering too, because none of them announce
themselves: a gateway target with no schema, a knowledge base index whose field
names don't match, a memory namespace left on its default, a runtime invoked
while its endpoint is mid-roll. Every one looks healthy in the console.
