variable "env" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "ap-southeast-1"
}

variable "app_version" {
  description = "Release identifier (git tag or SHA) stamped on bundles, instances and /health."
  type        = string
  default     = "dev"
}

# ---------------------------------------------------------------- network

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the three public subnets."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_cidrs) == 3
    error_message = "Exactly three public subnet CIDRs are required."
  }
}

variable "private_app_subnet_cidrs" {
  description = "CIDR blocks for the three private application subnets."
  type        = list(string)

  validation {
    condition     = length(var.private_app_subnet_cidrs) == 3
    error_message = "Exactly three private application subnet CIDRs are required."
  }
}

variable "private_data_subnet_cidrs" {
  description = "CIDR blocks for the three private data subnets."
  type        = list(string)

  validation {
    condition     = length(var.private_data_subnet_cidrs) == 3
    error_message = "Exactly three private data subnet CIDRs are required."
  }
}

variable "single_nat_gateway" {
  description = "Use one NAT Gateway for all private subnets instead of one per AZ."
  type        = bool
  default     = true
}

variable "enable_interface_endpoints" {
  description = "Create cost-incurring interface VPC endpoints for private subnets."
  type        = bool
  default     = false
}

variable "enable_vpc_flow_logs" {
  description = "Send VPC flow logs to CloudWatch Logs."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- data

variable "rds_multi_az" {
  description = "Enable a Multi-AZ RDS deployment (synchronous standby, automatic failover)."
  type        = bool
  default     = false
}

variable "enable_rds_read_replica" {
  description = "Create an asynchronous RDS read replica in the third AZ for statements and reports."
  type        = bool
  default     = false
}

variable "db_instance_class" {
  description = "RDS PostgreSQL instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "facaibank"
}

variable "db_username" {
  description = "Master PostgreSQL username. Its generated password is stored in Secrets Manager."
  type        = string
  default     = "facaibank_admin"
}

variable "db_allocated_storage" {
  description = "Allocated RDS storage in GiB."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "RDS automated backup retention (point-in-time restore window)."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Protect RDS, DynamoDB and Cognito from accidental deletion. Keep false for disposable demo stacks."
  type        = bool
  default     = false
}

variable "enable_elasticache" {
  description = "Create an encrypted Valkey replication group (primary + replica) for sessions and hot reads."
  type        = bool
  default     = false
}

variable "elasticache_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "audit_object_lock_mode" {
  description = "S3 Object Lock mode for the audit bucket. COMPLIANCE cannot be removed by anyone, including root, until retention ends."
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.audit_object_lock_mode)
    error_message = "audit_object_lock_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "audit_retention_days" {
  description = "Retention for audit logs and KYC records (MAS: at least five years)."
  type        = number
  default     = 1825
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for application and Lambda logs."
  type        = number
  default     = 90
}

# ---------------------------------------------------------------- compute

variable "lambda_timeout" {
  description = "Timeout in seconds for the notification Lambda."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Memory in MiB for the notification Lambda."
  type        = number
  default     = 128
}

variable "app_instance_type" {
  description = "EC2 instance type for the frontend, API and worker Auto Scaling Groups."
  type        = string
  default     = "t3.micro"
}

variable "asg_min_size" {
  description = "Minimum capacity for each application Auto Scaling Group (3 = one instance per AZ)."
  type        = number
  default     = 1
}

variable "asg_max_size" {
  description = "Maximum capacity for each application Auto Scaling Group."
  type        = number
  default     = 3
}

variable "asg_desired_capacity" {
  description = "Initial desired capacity for each application Auto Scaling Group."
  type        = number
  default     = 1
}

variable "frontend_requests_per_target" {
  description = "Target ALB requests per frontend instance per minute."
  type        = number
  default     = 1200
}

variable "backend_requests_per_target" {
  description = "Target ALB requests per API instance per minute."
  type        = number
  default     = 600
}

variable "cpu_target_percent" {
  description = "Target average CPU for the frontend and API fleets."
  type        = number
  default     = 60
}

variable "outbox_iterator_age_target_ms" {
  description = "Target maximum stream iterator age for the outbox worker fleet."
  type        = number
  default     = 10000
}

