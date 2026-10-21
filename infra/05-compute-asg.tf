data "aws_ssm_parameter" "amazon_linux_2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "archive_file" "frontend_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/frontend"
  output_path = "${path.root}/.terraform/frontend_bundle.zip"
}

data "archive_file" "backend_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/backend_api"
  output_path = "${path.root}/.terraform/backend_bundle.zip"
  excludes    = ["__pycache__/*", "*.pyc"]
}

data "archive_file" "workers_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../app/workers"
  output_path = "${path.root}/.terraform/workers_bundle.zip"
  excludes    = ["__pycache__/*", "*.pyc"]
}

resource "aws_s3_object" "frontend_bundle" {
  bucket                 = aws_s3_bucket.kyc_statements.bucket
  key                    = "deploy/${var.env}/frontend_bundle.zip"
  source                 = data.archive_file.frontend_bundle.output_path
  source_hash            = data.archive_file.frontend_bundle.output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = {
    Name = "facaibank-${var.env}-frontend-deployment"
    Tier = "deployment"
  }
}

resource "aws_s3_object" "backend_bundle" {
  bucket                 = aws_s3_bucket.kyc_statements.bucket
  key                    = "deploy/${var.env}/backend_bundle.zip"
  source                 = data.archive_file.backend_bundle.output_path
  source_hash            = data.archive_file.backend_bundle.output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = {
    Name = "facaibank-${var.env}-backend-deployment"
    Tier = "deployment"
  }
}

resource "aws_s3_object" "workers_bundle" {
  bucket                 = aws_s3_bucket.kyc_statements.bucket
  key                    = "deploy/${var.env}/workers_bundle.zip"
  source                 = data.archive_file.workers_bundle.output_path
  source_hash            = data.archive_file.workers_bundle.output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = {
    Name = "facaibank-${var.env}-workers-deployment"
    Tier = "deployment"
  }
}

resource "aws_security_group" "public_alb" {
  name        = "facaibank-${var.env}-public-alb"
  description = "Public HTTP ingress for the FaCaiBank frontend."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from the internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
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

resource "aws_security_group" "workers" {
  name        = "facaibank-${var.env}-workers"
  description = "Outbound-only security group for queue and stream workers."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-workers"
    Tier = "app"
  }
}

resource "aws_lb" "public" {
  name                       = "facaibank-${var.env}-public"
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.public_alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = false

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

  tags = {
    Name = "facaibank-${var.env}-internal-alb"
    Tier = "app"
  }
}

resource "aws_lb_target_group" "frontend" {
  name        = "facaibank-${var.env}-frontend"
  port        = 8080
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = aws_vpc.main.id

  health_check {
    enabled             = true
    path                = "/"
    protocol            = "HTTP"
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "facaibank-${var.env}-frontend"
    Tier = "app"
  }
}

resource "aws_lb_target_group" "backend" {
  name        = "facaibank-${var.env}-backend"
  port        = 8000
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = aws_vpc.main.id

  health_check {
    enabled             = true
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
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
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
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

resource "aws_cognito_user_pool" "main" {
  name                     = "facaibank-${var.env}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
  }

  tags = {
    Name = "facaibank-${var.env}-users"
    Tier = "security"
  }
}

resource "aws_cognito_user_pool_client" "frontend" {
  name                          = "facaibank-${var.env}-frontend"
  user_pool_id                  = aws_cognito_user_pool.main.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  access_token_validity         = 60
  id_token_validity             = 60
  refresh_token_validity        = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "facaibank-${var.env}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_iam_role" "backend_instance" {
  name = "facaibank-${var.env}-backend-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "facaibank-${var.env}-backend-instance"
    Tier = "app"
  }
}

resource "aws_iam_role_policy" "backend_instance" {
  name = "facaibank-${var.env}-backend-access"
  role = aws_iam_role.backend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.kyc_statements.arn}/deploy/${var.env}/backend_bundle.zip"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.kyc_statements.arn}/kyc/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_db_instance.main.master_user_secret[0].secret_arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:TransactWriteItems",
        ]
        Resource = aws_dynamodb_table.ledger.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.statement_jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey",
        ]
        Resource = aws_kms_key.main.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "backend" {
  name = "facaibank-${var.env}-backend"
  role = aws_iam_role.backend_instance.name
}

