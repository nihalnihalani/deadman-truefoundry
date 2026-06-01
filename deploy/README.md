# DEADMAN — Deploy overview

Three complementary deploy artifacts, each with a dedicated README:

| Artifact | Path | Purpose |
|---|---|---|
| TrueFoundry Service spec | `deploy/truefoundry/` | **Primary deploy target** — `tfy apply` directly to TFY platform |
| Helm chart | `deploy/helm/` | **Portable secondary** — any Kubernetes cluster |
| Terraform | `terraform/` | **AWS resources** — DynamoDB + IAM (used by both deploy paths) |

---

## Architecture at a glance

```
   Alertmanager / PagerDuty / AWS CloudWatch
              │ POST /incident
              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  DEADMAN Service  (port 8080)                           │
   │  FastAPI · uvicorn · non-root (uid 1001)                │
   │  /healthz · /readyz · /metrics · /incident              │
   └────────────┬───────────────────────────────┬────────────┘
                │                               │
   ┌────────────▼───────────┐   ┌───────────────▼────────────┐
   │  TFY AI Gateway        │   │  TFY MCP/Agent Gateway      │
   │  deadman-resilient-    │   │  Cedar default-DENY         │
   │  bedrock (5-tier)      │   │  + pre/post-tool guardrails │
   │  semantic cache        │   │  + autonomy budget          │
   └────────────────────────┘   └────────────────────────────┘
                │
   ┌────────────▼───────────────────────────────────────────┐
   │  AWS Bedrock  (Claude Opus 4.8 → Llama 4 → Mistral …)  │
   └────────────────────────────────────────────────────────┘
                │
   ┌────────────▼───────────────────────────────────────────┐
   │  DynamoDB  deadman-incident-state                       │
   │  Exactly-once state + audit log (PK/SK + GSI + TTL)    │
   └────────────────────────────────────────────────────────┘
```

---

## Deployment sequence

### 1. Provision AWS resources (both TFY and Helm paths need this)

```bash
cd terraform/
terraform init
terraform plan -var="iam_assume_role_principal=arn:aws:iam::ACCOUNT:root" -out=plan.tfplan
terraform apply plan.tfplan
```

Outputs needed downstream:
- `dynamodb_table_name` → `DEADMAN_DYNAMODB_TABLE`
- `iam_role_arn`        → IRSA annotation or static creds

### 2a. Deploy on TrueFoundry (primary)

```bash
# Apply AI/MCP gateway configs first
tfy apply -f infra/ai_gateway.yaml
tfy apply -f infra/guardrails.yaml

# Build/push image, create TFY Secrets (see deploy/truefoundry/README.md)
# then:
tfy apply -f deploy/truefoundry/deadman-service.yaml
```

Full guide: [deploy/truefoundry/README.md](truefoundry/README.md)

### 2b. Deploy on vanilla Kubernetes (portable path)

```bash
helm install deadman ./deploy/helm \
  --namespace deadman --create-namespace \
  --set image.repository=<your-registry>/deadman \
  --set image.tag=<sha-or-tag> \
  --set secrets.TFY_API_KEY="<key>" \
  --set secrets.TFY_GATEWAY_BASE_URL="<url>" \
  --set secrets.TFY_MCP_GATEWAY_URL="<url>" \
  --set secrets.DEADMAN_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="<iam_role_arn>"
```

Upgrade: `helm upgrade deadman ./deploy/helm -f my-prod-values.yaml`

---

## Secrets management

Secrets are **never** committed to this repo. Three options in increasing maturity:

1. **`--set` flags at deploy time** (demo/CI only).
2. **TFY Secrets / Helm `--set secrets.*`** (standard; the default wiring in this chart).
3. **External Secrets Operator** (production-grade): replace the `Secret` template with
   an `ExternalSecret` CR pointing at AWS Secrets Manager or Vault.

---

## Local development

```bash
# Mock mode — zero config, zero AWS creds
DEADMAN_MODE=mock uvicorn deadman.webhook:app --port 8080

# Docker + DynamoDB local
docker compose --profile local-db up
```
