output "dynamodb_table_name" {
  description = "DynamoDB table name — set as DEADMAN_DYNAMODB_TABLE in the service config."
  value       = aws_dynamodb_table.deadman_state.name
}

output "dynamodb_table_arn" {
  description = "DynamoDB table ARN."
  value       = aws_dynamodb_table.deadman_state.arn
}

output "dynamodb_gsi_arn" {
  description = "ARN of the KeyStatusIndex GSI (appended to the table ARN)."
  value       = "${aws_dynamodb_table.deadman_state.arn}/index/KeyStatusIndex"
}

output "iam_policy_arn" {
  description = "ARN of the least-privilege IAM policy attached to the deadman role."
  value       = aws_iam_policy.deadman_policy.arn
}

output "iam_role_arn" {
  description = "ARN of the DEADMAN IAM role. Use as IRSA annotation or instance-profile ARN."
  value       = aws_iam_role.deadman_role.arn
}

output "iam_role_name" {
  description = "Name of the DEADMAN IAM role."
  value       = aws_iam_role.deadman_role.name
}