resource "aws_iam_role" "worker_instance" {
  name = "facaibank-${var.env}-worker-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "facaibank-${var.env}-worker-instance"
    Tier = "app"
  }
}

resource "aws_iam_role_policy" "worker_instance" {
  name = "facaibank-${var.env}-worker-access"
  role = aws_iam_role.worker_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.kyc_statements.arn}/deploy/${var.env}/workers_bundle.zip"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable",
          "dynamodb:Query",
          "dynamodb:PutItem",
        ]
        Resource = [
          aws_dynamodb_table.ledger.arn,
          "${aws_dynamodb_table.ledger.arn}/stream/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodbstreams:DescribeStream",
          "dynamodbstreams:GetShardIterator",
          "dynamodbstreams:GetRecords",
        ]
        Resource = "${aws_dynamodb_table.ledger.arn}/stream/*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility",
          "sqs:SendMessage",
        ]
        Resource = [
          aws_sqs_queue.events.arn,
          aws_sqs_queue.statement_jobs.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
        ]
        Resource = "${aws_s3_bucket.kyc_statements.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey",
        ]
        Resource = aws_kms_key.main.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "workers" {
  name = "facaibank-${var.env}-workers"
  role = aws_iam_role.worker_instance.name
}

resource "aws_launch_template" "frontend" {
  name_prefix   = "facaibank-${var.env}-frontend-"
  image_id      = data.aws_ssm_parameter.amazon_linux_2023.value
  instance_type = var.app_instance_type
  user_data = base64encode(templatefile("${path.module}/templates/frontend.sh.tftpl", {
    aws_region       = var.aws_region
    bucket_name      = aws_s3_bucket.kyc_statements.bucket
    bundle_key       = aws_s3_object.frontend_bundle.key
    internal_alb_dns = aws_lb.internal.dns_name
  }))

  iam_instance_profile {
    name = aws_iam_instance_profile.frontend.name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.frontend.id]
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "facaibank-${var.env}-frontend"
      Tier = "app"
    }
  }
}

resource "aws_iam_role" "frontend_instance" {
  name = "facaibank-${var.env}-frontend-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "facaibank-${var.env}-frontend-instance"
    Tier = "app"
  }
}

resource "aws_iam_role_policy" "frontend_instance" {
  name = "facaibank-${var.env}-frontend-access"
  role = aws_iam_role.frontend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.kyc_statements.arn}/deploy/${var.env}/frontend_bundle.zip"
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.main.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "frontend" {
  name = "facaibank-${var.env}-frontend"
  role = aws_iam_role.frontend_instance.name
}

resource "aws_launch_template" "backend" {
  name_prefix   = "facaibank-${var.env}-backend-"
  image_id      = data.aws_ssm_parameter.amazon_linux_2023.value
  instance_type = var.app_instance_type
  user_data = base64encode(templatefile("${path.module}/templates/backend.sh.tftpl", {
    aws_region          = var.aws_region
    bucket_name         = aws_s3_bucket.kyc_statements.bucket
    bundle_key          = aws_s3_object.backend_bundle.key
    database_host       = aws_db_instance.main.address
    database_name       = var.db_name
    database_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn
    ledger_table_name   = aws_dynamodb_table.ledger.name
    statement_queue_url = aws_sqs_queue.statement_jobs.url
  }))

  iam_instance_profile {
    name = aws_iam_instance_profile.backend.name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.backend_app.id]
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "facaibank-${var.env}-backend"
      Tier = "app"
    }
  }
}

