# DEADMAN — Terraform (AWS resources)

Provisions the AWS-side resources required for a production DEADMAN deployment:
a DynamoDB table (incident state + audit log) and a least-privilege IAM role for
Bedrock + DynamoDB access.

## Resources created

| Resource | Name (default) | Purpose |
|---|---|---|
| `aws_dynamodb_table` | `deadman-incident-state` | Exactly-once incident state + audit log |
| `aws_iam_policy` | `deadman-policy` | Least-privilege DynamoDB + Bedrock access |
| `aws_iam_role` | `deadman-role` | Workload identity (IRSA / instance profile) |

## Prerequisites

- Terraform >= 1.5
- AWS credentials with permissions to create DynamoDB tables and IAM resources
- AWS provider ~> 5.x (fetched automatically by `terraform init`)

## Quick start

```bash
cd terraform/

# 1. Initialize providers
terraform init

# 2. Review the plan
terraform plan \
  -var="aws_region=us-east-1" \
  -var="iam_assume_role_principal=arn:aws:iam::ACCOUNT_ID:root" \
  -out=deadman.tfplan

# 3. Apply
terraform apply deadman.tfplan

# 4. Capture outputs for wiring into the TFY deploy
terraform output iam_role_arn          # → IRSA annotation
terraform output dynamodb_table_name   # → DEADMAN_DYNAMODB_TABLE
```

## Variables

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region for all resources |
| `dynamodb_table_name` | `deadman-incident-state` | Table name (must match `DEADMAN_DYNAMODB_TABLE`) |
| `iam_role_name` | `deadman-role` | IAM role name |
| `iam_policy_name` | `deadman-policy` | IAM policy name |
| `iam_assume_role_principal` | `arn:aws:iam::REPLACE_WITH_ACCOUNT_ID:root` | Who can assume the role |
| `enable_point_in_time_recovery` | `true` | PITR for the DynamoDB table |
| `ttl_attribute` | `ttl` | DynamoDB TTL attribute name |
| `tags` | `{Project=deadman,...}` | Tags applied to all resources |

## DynamoDB table schema

```
Table: deadman-incident-state
PK (S): incident_id
SK (S): "STATE" | "AUDIT#<seq>" | "COMMIT#<key>"

GSI: KeyStatusIndex
  GSI PK  (S): key
  GSI SK  (S): status
  Projection: ALL
  (used by is_committed() hot path in DynamoDBBackend)

TTL attribute: ttl (epoch seconds — written by Rampart lifecycle wave)
Billing: PAY_PER_REQUEST
PITR: enabled
```

## IAM policy summary (least-privilege)

```
DynamoDB (table + KeyStatusIndex ARN):
  dynamodb:GetItem
  dynamodb:PutItem
  dynamodb:Query
  dynamodb:DeleteItem
  dynamodb:BatchWriteItem

Bedrock (model invocation):
  bedrock:InvokeModel
  bedrock:InvokeModelWithResponseStream
  Resource: arn:aws:bedrock:*::foundation-model/*

Bedrock (model discovery at startup):
  bedrock:ListFoundationModels
  bedrock:ListInferenceProfiles
  Resource: * (list APIs have no resource-level restriction)
```

## IRSA wiring (EKS)

Replace the default trust policy principal with your EKS OIDC provider:

```hcl
# terraform.tfvars
iam_assume_role_principal = "arn:aws:iam::<ACCOUNT>:oidc-provider/<OIDC_URL>"
```

Then annotate the Kubernetes ServiceAccount (Helm values or TFY service config):

```yaml
# In values.yaml or deadman-service.yaml
service_account_annotations:
  eks.amazonaws.com/role-arn: "<iam_role_arn output>"
```

With IRSA in place, remove `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from the
TFY secrets — the pod will assume the role automatically via the projected token.

## State backend

Terraform state is local by default (`terraform.tfstate`). For team use, configure
a remote backend (e.g., S3 + DynamoDB state locking):

```hcl
# Add to versions.tf backend block
backend "s3" {
  bucket         = "<state-bucket>"
  key            = "deadman/terraform.tfstate"
  region         = "us-east-1"
  dynamodb_table = "<lock-table>"
  encrypt        = true
}
```
