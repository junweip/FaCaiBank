output "website_url" {
  description = "Customer-facing HTTPS URL (CloudFront, or the custom domain when configured)."
  value       = local.custom_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "api_base_url" {
  description = "Banking API base URL behind CloudFront and WAF."
  value       = "${local.custom_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.main.domain_name}"}/api"
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidations)."
  value       = aws_cloudfront_distribution.main.id
}

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
  description = "RDS PostgreSQL primary endpoint."
  value       = aws_db_instance.main.endpoint
}

output "rds_identifier" {
  description = "RDS primary instance identifier (for failover and restore tests)."
  value       = aws_db_instance.main.identifier
}

output "rds_replica_endpoint" {
  description = "RDS read replica endpoint, if enabled."
  value       = var.enable_rds_read_replica ? aws_db_instance.replica[0].endpoint : null
}

output "rds_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing RDS credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "ledger_table_name" {
  description = "Name of the encrypted DynamoDB ledger table."
  value       = aws_dynamodb_table.ledger.name
}

output "outbox_lease_table_name" {
  description = "DynamoDB table holding outbox shard leases and checkpoints."
  value       = aws_dynamodb_table.outbox_leases.name
}

output "kyc_statements_bucket_name" {
  description = "Name of the KYC and statements S3 bucket."
  value       = aws_s3_bucket.kyc_statements.bucket
}

output "audit_bucket_name" {
  description = "Name of the CloudTrail/Config audit S3 bucket (Object Lock)."
  value       = aws_s3_bucket.audit.bucket
}

output "artifacts_bucket_name" {
  description = "Bucket holding versioned deployment bundles and model artifacts."
  value       = aws_s3_bucket.artifacts.bucket
}

output "events_queue_url" {
  description = "URL of the notification events SQS queue."
  value       = aws_sqs_queue.events.url
}

output "events_dlq_arn" {
  description = "ARN of the notification events dead-letter queue."
  value       = aws_sqs_queue.events_dlq.arn
}

output "events_dlq_url" {
  description = "URL of the notification events dead-letter queue."
  value       = aws_sqs_queue.events_dlq.url
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

output "notifier_log_group" {
  description = "CloudWatch log group of the notification Lambda."
  value       = aws_cloudwatch_log_group.notifier.name
}

output "public_alb_dns_name" {
  description = "Public ALB hostname. It only answers CloudFront (secret header + origin prefix list)."
  value       = aws_lb.public.dns_name
}

output "internal_alb_dns_name" {
  description = "Private hostname for the backend API."
  value       = aws_lb.internal.dns_name
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_user_pool_client_id" {
  description = "Public Cognito app client ID."
  value       = aws_cognito_user_pool_client.frontend.id
}

output "asg_names" {
  description = "Auto Scaling Group names per fleet."
  value       = { for key, asg in aws_autoscaling_group.fleet : key => asg.name }
}

output "alerts_topic_arn" {
  description = "Page-level SNS topic (CloudWatch alarms, RDS events)."
  value       = aws_sns_topic.alerts.arn
}

output "ticket_topic_arn" {
  description = "Ticket-level SNS topic (DLQs, replica lag, budget)."
  value       = aws_sns_topic.alerts_ticket.arn
}

output "security_topic_arn" {
  description = "Security SNS topic (root/IAM/KMS changes, GuardDuty, Security Hub, Macie)."
  value       = aws_sns_topic.security_alerts.arn
}

output "dashboard_url" {
  description = "CloudWatch operations dashboard."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "fraud_endpoint_name" {
  description = "SageMaker fraud endpoint, if enabled."
  value       = var.enable_sagemaker_fraud ? aws_sagemaker_endpoint.fraud[0].name : null
}

output "athena_workgroup" {
  description = "Athena workgroup for the analytics lake, if enabled."
  value       = var.enable_analytics ? aws_athena_workgroup.analytics[0].name : null
}