resource "aws_launch_template" "workers" {
  name_prefix   = "facaibank-${var.env}-workers-"
  image_id      = data.aws_ssm_parameter.amazon_linux_2023.value
  instance_type = var.app_instance_type
  user_data = base64encode(templatefile("${path.module}/templates/workers.sh.tftpl", {
    aws_region          = var.aws_region
    bucket_name         = aws_s3_bucket.kyc_statements.bucket
    bundle_key          = aws_s3_object.workers_bundle.key
    ledger_table_name   = aws_dynamodb_table.ledger.name
    events_queue_url    = aws_sqs_queue.events.url
    statement_queue_url = aws_sqs_queue.statement_jobs.url
  }))

  iam_instance_profile {
    name = aws_iam_instance_profile.workers.name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.workers.id]
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "facaibank-${var.env}-workers"
      Tier = "app"
    }
  }
}

resource "aws_autoscaling_group" "frontend" {
  name                      = "facaibank-${var.env}-frontend"
  min_size                  = var.asg_min_size
  max_size                  = var.asg_max_size
  desired_capacity          = var.asg_desired_capacity
  vpc_zone_identifier       = aws_subnet.private_app[*].id
  health_check_type         = "ELB"
  health_check_grace_period = 300
  target_group_arns         = [aws_lb_target_group.frontend.arn]
  force_delete              = true

  launch_template {
    id      = aws_launch_template.frontend.id
    version = "$Latest"
  }

  dynamic "tag" {
    for_each = {
      Project     = "FaCaiBank"
      Environment = var.env
      Owner       = "Group3"
      Name        = "facaibank-${var.env}-frontend"
      Tier        = "app"
    }
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  depends_on = [aws_iam_role_policy.frontend_instance]
}

resource "aws_autoscaling_group" "backend" {
  name                      = "facaibank-${var.env}-backend"
  min_size                  = var.asg_min_size
  max_size                  = var.asg_max_size
  desired_capacity          = var.asg_desired_capacity
  vpc_zone_identifier       = aws_subnet.private_app[*].id
  health_check_type         = "ELB"
  health_check_grace_period = 300
  target_group_arns         = [aws_lb_target_group.backend.arn]
  force_delete              = true

  launch_template {
    id      = aws_launch_template.backend.id
    version = "$Latest"
  }

  dynamic "tag" {
    for_each = {
      Project     = "FaCaiBank"
      Environment = var.env
      Owner       = "Group3"
      Name        = "facaibank-${var.env}-backend"
      Tier        = "app"
    }
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  depends_on = [aws_iam_role_policy.backend_instance]
}

resource "aws_autoscaling_group" "workers" {
  name                      = "facaibank-${var.env}-workers"
  min_size                  = var.asg_min_size
  max_size                  = var.asg_max_size
  desired_capacity          = var.asg_desired_capacity
  vpc_zone_identifier       = aws_subnet.private_app[*].id
  health_check_type         = "EC2"
  health_check_grace_period = 300
  force_delete              = true

  launch_template {
    id      = aws_launch_template.workers.id
    version = "$Latest"
  }

  dynamic "tag" {
    for_each = {
      Project     = "FaCaiBank"
      Environment = var.env
      Owner       = "Group3"
      Name        = "facaibank-${var.env}-workers"
      Tier        = "app"
    }
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  depends_on = [aws_iam_role_policy.worker_instance]
}

resource "aws_wafv2_web_acl" "public" {
  name        = "facaibank-${var.env}-public"
  description = "Rate limiting and managed SQL injection protection for the public ALB."
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "RateLimitPerIp"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = 2000
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "facaibank-${var.env}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedSQLiRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesSQLiRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "facaibank-${var.env}-sqli"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "facaibank-${var.env}-public-waf"
    sampled_requests_enabled   = true
  }

  tags = {
    Name = "facaibank-${var.env}-public-waf"
    Tier = "security"
  }
}

resource "aws_wafv2_web_acl_association" "public_alb" {
  resource_arn = aws_lb.public.arn
  web_acl_arn  = aws_wafv2_web_acl.public.arn
}
