# Supply Chain Assistant on Amazon Bedrock AgentCore

A chat assistant that answers questions about a fictional industrial parts
distributor — stock levels, supplier records, shipments, quality inspections —
and also answers "how do we do X" from the company's written procedures.

The point of the project isn't the chatbot. It's everything an agent needs
around it before you'd let it near a real business: machine-to-machine auth on
every hop, tools it discovers at runtime rather than having hardcoded, a
retrieval pipeline over documents, memory that survives across sessions,
content filtering on the way out, and the option to run the whole thing inside
a VPC.

It deploys with one CloudFormation stack.

```
make bootstrap package deploy seed frontend
```

![The assistant answering a reorder question](docs/images/chat-ui.png)

The sidebar reports what was actually deployed rather than what the UI assumes
exists — VPC is off there because that deployment was run without it.

## Architecture

![Architecture](docs/images/architecture.svg)

Full walkthrough in [docs/architecture.md](docs/architecture.md).

Two agents, not one:

- **Orchestrator** (Nova Pro) decides what to do. It discovers tools from an
  AgentCore Gateway over MCP, reads and writes memory, and runs its model calls
  through a guardrail.
- **KB specialist** (Nova Lite) does one thing: search the document store and
  summarise with citations. The orchestrator reaches it over agent-to-agent
  invocation, and to the model it looks like any other tool.

Splitting them means retrieval runs on a model about an order of magnitude
cheaper, and I can change how retrieval works without redeploying the
orchestrator.

Behind the gateway there are four Lambda functions, one per domain
(inventory, supplier, logistics, quality), each with read access to only its
own DynamoDB tables. The knowledge base embeds documents with Titan V2 and
stores the vectors in S3 Vectors by default, or OpenSearch Serverless behind a
flag.

## The bit I found most interesting

Tools aren't compiled into the agent. On every invocation it calls `ListTools`
against the gateway and builds its toolset from whatever comes back:

```python
with client:
    discovered = client.list_tools_sync()
    agent = Agent(model=..., tools=list(discovered) + local_tools, ...)
```

Add a gateway target tomorrow and the agent can use it with no code change and
no redeploy. The tool list is data, not code.

The corollary bit me while I was learning this: a gateway target with no tool
schema is created successfully, reports READY, and exposes nothing at all. The
Lambda is the capability, the schema is the advertisement, and without the
advertisement the agent has no idea the capability exists.

## Deploying

You need the AWS CLI configured, Python 3.11+, and model access enabled for
Nova Pro, Nova Lite and Titan Text Embeddings V2 in your region.

```bash
make bootstrap      # S3 bucket for agent zips, tool schemas, packaged templates
make package        # zip both agents, upload them and the schemas
make deploy         # the whole stack, about 15 minutes the first time
make seed           # dummy data into DynamoDB, documents into the KB, then index them
make frontend       # generate config.js from stack outputs, publish the UI
make url            # prints the CloudFront URL
```

`make deploy` takes a while on a cold account, mostly waiting on the OpenSearch
collection and the two agent runtimes.

### Cost

Almost everything here is per-request and effectively free at demo scale — the
agent runtimes, gateway, memory, Lambdas, DynamoDB and the models together cost
well under a dollar for a day of poking at it.

On the defaults, **nothing bills by the hour at all**, because the knowledge
base stores its embeddings in S3 Vectors rather than OpenSearch. A full day of
building costs a couple of dollars, and leaving the stack up overnight costs
pennies.

Two flags turn on the expensive parts:

```bash
make deploy VECTOR_STORE=opensearch   # OpenSearch Serverless instead of S3 Vectors
make deploy ENABLE_VPC=true           # private subnets + PrivateLink
make deploy ENABLE_KB=false           # no knowledge base at all
```

| Flag | Adds |
|---|---|
| `VECTOR_STORE=opensearch` | ~$0.48/hr — a classic collection holds a 2 OCU floor, about $350/month if you forget |
| `ENABLE_VPC=true` | ~$0.13/hr — NAT gateway plus eight endpoint ENIs |

Both vector stores give the same knowledge base behaviour. S3 Vectors queries
at ~100ms against OpenSearch's single-digit milliseconds, which is invisible
next to a model call of several seconds. OpenSearch is there because it's what
most enterprise RAG actually runs on, and because the template that sets it up
is more interesting.

**Run `make destroy` when you're done anyway.** I set a $20 billing alarm before
the first deploy and I'd suggest the same. Details in [docs/cost.md](docs/cost.md).

## Repository layout

```
template.yaml         root stack — composes the nine below
infra/                one template per layer
  network.yaml          VPC, subnets, NAT, PrivateLink endpoints
  data.yaml             7 DynamoDB tables, KB document bucket
  auth.yaml             Cognito pool, resource server, M2M client
  tools.yaml            4 domain Lambdas, gateway, 4 targets
  knowledge-s3vectors.yaml  Bedrock KB over S3 Vectors (default)
  knowledge.yaml        Bedrock KB over OpenSearch Serverless
  guardrails.yaml       content, word, regex and PII policies
  agents.yaml           memory + both agent runtimes
  api.yaml              chat handler + HTTP API
  frontend.yaml         S3 + CloudFront
src/
  agents/               orchestrator and kb_specialist
  lambdas/              domain tools, chat handler, KB index custom resource
schemas/                the 12 tool definitions the agent discovers
seed/                   dummy data and the source documents
frontend/               chat UI, plain HTML and JS, no build step
scripts/                packaging, seeding, publishing
docs/
```

