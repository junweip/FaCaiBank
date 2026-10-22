data "aws_ssm_parameter" "amazon_linux_2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_ec2_managed_prefix_list" "cloudfront_origin" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# ---------------------------------------------------------------- versioned bundles

data "archive_file" "frontend_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/frontend"
  output_path = "${path.root}/.terraform/frontend_bundle.zip"
}

data "archive_file" "backend_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/backend_api"
  output_path = "${path.root}/.terraform/backend_bundle.zip"
  excludes    = ["__pycache__", "__pycache__/*", "*.pyc", "requirements-dev.txt"]
}

data "archive_file" "workers_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/workers"
  output_path = "${path.root}/.terraform/workers_bundle.zip"
  excludes    = ["__pycache__", "__pycache__/*", "*.pyc"]
}

locals {
  bundles = {
    frontend = data.archive_file.frontend_bundle
    backend  = data.archive_file.backend_bundle
    workers  = data.archive_file.workers_bundle
  }
  service_log_groups = ["frontend", "backend", "outbox-worker", "statement-worker"]
}

# Each release uploads content-addressed bundles, so a code change produces a new
# launch template version and a rolling instance refresh. Older bundles stay in
# the bucket for rollback.
resource "aws_s3_object" "bundle" {
  for_each = local.bundles

  bucket                 = aws_s3_bucket.artifacts.bucket
  key                    = "deploy/${var.env}/${each.key}-${each.value.output_md5}.zip"
  source                 = each.value.output_path
  source_hash            = each.value.output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = {
    Name       = "facaibank-${var.env}-${each.key}-bundle"
    Tier       = "deployment"
    AppVersion = var.app_version
  }
}

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset(local.service_log_groups)

  name              = "/facaibank/${var.env}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# ---------------------------------------------------------------- security groups

resource "aws_security_group" "public_alb" {
  name        = "facaibank-${var.env}-public-alb"
  description = "Public HTTP ingress for the FaCaiBank frontend."
  vpc_id      = aws_vpc.main.id

  # Only CloudFront edge locations can reach the ALB; WAF runs at CloudFront.
  ingress {
    description     = "CloudFront origin-facing addresses only"
    from_port       = local.custom_domain ? 443 : 80
    to_port         = local.custom_domain ? 443 : 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-public-alb"
    Tier = "public"
  }
}

resource "aws_security_group" "frontend" {
  name        = "facaibank-${var.env}-frontend"
  description = "Allow frontend traffic only from the public ALB."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Allow outbound traffic for package installation and AWS APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-frontend"
    Tier = "app"
  }
}

resource "aws_vpc_security_group_ingress_rule" "frontend_from_public_alb" {
  security_group_id            = aws_security_group.frontend.id
  referenced_security_group_id = aws_security_group.public_alb.id
  description                  = "Frontend HTTP from the public ALB."
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "internal_alb" {
  name        = "facaibank-${var.env}-internal-alb"
  description = "Private HTTP access from frontend instances to API targets."
  vpc_id      = aws_vpc.main.id

  egress {
    description     = "HTTP to backend API targets"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_app.id]
  }

  tags = {
    Name = "facaibank-${var.env}-internal-alb"
    Tier = "app"
  }
}

resource "aws_vpc_security_group_ingress_rule" "internal_alb_from_frontend" {
  security_group_id            = aws_security_group.internal_alb.id
  referenced_security_group_id = aws_security_group.frontend.id
  description                  = "Private API access from frontend instances."
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "backend_api_from_internal_alb" {
  security_group_id            = aws_security_group.backend_app.id
  referenced_security_group_id = aws_security_group.internal_alb.id
  description                  = "Backend API traffic from the internal ALB."
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "outbox_workers" {
  name        = "facaibank-${var.env}-outbox-workers"
  description = "Outbound-only security group for the DynamoDB Streams outbox workers."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-outbox-workers"
    Tier = "app"
  }
}

resource "aws_security_group" "statement_workers" {
  name        = "facaibank-${var.env}-statement-workers"
  description = "Outbound-only security group for the SQS statement workers."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-statement-workers"
    Tier = "app"
  }
}

# ---------------------------------------------------------------- load balancers

resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

resource "aws_lb" "public" {
  name                       = "facaibank-${var.env}-public"
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.public_alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = false
  drop_invalid_header_fields = true

  tags = {
    Name = "facaibank-${var.env}-public-alb"
    Tier = "public"
  }
}

