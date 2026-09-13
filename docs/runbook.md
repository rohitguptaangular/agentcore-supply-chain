# Runbook

Operating and debugging the stack.

## First deploy

```bash
make bootstrap package deploy seed frontend url
```

Roughly 15–20 minutes on a cold account. Most of it is the OpenSearch
collection and the two agent runtimes building from their zips.

Before you start, check model access is enabled in your region for Nova Pro,
Nova Lite and Titan Text Embeddings V2. Bedrock → Model access. Missing access
shows up as an `AccessDeniedException` on the first message, not at deploy
time, which is an annoying place to find out.

## Redeploying after a change

| Changed | Run |
|---|---|
| A template | `make deploy` |
| Lambda code | `make deploy` |
| Agent code | `make package deploy` |
| Tool schema | `make package deploy` |
| Seed data or documents | `make seed` |
| Frontend | `make frontend` |

Agent and schema changes need `package` first because those artifacts are
uploaded to S3 outside the CloudFormation packaging step.

## Switching the feature flags

```bash
make deploy ENABLE_VPC=true      # move both runtimes into the VPC
make deploy ENABLE_VPC=false     # back to public networking
make deploy ENABLE_KB=false      # tear down OpenSearch and the specialist
```

Moving runtimes between public and VPC mode takes several minutes each and
creates a new runtime version. Wait for both to report READY before testing, or
you'll get a 404 on the DEFAULT endpoint while it rolls.

Turning the knowledge base off deletes the collection and everything indexed in
it. Turning it back on requires `make seed` again.

## When something breaks

### The chat UI says the agent endpoint was not found

A runtime version is still rolling. Wait a minute and retry. If it persists,
check the runtime's endpoint:

```bash
aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id <id> --region us-east-1
```

### The chat UI says the agent rejected the request

The Cognito client isn't in the runtime's `allowedClients`, or the token is
missing scopes. Test the token on its own:

```bash
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name supplychain \
  --query 'Stacks[0].Outputs[?OutputKey==`MachineClientId`].OutputValue' --output text)
POOL_ID=$(aws cloudformation describe-stacks --stack-name supplychain \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)
DOMAIN=$(aws cloudformation describe-stacks --stack-name supplychain \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoDomain`].OutputValue' --output text)
SECRET=$(aws cognito-idp describe-user-pool-client --user-pool-id "$POOL_ID" \
  --client-id "$CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)

curl -s -X POST "$DOMAIN/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "$CLIENT_ID:$SECRET" \
  -d "grant_type=client_credentials&scope=supplychain/read supplychain/write"
```

- `access_token` back → Cognito is fine, problem is downstream
- `invalid_scope` → the scopes aren't attached to the app client
- `invalid_client` → wrong id or secret, or client_credentials isn't enabled

### The agent says it has no tools

Ask it directly:

```
List every tool you can call, grouped by domain. Do not call any of them.
```

If it lists nothing, tool discovery failed. Check `GATEWAY_URL` is set on the
orchestrator and that all four targets are READY:

```bash
aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier <gateway-id> --region us-east-1
```

A target that is READY but contributes no tools almost always has no schema
attached. That's the failure mode to check first — it looks healthy from every
angle except the tool list.

The orchestrator caches discovery per invocation, so after changing targets
start a new session in the UI rather than continuing an existing one.

### Document questions return nothing

Check the ingestion job actually indexed something:

```bash
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id <kb-id> --data-source-id <ds-id> --region us-east-1
```

`numberOfDocumentsScanned: 0` means the data source can't see the files —
usually a prefix mismatch or missing `s3:GetObject` on the knowledge base role.

To test retrieval without the agent in the way:

```bash
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <kb-id> \
  --retrieval-query '{"text":"receiving procedure"}' \
  --region us-east-1
```

If that returns passages, the knowledge base is healthy and the problem is in
the specialist or the delegation hop.

### Memory doesn't recall anything

First check whether it's a write problem or a read problem. Console → Bedrock
AgentCore → Memory → your memory → Observability:

- `Create events` at zero → the orchestrator isn't writing. Check `MEMORY_ID`
  and that the runtime is on the current version.
- Events but `New memory extracted` at zero → extraction hasn't run yet, or is
  rejecting the content. Wait, send a couple more turns, check again.
- Both non-zero but no recall → namespace mismatch between what's written and
  what's queried.

Extraction takes 30–60 seconds. Testing recall immediately after stating a
preference will fail even when everything is configured correctly. Send the
preference, send two more messages, wait a minute, then start a new session and
ask.

### Stack won't delete

Almost always a non-empty bucket. `make destroy` empties them first. By hand:

```bash
aws s3 rm s3://<bucket> --recursive
```

If the OpenSearch collection blocks deletion, check nothing else references it.

## Logs

```bash
make logs                                      # chat handler, tailed
aws logs tail /aws/lambda/supplychain-inventory-handler --follow
```

Agent runtime logs are under
`/aws/bedrock-agentcore/runtimes/<runtime-id>` — the Runtime console page links
straight to them from the endpoint row, which is quicker than finding the log
group by hand.

## Hardening before you show it to anyone

The deployed defaults are open in a couple of ways that are fine for a demo and
not otherwise:

**CORS is `*`.** Narrow it once you know the CloudFront domain:

```bash
make deploy ALLOWED_ORIGIN=https://d1234abcd.cloudfront.net
```

**The chat API is unauthenticated.** Anyone with the URL can spend your model
budget. Throttling caps the damage at 5 requests/second; a real deployment
would put a Cognito user pool authorizer in front of the route so a human has
to sign in.

**The guardrail runs on DRAFT.** Switch `GUARDRAIL_VERSION` on the orchestrator
to the published version number so policy changes are deliberate.
