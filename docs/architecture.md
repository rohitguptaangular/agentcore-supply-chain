# Architecture

## The shape of it

```
Users ──► CloudFront ──► S3 (chat UI)
  │
  └─ POST /chat ──► API Gateway ──► Lambda (chat handler)
                                        │
                                        ├──► Cognito   (client_credentials → JWT)
                                        │
                                        └──► AgentCore Runtime: ORCHESTRATOR
                                                │
     ┌──────────────────┬─────────────────┬─────┴────────────┐
     ▼                  ▼                 ▼                  ▼
  Nova Pro         Guardrails      AgentCore Memory   AgentCore Gateway
                                                             │
     │ A2A                        ┌──────────┬───────────┬───┴──────┐
     ▼                            ▼          ▼           ▼          ▼
  KB SPECIALIST               inventory  supplier   logistics   quality
  (Nova Lite)                  Lambda     Lambda      Lambda     Lambda
     │                            │          │           │          │
     ▼                         DynamoDB   DynamoDB   DynamoDB×2 DynamoDB×3
  Bedrock Knowledge Base
     ├─ Titan Embeddings V2
     ├─ OpenSearch Serverless
     └─ S3 (source documents)
```

With `ENABLE_VPC=true`, both runtimes move into private subnets and reach
AgentCore, Bedrock and CloudWatch over PrivateLink. Cognito has no PrivateLink
service, so token traffic goes out through the NAT gateway.

## Two kinds of question

This is the thing that drove most of the design.

*"How many steel bolts do we have?"* is a row in a table. Exact key lookup,
current to the second, costs a fraction of a cent.

*"What's our receiving procedure?"* is three paragraphs in a document, and the
user's words don't match the document's words — they said "receiving
procedure", the manual says "goods inwards process". No keyword search finds
that. You need embeddings, so both question and documents become vectors and
you search by meaning.

Different storage, different retrieval, different cost. So there are two paths:

| | Structured | Unstructured |
|---|---|---|
| Stored in | DynamoDB | S3 → OpenSearch vectors |
| Retrieved by | exact key | semantic similarity |
| Answer | a value | passages with citations |
| Freshness | real time | as of last ingestion job |

Most demos build one and fall over on half of real questions.

The agent picks between them by reading tool descriptions. That's why the
delegation tool's docstring says what it is *not* for:

> Do not use it for current quantities, shipment status, supplier records or
> inspection results; those come from the inventory, logistics, supplier and
> quality tools.

The docstring is the routing logic.

## One request, end to end

1. Browser posts to `/chat`.
2. Chat handler exchanges the Cognito client id and secret for an access token
   (`client_credentials`, scopes `supplychain/read` and `supplychain/write`).
   The token is cached in the container until shortly before it expires.
3. It POSTs to the orchestrator's `/invocations` endpoint with
   `Authorization: Bearer`, qualifier `DEFAULT`.
4. The runtime validates the token against the Cognito discovery URL and checks
   the client is in `allowedClients`.
5. The orchestrator recalls memories for this actor, opens an MCP session to
   the gateway and calls `ListTools`.
6. Nova Pro sees the tool definitions and either answers or asks for a tool
   call. Each tool call is executed by the runtime, appended to the
   conversation, and the model is called again. Loop until it returns text.
7. Guardrails evaluate every model call, in both directions.
8. The turn is written to memory. Extraction into long-term records happens in
   the background, 30–60 seconds later.
9. Response goes back up the chain.

Each arrow in step 6 is a billed model call, which is why a single question can
take ten seconds or more, and why the 30 second API Gateway limit is a real
constraint rather than a theoretical one.

## Trust boundaries

| Hop | Proves what | How |
|---|---|---|
| Browser → API Gateway | nothing | public, CORS + throttling |
| Chat handler → Orchestrator | "I'm an authorised service" | Cognito M2M JWT |
| Orchestrator → Gateway | "I'm an authorised agent" | same JWT, validated again |
| Orchestrator → Specialist | same | same JWT |
| Gateway → Lambda | "I'm the gateway" | IAM role |
| Lambda → DynamoDB | "I'm this handler" | scoped IAM policy |

The credential type changes at the gateway: JWTs outside, IAM inside. AWS
credentials never pass through the agent, and the agent never holds anything
that would let it call a table directly.

## Why two agents

I could have given the orchestrator a `retrieve` tool and skipped the second
runtime. Three reasons I didn't:

**Cost.** Summarising retrieved passages doesn't need Nova Pro. Nova Lite is
roughly a thirteenth of the price and perfectly good at faithful restatement.

**Focus.** The specialist's prompt is entirely about not inventing things. The
orchestrator's is about choosing the right source. Those are different jobs and
they pull a single prompt in different directions.

**Deployment independence.** I can change chunk counts, prompts or the
retrieval model without touching the orchestrator.

Worth noting the specialist doesn't use an agent framework at all — it's
`boto3` and about 150 lines. It has no decisions to make: retrieve, then
summarise. An agent loop there would burn tokens arriving at the only possible
plan. Use a framework where there's agency; elsewhere write the function.

## AgentCore pieces, and what each actually does

**Runtime** — a serverless sandbox for agent code. Sessions can run for hours
(Lambda caps at 15 minutes), each gets its own microVM, and it has a built-in
JWT authorizer. You give it a zip, an entry point and environment variables.

Versions and endpoints matter: every update creates a version, and callers
invoke an *endpoint* that points at one. That indirection lets you deploy
without cutting traffic over — and it's why you'll occasionally see
`No endpoint or agent found with qualifier 'DEFAULT'` if you invoke during a
version roll. The chat handler turns that into a message saying as much.

**Gateway** — turns Lambdas into MCP tools. It handles discovery, protocol
translation, inbound JWT validation and outbound IAM. Each target binds one
Lambda to one tool schema, and the schema is what the agent discovers. A target
without one exposes nothing.

**Memory** — short-term is the raw event stream, written synchronously.
Long-term is what the strategies extract in the background: semantic facts,
user preferences, session summaries. Namespaces like
`supplychain/user/{actorId}/preferences` are both the addressing scheme and the
isolation model — `{actorId}` is substituted at read and write time, so one
user's memories can't be read by another's queries.

Extraction being asynchronous is a design property, not a bug. Nothing you say
is recallable in the same turn.

**Identity** — inbound and outbound auth. No extra charge when used through
Runtime or Gateway.

## Knowledge pipeline

Documents land in S3. An ingestion job chunks them, embeds each chunk with
Titan V2 (1024 dimensions) and writes vectors into the OpenSearch index. At
query time the question is embedded with the same model and the nearest chunks
come back with their source.

Three things have to agree or it fails in confusing ways:

- the vector field name in the index mapping and in the KB's `FieldMapping`
- the embedding dimension and the model (Titan V2 is 1024)
- the data source prefix and where documents actually are

The first two are single CloudFormation parameters feeding both places, so they
can't drift. The third is why `seed.py` warns loudly if an ingestion job scans
zero documents.

OpenSearch Serverless also has two independent authorisation layers and both
must allow a call: the IAM policy (`aoss:APIAccessAll`) and the collection's
data access policy naming principals and index permissions. IAM alone gets a
403, which is a memorable afternoon if you don't know that.

## Networking

With `ENABLE_VPC=true`:

- both runtimes get ENIs in private subnets
- interface endpoints for `bedrock-agentcore`, `bedrock-agentcore.gateway`,
  `bedrock-runtime` and `logs`
- a gateway endpoint for S3, which is free
- one NAT gateway for Cognito, which has no PrivateLink service

`PrivateDnsEnabled` is what makes this invisible to the code: it overrides the
public DNS name inside the VPC, so the same SDK call resolves to a private ENI.
The agents don't know they moved.

The VPC needs both `EnableDnsSupport` and `EnableDnsHostnames`. Without them
private DNS silently doesn't work and everything looks configured while traffic
still goes out to the internet.

## Deliberate trade-offs

Listed here so it's clear they were choices.

- **One NAT gateway, not one per AZ.** Halves the hourly cost, loses AZ
  redundancy.
- **Read-only tools.** Writes need approval flows, idempotency keys and audit
  logs. Out of scope.
- **`AllowFromPublic` on the OpenSearch network policy.** Still requires SigV4
  on every request. Locking it to a VPC endpoint is the production answer.
- **Guardrail on DRAFT.** Convenient while iterating, wrong for production.
- **Orchestrator may invoke any runtime in the account.** Scoping it to the
  specialist's ARN creates a circular stack dependency.
- **Scans instead of queries** in a couple of tool handlers. Correct at this
  data volume, wrong at ten thousand rows; noted in the code where it applies.