resource "aws_lb" "internal" {
  name                       = "facaibank-${var.env}-internal"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.internal_alb.id]
  subnets                    = aws_subnet.private_app[*].id
  enable_deletion_protection = false
  drop_invalid_header_fields = true

  tags = {
    Name = "facaibank-${var.env}-internal-alb"
    Tier = "app"
  }
}

resource "aws_lb_target_group" "frontend" {
  name                 = "facaibank-${var.env}-frontend"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/healthz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "facaibank-${var.env}-frontend"
    Tier = "app"
  }
}

resource "aws_lb_target_group" "backend" {
  name                 = "facaibank-${var.env}-backend"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "facaibank-${var.env}-backend"
    Tier = "app"
  }
}

resource "aws_lb_listener" "public_http" {
  load_balancer_arn = aws_lb.public.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Direct access is not allowed. Use the FaCaiBank website."
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener" "public_https" {
  count = local.custom_domain ? 1 : 0

  load_balancer_arn = aws_lb.public.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.origin[0].certificate_arn

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Direct access is not allowed. Use the FaCaiBank website."
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_cloudfront" {
  for_each = merge(
    { http = aws_lb_listener.public_http.arn },
    local.custom_domain ? { https = aws_lb_listener.public_https[0].arn } : {},
  )

  listener_arn = each.value
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }
}

resource "aws_lb_listener" "internal_http" {
  load_balancer_arn = aws_lb.internal.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

# ---------------------------------------------------------------- Cognito

resource "aws_cognito_user_pool" "main" {
  name                     = "facaibank-${var.env}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = var.cognito_mfa_configuration
  deletion_protection      = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Your FaCai Bank verification code"
    email_message        = "Your FaCai Bank verification code is {####}. It expires in 24 hours."
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true

    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  tags = {
    Name = "facaibank-${var.env}-users"
    Tier = "security"
  }

  lifecycle {
    ignore_changes = [schema]
  }
}

resource "aws_cognito_user_pool_client" "frontend" {
  name                          = "facaibank-${var.env}-frontend"
  user_pool_id                  = aws_cognito_user_pool.main.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  auth_session_validity         = 10
  access_token_validity         = 60
  id_token_validity             = 60
  refresh_token_validity        = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "facaibank-${var.env}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# ---------------------------------------------------------------- instance roles

locals {
  ec2_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  instance_roles   = ["frontend", "backend", "outbox", "statement"]
  fraud_endpoint   = "facaibank-${var.env}-fraud"
  bundle_read      = "${aws_s3_bucket.artifacts.arn}/deploy/${var.env}/*"
  kms_use_actions  = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
  ledger_stream    = "${aws_dynamodb_table.ledger.arn}/stream/*"
  redis_secret_arn = var.enable_elasticache ? aws_secretsmanager_secret.redis_auth[0].arn : null
}

resource "aws_iam_role" "instance" {
  for_each = toset(local.instance_roles)

  name               = "facaibank-${var.env}-${each.key}-instance"
  assume_role_policy = local.ec2_assume_role

  tags = {
    Name = "facaibank-${var.env}-${each.key}-instance"
    Tier = "app"
  }
}

# Session Manager replaces SSH and bastions; the CloudWatch agent ships logs.
resource "aws_iam_role_policy_attachment" "ssm" {
  for_each = aws_iam_role.instance

  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  for_each = aws_iam_role.instance

  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "frontend_instance" {
  name = "facaibank-${var.env}-frontend-access"
  role = aws_iam_role.instance["frontend"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.bundle_read },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"], Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_iam_role_policy" "backend_instance" {
  name = "facaibank-${var.env}-backend-access"
  role = aws_iam_role.instance["backend"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.bundle_read },
        {
          Effect   = "Allow"
          Action   = ["s3:PutObject", "s3:DeleteObject"]
          Resource = "${aws_s3_bucket.kyc_statements.arn}/kyc/*"
        },
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = "${aws_s3_bucket.kyc_statements.arn}/statements/*"
        },
        {
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = concat([aws_db_instance.main.master_user_secret[0].secret_arn], var.enable_elasticache ? [local.redis_secret_arn] : [])
        },
        {
          Effect = "Allow"
          Action = [
            "dynamodb:ConditionCheckItem",
            "dynamodb:DeleteItem",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:Query",
            "dynamodb:TransactWriteItems",
            "dynamodb:UpdateItem",
          ]
          Resource = aws_dynamodb_table.ledger.arn
        },
        { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.statement_jobs.arn },
        { Effect = "Allow", Action = ["sns:Subscribe"], Resource = aws_sns_topic.notifications.arn },
        {
          Effect   = "Allow"
          Action   = ["sns:GetSubscriptionAttributes"]
          Resource = "${aws_sns_topic.notifications.arn}:*"
        },
        {
          Effect    = "Allow"
          Action    = ["cloudwatch:PutMetricData"]
          Resource  = "*"
          Condition = { StringEquals = { "cloudwatch:namespace" = "FaCaiBank/API" } }
        },
        { Effect = "Allow", Action = local.kms_use_actions, Resource = aws_kms_key.main.arn },
      ],
      var.enable_sagemaker_fraud ? [{
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = "arn:aws:sagemaker:${var.aws_region}:${local.account_id}:endpoint/${local.fraud_endpoint}"
      }] : [],
    )
  })
}