variable "statement_backlog_per_instance" {
  description = "Target SQS statement jobs waiting per statement worker."
  type        = number
  default     = 5
}

# ---------------------------------------------------------------- edge and identity

variable "domain_name" {
  description = "Optional custom domain (for example bank.example.com). Empty uses the CloudFront domain."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone that owns domain_name. Required when domain_name is set."
  type        = string
  default     = ""
}

variable "waf_rate_limit_per_5min" {
  description = "Requests per client IP per five minutes before WAF blocks. Raise temporarily for single-IP load tests."
  type        = number
  default     = 2000
}

variable "enable_waf_bot_control" {
  description = "Add the AWS WAF Bot Control managed rule group (extra monthly cost)."
  type        = bool
  default     = false
}

variable "geo_allowlist" {
  description = "Optional ISO country codes allowed through CloudFront (for example [\"SG\"]). Empty allows all."
  type        = list(string)
  default     = []
}

variable "cognito_mfa_configuration" {
  description = "Cognito MFA mode. ON requires TOTP for every customer."
  type        = string
  default     = "ON"
}

variable "step_up_amount" {
  description = "Transfers and withdrawals at or above this SGD amount require a recent sign-in."
  type        = string
  default     = "1000.00"
}

# ---------------------------------------------------------------- security services

variable "enable_cloudtrail" {
  description = "Create the multi-region CloudTrail trail with log validation and CloudWatch Logs delivery."
  type        = bool
  default     = true
}

variable "enable_guardduty" {
  description = "Enable GuardDuty threat detection (account-level singleton)."
  type        = bool
  default     = true
}

variable "enable_security_hub" {
  description = "Enable Security Hub with the AWS Foundational Security Best Practices standard."
  type        = bool
  default     = true
}

variable "enable_macie" {
  description = "Enable Macie and a weekly sensitive-data job on the KYC bucket."
  type        = bool
  default     = true
}

variable "enable_config" {
  description = "Enable AWS Config recording and managed compliance rules."
  type        = bool
  default     = true
}

variable "enable_identity_center" {
  description = "Create IAM Identity Center permission sets and groups (Identity Center must already be enabled)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- optional services

variable "enable_sagemaker_fraud" {
  description = "Deploy the fraud-scoring model to a SageMaker serverless endpoint."
  type        = bool
  default     = false
}

variable "enable_analytics" {
  description = "Create the analytics lake: daily DynamoDB export, Glue job and crawler, Athena workgroup."
  type        = bool
  default     = false
}

variable "quicksight_user_arn" {
  description = "Optional QuickSight user ARN to own an Athena data source (requires an active QuickSight subscription)."
  type        = string
  default     = ""
}

variable "enable_bedrock_assistant" {
  description = "Deploy the Bedrock ops assistant. Run scripts/build_lambdas.sh first and enable model access."
  type        = bool
  default     = false
}

variable "bedrock_model_id" {
  description = "Claude model on Amazon Bedrock used by the ops assistant."
  type        = string
  default     = "anthropic.claude-opus-5-5"
}

variable "bedrock_fallback_model_id" {
  description = "Model the ops assistant retries on if the primary model declines."
  type        = string
  default     = "anthropic.claude-opus-4-8"
}

variable "enable_monthly_statements" {
  description = "Schedule the monthly statement run on the 1st of each month (Singapore time)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- notifications and cost

variable "monthly_budget_limit" {
  description = "Monthly AWS cost budget in USD."
  type        = number
  default     = 200
}

variable "budget_alert_emails" {
  description = "Budget owners emailed at 50, 80 and 100 percent of the monthly budget."
  type        = list(string)
  default     = []
}

variable "alert_email" {
  description = "On-call email subscribed to page, ticket and ops-assistant topics. Empty skips the subscription."
  type        = string
  default     = ""
}

variable "security_alert_email" {
  description = "Security team email subscribed to the security topic. Empty skips the subscription."
  type        = string
  default     = ""
}

variable "notification_email" {
  description = "Demo inbox that receives every customer transaction alert. Empty skips the subscription."
  type        = string
  default     = ""
}

variable "slack_webhook_url" {
  description = "Optional Slack incoming-webhook URL for operational alerts (stored in Secrets Manager)."
  type        = string
  default     = ""
  sensitive   = true
}
