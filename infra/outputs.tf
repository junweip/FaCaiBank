output "vpc_id" {
  description = "ID of the FaCaiBank VPC."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "IDs of the three public subnets."
  value       = aws_subnet.public[*].id
}

output "private_app_subnet_ids" {
  description = "IDs of the three private application subnets."
  value       = aws_subnet.private_app[*].id
}

output "private_data_subnet_ids" {
  description = "IDs of the three private data subnets."
  value       = aws_subnet.private_data[*].id
}

output "kms_key_arn" {
  description = "ARN of the FaCaiBank customer-managed key."
  value       = aws_kms_key.main.arn
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint."
  value       = aws_db_instance.main.endpoint
}

output "rds_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing RDS credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "ledger_table_name" {
  description = "Name of the encrypted DynamoDB ledger table."
  value       = aws_dynamodb_table.ledger.name
}

output "kyc_statements_bucket_name" {
  description = "Name of the KYC and statements S3 bucket."
  value       = aws_s3_bucket.kyc_statements.bucket
}

output "audit_bucket_name" {
  description = "Name of the CloudTrail audit S3 bucket."
  value       = aws_s3_bucket.audit.bucket
}

output "events_queue_url" {
  description = "URL of the notification events SQS queue."
  value       = aws_sqs_queue.events.url
}

output "events_dlq_arn" {
  description = "ARN of the notification events dead-letter queue."
  value       = aws_sqs_queue.events_dlq.arn
}

output "statement_jobs_queue_url" {
  description = "URL of the statement jobs SQS queue."
  value       = aws_sqs_queue.statement_jobs.url
}

output "statement_dlq_arn" {
  description = "ARN of the statement jobs dead-letter queue."
  value       = aws_sqs_queue.statement_dlq.arn
}

output "notifications_topic_arn" {
  description = "ARN of the customer notifications SNS topic."
  value       = aws_sns_topic.notifications.arn
}
