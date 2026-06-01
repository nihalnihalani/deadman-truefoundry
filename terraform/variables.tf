variable "aws_region" {
  description = "Primary AWS region for DynamoDB and Bedrock."
  type        = string
  default     = "us-east-1"
}

variable "dynamodb_table_name" {
  description = "DynamoDB table name for DEADMAN incident state. Must match DEADMAN_DYNAMODB_TABLE in the service config."
  type        = string
  default     = "deadman-incident-state"
}

variable "iam_role_name" {
  description = "Name of the IAM role granted least-privilege access to DynamoDB and Bedrock. Intended for IRSA (EKS workload identity) or a TFY-provisioned service account."
  type        = string
  default     = "deadman-role"
}

variable "iam_policy_name" {
  description = "Name of the IAM policy attached to the deadman role."
  type        = string
  default     = "deadman-policy"
}

variable "iam_assume_role_principal" {
  description = <<-EOT
    IAM principal ARN(s) allowed to assume the deadman role. For IRSA set this to
    the OIDC provider ARN of your EKS cluster (the trust policy will be configured
    separately per the IRSA guide). For a simpler setup, set to an IAM user or role ARN.
    Example: "arn:aws:iam::123456789012:root"
  EOT
  type        = string
  default     = "arn:aws:iam::REPLACE_WITH_ACCOUNT_ID:root"
}

variable "enable_point_in_time_recovery" {
  description = "Enable DynamoDB point-in-time recovery (PITR) for the incident state table. Recommended in production."
  type        = bool
  default     = true
}

variable "ttl_attribute" {
  description = "DynamoDB attribute name used for TTL (time-to-live) item expiry. Must match the attribute written by the Rampart lifecycle wave."
  type        = string
  default     = "ttl"
}

variable "tags" {
  description = "Tags to apply to all AWS resources."
  type        = map(string)
  default = {
    Project     = "deadman"
    ManagedBy   = "terraform"
    Environment = "production"
  }
}
