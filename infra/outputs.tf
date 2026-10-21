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

output "public_alb_dns_name" {
  description = "Public URL hostname for the FaCaiBank frontend."
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

output "cognito_hosted_ui_domain" {
  description = "Cognito hosted UI domain prefix."
  value       = aws_cognito_user_pool_domain.main.domain
}

output "frontend_asg_name" {
  description = "Frontend Auto Scaling Group name."
  value       = aws_autoscaling_group.frontend.name
}

output "backend_asg_name" {
  description = "Backend API Auto Scaling Group name."
  value       = aws_autoscaling_group.backend.name
}

output "workers_asg_name" {
  description = "Background workers Auto Scaling Group name."
  value       = aws_autoscaling_group.workers.name
}

output "alerts_topic_arn" {
  description = "SNS topic for CloudWatch and AWS Budget alerts."
  value       = aws_sns_topic.alerts.arn
}
