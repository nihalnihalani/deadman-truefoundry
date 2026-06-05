# DEADMAN — Deploy on TrueFoundry

## Overview

DEADMAN is deployed as a TrueFoundry **Service** component backed by the TFY AI Gateway
and MCP/Agent Gateway. The primary deploy artifact is `deadman-service.yaml`; the AI/MCP
configs in `infra/` are applied first with `tfy apply`.

```
infra/ai_gateway.yaml     — virtual model "deadman-resilient-bedrock" + fallback chain
infra/guardrails.yaml     — MCP scopes + pre/post-tool guardrails + agent autonomy budget
deploy/truefoundry/deadman-service.yaml  — the Service itself (this dir)
```

---

## Prerequisites

| Item | Details |
|---|---|
| `tfy` CLI | `pip install truefoundry` — authenticate with `tfy login` |
| Docker registry | Push the image to a registry reachable by TFY |
| TFY workspace | Note the workspace FQN (shown in TFY console Settings) |
| AWS creds or IRSA | For Bedrock + DynamoDB; see *IAM* section below |
| TFY Secrets created | See *Secrets* section below |

---

## Step 1 — Apply AI/MCP Gateway configs

These define the virtual model and guardrails that DEADMAN depends on.
Apply them **before** deploying the Service, or the readiness check will fail.

```bash
tfy apply -f infra/ai_gateway.yaml
tfy apply -f infra/guardrails.yaml
```

Verify the virtual model is active in the TFY console under
**AI Gateway → Models → deadman-resilient-bedrock**.

---

## Step 2 — Build and push the Docker image

```bash
IMAGE=<your-registry>/<workspace>/deadman:$(git rev-parse --short HEAD)
docker build -t $IMAGE .
docker push $IMAGE
```

Then edit `deadman-service.yaml` and set `image.image_uri` to `$IMAGE`.

Alternatively, switch to the inline build spec (Option B in the YAML comments) to have
TFY trigger builds on every push.

---

## Step 3 — Create TFY Secrets

DEADMAN requires the following secrets. Create them in the TFY console
(**Secrets → New Secret**) or via the CLI:

```bash
# TrueFoundry AI Gateway
tfy secret create --name deadman-tfy-api-key       --value "<TFY API key>"
tfy secret create --name deadman-tfy-gateway-url   --value "https://gateway.truefoundry.ai"
tfy secret create --name deadman-mcp-gateway-url   --value "https://gateway.truefoundry.ai/mcp/<mcp-server-name>/server"

# Webhook security (generate a random 32-byte hex secret)
tfy secret create --name deadman-webhook-secret    --value "$(openssl rand -hex 32)"

# AWS credentials (omit if using IRSA — see IAM section)
tfy secret create --name deadman-aws-access-key-id --value "<AWS_ACCESS_KEY_ID>"
tfy secret create --name deadman-aws-secret-key    --value "<AWS_SECRET_ACCESS_KEY>"

# OTel endpoint (optional — skip if you don't use OTel)
tfy secret create --name deadman-otel-endpoint     --value "https://<otel-collector>:4317"
```

> **Secret Group alternative**: create a TFY Secret Group named `deadman` with all
> the above as keys. Then replace the individual `value_from` blocks in the YAML with
> a single `secretFrom: deadman`. Syntax: `tfy explain secretFrom` for your tenant.

---

## Step 4 — Edit the service YAML

In `deadman-service.yaml`:

1. Set `workspace` and `project` FQN (uncomment lines near the top).
2. Set `image.image_uri` to the image pushed in Step 2.
3. Confirm `DEADMAN_DYNAMODB_TABLE` matches the table created by Terraform.
4. Review `replicas.min_replicas` — set to 2 for high-availability production.

---

## Step 5 — Apply the Service

```bash
tfy apply -f deploy/truefoundry/deadman-service.yaml
```

Watch rollout:

```bash
tfy service status deadman
tfy service logs deadman --follow
```

Verify the probes are green:

```bash
# Replace <url> with the TFY-assigned ingress URL shown in the console
curl https://<url>/healthz    # → {"ok":true, ...}
curl https://<url>/readyz     # → {"ok":true, ...}  (errors:[] means fully ready)
```

---

