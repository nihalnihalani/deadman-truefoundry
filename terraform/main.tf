# DEADMAN — AWS resources
# ========================
# Provisions:
#   1. DynamoDB table  (incident state + audit log, exactly-once writes)
#   2. IAM policy      (least-privilege: DynamoDB + Bedrock)
#   3. IAM role        (assumable by the TFY workload / EKS IRSA principal)
#
# Apply:
#   terraform init
#   terraform plan   -out=deadman.tfplan
#   terraform apply  deadman.tfplan
#
# After apply, wire the outputs into TFY secrets / IRSA annotations:
#   terraform output iam_role_arn      → IRSA annotation or fallback IAM creds
#   terraform output dynamodb_table_name → DEADMAN_DYNAMODB_TABLE env var

# ---------------------------------------------------------------------------
# 1. DynamoDB table
# ---------------------------------------------------------------------------
#
# Schema (must match deadman/state.py DynamoDB backend):
#   PK  (S)  incident_id
#   SK  (S)  "STATE" | "AUDIT#<seq>" | "COMMIT#<key>"
#
# GSI "KeyStatusIndex" (optional, for is_committed() hot path):
#   GSI PK:  key    (S)
#   GSI SK:  status (S)
#   Covers the Query in DynamoDBBackend.is_committed() path 2.
#
# Billing: PAY_PER_REQUEST — no capacity planning needed; auto-scales with traffic.
# TTL:     enabled on var.ttl_attribute so Rampart lifecycle can expire old incidents.
# PITR:    enabled (var.enable_point_in_time_recovery) for production safety.

resource "aws_dynamodb_table" "deadman_state" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # Attributes projected into the GSI must be declared here even if not table keys.
  attribute {
    name = "key"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  # TTL — Rampart wave writes epoch seconds into this attribute on incident items.
  # DynamoDB will delete items after the TTL expires (best-effort, within ~48 h).
  ttl {
    attribute_name = var.ttl_attribute
    enabled        = true
  }

  # Point-in-time recovery for production safety.
  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  # GSI "KeyStatusIndex" — used by DynamoDBBackend.is_committed() path 2.
  # GSI PK = key (the idempotency key), GSI SK = status.
  # Partition skew is acceptable here: each unique idempotency key is one item.
  global_secondary_index {
    name            = "KeyStatusIndex"
    hash_key        = "key"
    range_key       = "status"
    projection_type = "ALL"   # project all attributes so is_committed needs no extra fetch
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# 2. IAM policy — least-privilege
# ---------------------------------------------------------------------------
#
# DynamoDB grants (table + GSI ARN required for Index queries):
#   GetItem       — load_state()
#   PutItem       — save_state(), append_audit(), claim_commit()
#   Query         — read_audit(), _next_seq(), is_committed() (GSI query)
#   DeleteItem    — reset() individual item deletes
#   BatchWriteItem— reset() batch deletes (batch_writer)
#
# Bedrock grants:
#   InvokeModel / InvokeModelWithResponseStream — realmode_ai.complete()
#   ListFoundationModels / ListInferenceProfiles — resolve_model_id() at startup
#
# No s3, no ec2, no iam — strictly what the code calls.

data "aws_iam_policy_document" "deadman_policy_doc" {
  # DynamoDB — table-level operations
  statement {
    sid    = "DynamoDBTableAccess"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      aws_dynamodb_table.deadman_state.arn,
      "${aws_dynamodb_table.deadman_state.arn}/index/KeyStatusIndex",
    ]
  }

  # Bedrock — model invocation (all models; restrict to specific ARNs if desired)
  statement {
    sid    = "BedrockInvokeModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Wildcard resource is standard for Bedrock model invocation.
    # To restrict to specific model ARNs add them here:
    #   "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-8-*"
    resources = ["arn:aws:bedrock:*::foundation-model/*"]
  }

  # Bedrock — model discovery at startup (resolve_model_id uses both APIs)
  statement {
    sid    = "BedrockListModels"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:ListInferenceProfiles",
    ]
    resources = ["*"] # list APIs do not accept resource-level restrictions
  }
}

resource "aws_iam_policy" "deadman_policy" {
  name        = var.iam_policy_name
  description = "Least-privilege policy for DEADMAN incident-commander: DynamoDB state + Bedrock model invocation."
  policy      = data.aws_iam_policy_document.deadman_policy_doc.json

  tags = var.tags
}

# ---------------------------------------------------------------------------
# 3. IAM role
# ---------------------------------------------------------------------------
#
# Trust policy: allows the principal in var.iam_assume_role_principal to assume
# this role. For IRSA replace the principal with your EKS OIDC provider ARN and
# add the Condition block for the service account subject:
#
#   "Principal": {
#     "Federated": "arn:aws:iam::<account>:oidc-provider/<oidc-provider-url>"
#   },
#   "Condition": {
#     "StringEquals": {
#       "<oidc-provider-url>:sub": "system:serviceaccount:<namespace>:deadman"
#     }
#   }
#
# See: https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html

data "aws_iam_policy_document" "deadman_assume_role" {
  statement {
    sid     = "AllowAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.iam_assume_role_principal]
    }
  }
}

resource "aws_iam_role" "deadman_role" {
  name               = var.iam_role_name
  assume_role_policy = data.aws_iam_policy_document.deadman_assume_role.json
  description        = "DEADMAN incident-commander workload identity — DynamoDB + Bedrock."

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "deadman_policy_attach" {
  role       = aws_iam_role.deadman_role.name
  policy_arn = aws_iam_policy.deadman_policy.arn
}