## Things worth knowing if you read the code

**There are two knowledge base templates and they're worth comparing.** The S3
Vectors one is about 130 lines: a vector bucket, an index, a role, the knowledge
base. The OpenSearch one is nearly twice that, because a collection needs an
encryption policy before it can be created, a network policy, a data access
policy naming every principal, and — since CloudFormation has no resource type
for an OpenSearch index while Bedrock requires one to already exist — a
Lambda-backed custom resource to create it over the collection's HTTP API.

That custom resource retries for two minutes, because the data access policy is
applied moments earlier and takes a few seconds to propagate. A 403 straight
after stack creation means "not allowed yet", not "not allowed", and telling
those apart is most of the work in anything eventually consistent.

**OpenSearch Serverless has two independent authorisation layers** and both
have to allow a call: the IAM policy, and the collection's data access policy.
IAM alone gets you a 403 that looks exactly like a missing permission. S3
Vectors has only IAM, which is one of the reasons that template is so much
shorter.

**Both runtimes use a JWT authorizer, so neither can be invoked with boto3.**
AWS documents that an OAuth-protected runtime has to be called over HTTPS
directly, since the SDK signs with SigV4 and a JWT authorizer won't accept it.
That's why `chat_handler` and `delegation.py` build the request by hand. The
alternative is leaving the specialist on IAM auth and using boto3 for that hop;
I preferred one identity model end to end.

**No client secret is stored anywhere.** Both the chat handler and the
orchestrator read it from Cognito at runtime with `DescribeUserPoolClient`.
Environment variables are readable by anyone with console access to the
resource.

**Tool errors are written for the model, not for a log file.** When a lookup
misses, the tool says what's valid:

```python
raise ToolError(f"No standards recorded for {product_category!r}. "
                f"Known categories: {', '.join(sorted(available))}.")
```

So the assistant offers the real options instead of dead-ending. It's a small
thing that changes how the whole system feels.

## Known limitations

- **30 second ceiling.** HTTP API caps integrations at 30s and a complex
  multi-tool turn can exceed it. The fix is response streaming through a Lambda
  Function URL; I kept API Gateway for CORS and throttling.
- **All tools are read-only.** Anything that mutates data needs approval
  workflows, idempotency and an audit trail, and none of that is here.
- **Single NAT gateway.** Production wants one per AZ. This doubles the hourly
  cost for redundancy nobody is testing in a demo.
- **Guardrail runs on DRAFT.** Convenient while iterating, wrong for
  production — a policy edit silently changes behaviour. The stack publishes a
  version too; switching is a one-line change.
- **Actor identity comes from the browser.** `actorId` is generated client-side
  and sits in localStorage, so memories are per-browser, not per-person. Real
  auth would derive it from the signed-in user.
- **Orchestrator can invoke any runtime in the account.** Scoping that IAM
  statement to the specialist's ARN creates a circular dependency between the
  two stacks. Fixable with a second pass; I left it wide and commented.
- **No tests.** The tool handlers are pure enough to unit test against a
  DynamoDB local or moto; I haven't written them yet.

## What deploying it actually taught me

I wrote the templates first, then built the same architecture by hand in the
console to check my understanding. That found **nine bugs**, none of which any
amount of re-reading the YAML would have caught. All are fixed here; all are
written up in [docs/console-walkthrough.md](docs/console-walkthrough.md).

The one I'd never have guessed:

**Amazon Nova cannot handle a hyphen in a tool name.** The AgentCore Gateway
names tools `<target>___<tool>`, so a target called `inventory-target` produces
`inventory-target___list_products`, and every turn dies with
`modelStreamErrorException: Model produced invalid sequence as part of ToolUse`.
Bedrock's own `toolSpec` schema accepts hyphens, so nothing rejects it up front.
I isolated it by calling `converse` directly with one tool at a time —
`inventory___list_products` works, `inventory-target___list_products` does not.
Gateway targets in this repo are named `inventory`, `supplier`, `logistics`,
`quality` for exactly that reason.

Three others worth the summary:

- **AgentCore does not install `requirements.txt`.** Dependencies must be
  vendored into the zip, resolved for linux/aarch64. A missing module shows up
  as `Runtime initialization time exceeded`, which points at the wrong problem
  entirely.
- **A model id starting `us.` is a cross-region inference profile.** IAM
  evaluates the *destination* ARN, so a policy scoped to one region fails with
  an AccessDenied naming a region you never asked for.
- **`except ClientError` was too narrow** around the memory write. botocore
  raises `ParamValidationError` before sending, so the exception escaped a
  handler whose only job was to stop memory failures costing the user their
  answer. A `try/except` naming too specific an exception can be worse than
  none, because it reads as protection that isn't there.

## Why I built it

I worked through an AWS lab where six parts of an architecture like this were
broken and had to be repaired in the console — a Cognito client with no secret,
a gateway target pointing at the wrong Lambda, a missing knowledge base, no
memory resource, no guardrail, and both runtimes sitting on public networking.

Fixing them taught me what each piece does. Rebuilding the whole thing as one
stack taught me something else: five of those six failures were a hand-typed
environment variable that was empty or wrong. In `infra/agents.yaml` they're
all `!Ref` and `!GetAtt` to the resources that produce them, so CloudFormation
won't create the orchestrator until every one of them exists and has a real
value.

Same architecture, same six failure modes, now structurally impossible.
