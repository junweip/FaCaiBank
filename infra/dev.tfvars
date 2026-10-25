# Cost-safe development profile (fits the $200 credit). See docs/cost-review.md.
env                       = "dev"
aws_region                = "ap-southeast-1"
vpc_cidr                  = "10.0.0.0/16"
public_subnet_cidrs       = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
private_app_subnet_cidrs  = ["10.0.11.0/24", "10.0.12.0/24", "10.0.13.0/24"]
private_data_subnet_cidrs = ["10.0.21.0/24", "10.0.22.0/24", "10.0.23.0/24"]

# Networking and data: single NAT, single-AZ RDS, no paid interface endpoints.
single_nat_gateway         = true
enable_interface_endpoints = false
rds_multi_az               = false
enable_rds_read_replica    = false
enable_elasticache         = false
db_instance_class          = "db.t4g.micro"
db_backup_retention_days   = 1 # Free account plan maximum; 7 on a paid plan

# One instance per fleet, scaling to three under load.
app_instance_type    = "t3.micro"
asg_min_size         = 1
asg_max_size         = 3
asg_desired_capacity = 1

# GuardDuty, Security Hub and Macie are not available on the AWS Free account
# plan (SubscriptionRequiredException). Set them to true after upgrading the
# account to a paid plan; unused credits carry over. Object Lock in GOVERNANCE
# mode so the stack can be cleaned up.
enable_cloudtrail      = true
enable_guardduty       = false
enable_security_hub    = false
enable_macie           = false
enable_config          = true
enable_vpc_flow_logs   = true
audit_object_lock_mode = "GOVERNANCE"

# Turn on to save roughly a third of EC2 and RDS cost while nobody is working.
enable_office_hours_schedule = false

# Optional services stay off in dev.
enable_sagemaker_fraud   = false
enable_analytics         = false
enable_bedrock_assistant = false
enable_identity_center   = false

monthly_budget_limit = 200
budget_alert_emails  = []
alert_email          = ""
security_alert_email = ""
notification_email   = ""