## Environment Variables — Full Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DEADMAN_MODE` | yes | `real` | Must be `real` in production |
| `DEADMAN_STATE_BACKEND` | yes | `dynamodb` | `dynamodb` for production |
| `DEADMAN_DYNAMODB_TABLE` | yes | `deadman-incident-state` | DynamoDB table name |
| `TFY_API_KEY` | yes (real mode) | — | TFY API key (secret) |
| `TFY_GATEWAY_BASE_URL` | yes (real mode) | — | AI Gateway base URL (secret) |
| `TFY_MCP_GATEWAY_URL` | yes (real mode) | — | MCP Gateway URL (secret) |
| `TFY_MCP_TRANSPORT` | no | `auto` | `auto`, `mcp`, or `rest`; use `auto` for TFY MCP server URLs |
| `TFY_RESILIENT_MODEL` | no | `deadman-resilient-bedrock` | Virtual model name |
| `TFY_METADATA` | no | `app=deadman,role=incident-commander` | Gateway tracing tags |
| `DEADMAN_WEBHOOK_SECRET` | yes (real mode) | — | HMAC-SHA256 signing secret |
| `DEADMAN_ENABLE_DEMO` | no | `0` | Set `1` only for stage demos |
| `AWS_REGION` | no | `us-east-1` | Primary Bedrock/DynamoDB region |
| `AWS_FALLBACK_REGION` | no | `us-west-2` | Cross-region fallback |
| `AWS_ACCESS_KEY_ID` | if no IRSA | — | Static AWS creds (prefer IRSA) |
| `AWS_SECRET_ACCESS_KEY` | if no IRSA | — | Static AWS creds (prefer IRSA) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | `""` | OTLP endpoint; empty = OTel off |
| `OTEL_SERVICE_NAME` | no | `deadman-incident-commander` | OTel service name |
| `DEADMAN_P99_BUDGET_MS` | no | `1500` | Latency budget before tier shed |
| `DEADMAN_REVOKE_AT_DEPTH` | no | `2` | Fallback depth for authority revoke |
| `DEADMAN_MIN_REPLICA_FLOOR` | no | `2` | Minimum ASG replicas guard |

---

## IAM: IRSA (recommended) vs static AWS keys

**IRSA (preferred)**: annotate the pod's service account with the IAM role ARN output
by Terraform (`terraform output iam_role_arn`). Skip the `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` secrets entirely.

```yaml
# In deadman-service.yaml, add under the top-level spec:
service_account_annotations:
  eks.amazonaws.com/role-arn: "arn:aws:iam::<account>:role/deadman-role"
```

**Static keys**: create the two secrets shown in Step 3. Rotate them regularly.
The IAM policy/role in `terraform/` grants least-privilege access to DynamoDB and Bedrock.

---

## Scaling

| Environment | `min_replicas` | `max_replicas` | Notes |
|---|---|---|---|
| Dev / demo | 1 | 2 | File backend OK; DynamoDB optional |
| Production | 2 | 5 | `dynamodb` backend required for HA |
| High-load | 3 | 10 | Profile p99 first; agent is I/O-bound |

DynamoDB `PAY_PER_REQUEST` billing auto-scales with traffic. No table changes needed.

---

## Rolling updates

`tfy apply` performs a rolling update. DEADMAN's graceful shutdown (SIGTERM drain,
`termination_grace_period_seconds: 60`) ensures in-flight incidents are not dropped.
The exactly-once DynamoDB spine means a re-queued webhook after a pod restart will
see the existing committed state and not re-execute tools.

---

## Infra relationship

```
┌──────────────────────────────────────────────────────┐
│  TrueFoundry Platform                                │
│  ┌──────────────────────┐  ┌────────────────────┐   │
│  │  AI Gateway          │  │  MCP/Agent Gateway  │   │
│  │  deadman-resilient-  │  │  Cedar DENY default │   │
│  │  bedrock (5-tier)    │  │  + guardrails       │   │
│  └──────────┬───────────┘  └────────┬───────────┘   │
│             │                        │               │
│  ┌──────────▼────────────────────────▼──────────┐   │
│  │  DEADMAN Service  (deadman-service.yaml)      │   │
│  │  port 8080 · uvicorn · non-root               │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│  AWS (terraform/)                                    │
│  DynamoDB  deadman-incident-state  (PK/SK + GSI)     │
│  IAM role  deadman-role  (least-priv: dynamo+bedrock)│
└──────────────────────────────────────────────────────┘
```
