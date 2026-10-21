data "aws_iam_policy_document" "cmk" {
  statement {
    sid       = "EnableAccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  dynamic "statement" {
    for_each = {
      rds = {
        principal   = "rds.amazonaws.com"
        via_service = "rds.${var.aws_region}.amazonaws.com"
      }
      s3 = {
        principal   = "s3.amazonaws.com"
        via_service = "s3.${var.aws_region}.amazonaws.com"
      }
      dynamodb = {
        principal   = "dynamodb.amazonaws.com"
        via_service = "dynamodb.${var.aws_region}.amazonaws.com"
      }
      sqs = {
        principal   = "sqs.amazonaws.com"
        via_service = "sqs.${var.aws_region}.amazonaws.com"
      }
      secretsmanager = {
        principal   = "secretsmanager.amazonaws.com"
        via_service = "secretsmanager.${var.aws_region}.amazonaws.com"
      }
      sns = {
        principal   = "sns.amazonaws.com"
        via_service = "sns.${var.aws_region}.amazonaws.com"
      }
    }

    content {
      sid = "Allow${title(statement.key)}ServiceUse"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
      ]
      resources = ["*"]

      principals {
        type        = "Service"
        identifiers = [statement.value.principal]
      }

      condition {
        test     = "StringEquals"
        variable = "kms:CallerAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = [statement.value.via_service]
      }
    }
  }

  statement {
    sid = "AllowCloudTrailLogEncryption"
    actions = [
      "kms:DescribeKey",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "AllowAwsResourceGrants"
    actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "main" {
  description             = "FaCaiBank ${var.env} customer-managed encryption key."
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.cmk.json

  tags = {
    Name = "facaibank-${var.env}-cmk"
  }
}

resource "aws_kms_alias" "main" {
  name          = "alias/facaibank-${var.env}"
  target_key_id = aws_kms_key.main.key_id
}

resource "aws_security_group" "backend_app" {
  name        = "facaibank-${var.env}-backend-app"
  description = "Security group for future backend application workloads."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-backend-app"
    Tier = "app"
  }
}

resource "aws_security_group" "rds" {
  name        = "facaibank-${var.env}-rds"
  description = "Restrict PostgreSQL access to backend application workloads."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from backend application workloads"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_app.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "facaibank-${var.env}-rds"
    Tier = "data"
  }
}
