# Demo-day profile: every component on the architecture diagram. Expect roughly
# USD 30 per day, a third of it interface endpoints (docs/cost-review.md). Apply
# for the demo, then go back to dev.tfvars or destroy.
env                       = "dev"
aws_region                = "ap-southeast-1"
vpc_cidr                  = "10.0.0.0/16"
public_subnet_cidrs       = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
private_app_subnet_cidrs  = ["10.0.11.0/24", "10.0.12.0/24", "10.0.13.0/24"]
private_data_subnet_cidrs = ["10.0.21.0/24", "10.0.22.0/24", "10.0.23.0/24"]

single_nat_gateway         = false
enable_interface_endpoints = true
rds_multi_az               = true
enable_rds_read_replica    = true
enable_elasticache         = true
db_instance_class          = "db.t4g.small"

# Full fleet redundancy: one instance per AZ in every fleet.
app_instance_type    = "t3.small"
asg_min_size         = 3
asg_max_size         = 6
asg_desired_capacity = 3

enable_cloudtrail      = true
enable_guardduty       = true
enable_security_hub    = true
enable_macie           = true
enable_config          = true
enable_vpc_flow_logs   = true
audit_object_lock_mode = "GOVERNANCE"

enable_sagemaker_fraud   = true
enable_analytics         = true
enable_bedrock_assistant = true
enable_identity_center   = false
enable_waf_bot_control   = false

monthly_budget_limit = 200
budget_alert_emails  = []
alert_email          = ""
security_alert_email = ""
notification_email   = ""