resource "aws_iam_role_policy" "outbox_instance" {
  name = "facaibank-${var.env}-outbox-access"
  role = aws_iam_role.instance["outbox"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.bundle_read },
      { Effect = "Allow", Action = ["dynamodb:DescribeTable"], Resource = aws_dynamodb_table.ledger.arn },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
        Resource = local.ledger_stream
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.outbox_leases.arn
      },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.events.arn },
      {
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "FaCaiBank/Workers" } }
      },
      { Effect = "Allow", Action = local.kms_use_actions, Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_iam_role_policy" "statement_instance" {
  name = "facaibank-${var.env}-statement-access"
  role = aws_iam_role.instance["statement"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.bundle_read },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.ledger.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ChangeMessageVisibility",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage",
          "sqs:SendMessage",
        ]
        Resource = aws_sqs_queue.statement_jobs.arn
      },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.kyc_statements.arn}/statements/*" },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_db_instance.main.master_user_secret[0].secret_arn
      },
      { Effect = "Allow", Action = local.kms_use_actions, Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  for_each = aws_iam_role.instance

  name = "facaibank-${var.env}-${each.key}"
  role = each.value.name
}

# ---------------------------------------------------------------- launch templates

locals {
  database_read_host = var.enable_rds_read_replica ? aws_db_instance.replica[0].address : aws_db_instance.main.address

  backend_environment = {
    ENVIRONMENT             = var.env
    APP_VERSION             = var.app_version
    AWS_REGION              = var.aws_region
    DATABASE_HOST           = aws_db_instance.main.address
    DATABASE_NAME           = var.db_name
    DATABASE_SECRET_ARN     = aws_db_instance.main.master_user_secret[0].secret_arn
    LEDGER_TABLE_NAME       = aws_dynamodb_table.ledger.name
    KYC_STATEMENTS_BUCKET   = aws_s3_bucket.kyc_statements.bucket
    STATEMENT_QUEUE_URL     = aws_sqs_queue.statement_jobs.url
    NOTIFICATIONS_TOPIC_ARN = aws_sns_topic.notifications.arn
    COGNITO_USER_POOL_ID    = aws_cognito_user_pool.main.id
    COGNITO_APP_CLIENT_ID   = aws_cognito_user_pool_client.frontend.id
    REDIS_HOST              = var.enable_elasticache ? aws_elasticache_replication_group.main[0].primary_endpoint_address : ""
    REDIS_AUTH_SECRET_ARN   = var.enable_elasticache ? local.redis_secret_arn : ""
    FRAUD_ENDPOINT_NAME     = var.enable_sagemaker_fraud ? local.fraud_endpoint : ""
    STEP_UP_AMOUNT          = var.step_up_amount
    METRICS_ENABLED         = "true"
    CORS_ORIGINS            = ""
  }

  worker_environment = {
    outbox = {
      ENVIRONMENT        = var.env
      AWS_REGION         = var.aws_region
      LEDGER_TABLE_NAME  = aws_dynamodb_table.ledger.name
      EVENTS_QUEUE_URL   = aws_sqs_queue.events.url
      OUTBOX_LEASE_TABLE = aws_dynamodb_table.outbox_leases.name
      METRICS_ENABLED    = "true"
    }
    statement = {
      ENVIRONMENT           = var.env
      AWS_REGION            = var.aws_region
      LEDGER_TABLE_NAME     = aws_dynamodb_table.ledger.name
      STATEMENT_QUEUE_URL   = aws_sqs_queue.statement_jobs.url
      KYC_STATEMENTS_BUCKET = aws_s3_bucket.kyc_statements.bucket
      DATABASE_HOST         = aws_db_instance.main.address
      DATABASE_READ_HOST    = local.database_read_host
      DATABASE_NAME         = var.db_name
      DATABASE_SECRET_ARN   = aws_db_instance.main.master_user_secret[0].secret_arn
    }
  }

  frontend_config = <<-EOT
    window.FACAI_CONFIG = ${jsonencode({
  apiBase    = "/api"
  region     = var.aws_region
  userPoolId = aws_cognito_user_pool.main.id
  clientId   = aws_cognito_user_pool_client.frontend.id
  version    = var.app_version
})};
  EOT

launch_templates = {
  frontend = {
    security_group = aws_security_group.frontend.id
    user_data = templatefile("${path.module}/templates/frontend.sh.tftpl", {
      aws_region       = var.aws_region
      bucket_name      = aws_s3_bucket.artifacts.bucket
      bundle_key       = aws_s3_object.bundle["frontend"].key
      internal_alb_dns = aws_lb.internal.dns_name
      frontend_config  = local.frontend_config
      log_group        = aws_cloudwatch_log_group.service["frontend"].name
    })
  }
  backend = {
    security_group = aws_security_group.backend_app.id
    user_data = templatefile("${path.module}/templates/python_service.sh.tftpl", {
      aws_region   = var.aws_region
      bucket_name  = aws_s3_bucket.artifacts.bucket
      bundle_key   = aws_s3_object.bundle["backend"].key
      service      = "backend"
      package_dir  = "app/backend_api"
      exec_start   = "venv/bin/uvicorn app.backend_api.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers"
      environment  = local.backend_environment
      log_group    = aws_cloudwatch_log_group.service["backend"].name
      requirements = "app/backend_api/requirements.txt"
    })
  }
  outbox = {
    security_group = aws_security_group.outbox_workers.id
    user_data = templatefile("${path.module}/templates/python_service.sh.tftpl", {
      aws_region   = var.aws_region
      bucket_name  = aws_s3_bucket.artifacts.bucket
      bundle_key   = aws_s3_object.bundle["workers"].key
      service      = "outbox-worker"
      package_dir  = "app/workers"
      exec_start   = "venv/bin/python -m app.workers.outbox_worker"
      environment  = local.worker_environment.outbox
      log_group    = aws_cloudwatch_log_group.service["outbox-worker"].name
      requirements = "app/workers/requirements.txt"
    })
  }
  statement = {
    security_group = aws_security_group.statement_workers.id
    user_data = templatefile("${path.module}/templates/python_service.sh.tftpl", {
      aws_region   = var.aws_region
      bucket_name  = aws_s3_bucket.artifacts.bucket
      bundle_key   = aws_s3_object.bundle["workers"].key
      service      = "statement-worker"
      package_dir  = "app/workers"
      exec_start   = "venv/bin/python -m app.workers.statement_worker"
      environment  = local.worker_environment.statement
      log_group    = aws_cloudwatch_log_group.service["statement-worker"].name
      requirements = "app/workers/requirements.txt"
    })
  }
}
}

resource "aws_launch_template" "service" {
  for_each = local.launch_templates

  name_prefix            = "facaibank-${var.env}-${each.key}-"
  image_id               = data.aws_ssm_parameter.amazon_linux_2023.value
  instance_type          = var.app_instance_type
  update_default_version = true
  user_data              = base64encode(each.value.user_data)

  iam_instance_profile {
    name = aws_iam_instance_profile.instance[each.key].name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [each.value.security_group]
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 8
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.default_tags, {
      Name = "facaibank-${var.env}-${each.key}"
      Tier = "app"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.default_tags, {
      Name = "facaibank-${var.env}-${each.key}"
    })
  }
}

# ---------------------------------------------------------------- auto scaling groups

locals {
  fleets = {
    frontend  = { target_groups = [aws_lb_target_group.frontend.arn], health_check = "ELB", name = "frontend" }
    backend   = { target_groups = [aws_lb_target_group.backend.arn], health_check = "ELB", name = "backend" }
    outbox    = { target_groups = [], health_check = "EC2", name = "outbox-workers" }
    statement = { target_groups = [], health_check = "EC2", name = "statement-workers" }
  }
}

resource "aws_autoscaling_group" "fleet" {
  for_each = local.fleets

  name                      = "facaibank-${var.env}-${each.value.name}"
  min_size                  = var.asg_min_size
  max_size                  = var.asg_max_size
  desired_capacity          = var.asg_desired_capacity
  vpc_zone_identifier       = aws_subnet.private_app[*].id
  health_check_type         = each.value.health_check
  health_check_grace_period = 300
  target_group_arns         = each.value.target_groups
  default_instance_warmup   = 180
  wait_for_capacity_timeout = "15m"
  force_delete              = true
  enabled_metrics           = ["GroupDesiredCapacity", "GroupInServiceInstances", "GroupPendingInstances", "GroupTotalInstances"]

  launch_template {
    id      = aws_launch_template.service[each.key].id
    version = aws_launch_template.service[each.key].latest_version
  }

  # Launch replacements before terminating old instances, so even a one-instance
  # fleet deploys without downtime. Triggered by every new launch template version.
  instance_refresh {
    strategy = "Rolling"

    preferences {
      min_healthy_percentage = 100
      max_healthy_percentage = 200
      instance_warmup        = 180
      auto_rollback          = true
    }
  }

  dynamic "tag" {
    for_each = merge(local.default_tags, {
      Name = "facaibank-${var.env}-${each.value.name}"
      Tier = "app"
    })
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }

  depends_on = [
    aws_iam_role_policy.frontend_instance,
    aws_iam_role_policy.backend_instance,
    aws_iam_role_policy.outbox_instance,
    aws_iam_role_policy.statement_instance,
  ]
}

# ---------------------------------------------------------------- scaling policies (slide 7)

resource "aws_autoscaling_policy" "requests_per_target" {
  for_each = {
    frontend = { lb = aws_lb.public, tg = aws_lb_target_group.frontend, target = var.frontend_requests_per_target }
    backend  = { lb = aws_lb.internal, tg = aws_lb_target_group.backend, target = var.backend_requests_per_target }
  }

  name                   = "facaibank-${var.env}-${each.key}-requests-per-target"
  autoscaling_group_name = aws_autoscaling_group.fleet[each.key].name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value = each.value.target

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${each.value.lb.arn_suffix}/${each.value.tg.arn_suffix}"
    }
  }
}

resource "aws_autoscaling_policy" "cpu" {
  for_each = toset(["frontend", "backend"])

  name                   = "facaibank-${var.env}-${each.key}-cpu"
  autoscaling_group_name = aws_autoscaling_group.fleet[each.key].name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value = var.cpu_target_percent

    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
  }
}

resource "aws_autoscaling_policy" "outbox_iterator_age" {
  name                   = "facaibank-${var.env}-outbox-iterator-age"
  autoscaling_group_name = aws_autoscaling_group.fleet["outbox"].name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value = var.outbox_iterator_age_target_ms

    customized_metric_specification {
      metric_name = "OutboxIteratorAgeMilliseconds"
      namespace   = "FaCaiBank/Workers"
      statistic   = "Maximum"
      unit        = "Milliseconds"

      metric_dimension {
        name  = "Environment"
        value = var.env
      }

      metric_dimension {
        name  = "Service"
        value = "outbox"
      }
    }
  }
}

resource "aws_autoscaling_policy" "statement_backlog" {
  name                   = "facaibank-${var.env}-statement-backlog-per-instance"
  autoscaling_group_name = aws_autoscaling_group.fleet["statement"].name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value = var.statement_backlog_per_instance

    customized_metric_specification {
      metrics {
        id          = "backlog"
        label       = "Statement jobs waiting"
        return_data = false

        metric_stat {
          stat = "Sum"

          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"

            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.statement_jobs.name
            }
          }
        }
      }

      metrics {
        id          = "workers"
        label       = "Statement workers in service"
        return_data = false

        metric_stat {
          stat = "Average"

          metric {
            namespace   = "AWS/AutoScaling"
            metric_name = "GroupInServiceInstances"

            dimensions {
              name  = "AutoScalingGroupName"
              value = aws_autoscaling_group.fleet["statement"].name
            }
          }
        }
      }

      metrics {
        id          = "backlog_per_worker"
        label       = "Statement jobs per worker"
        expression  = "backlog / workers"
        return_data = true
      }
    }
  }
}
