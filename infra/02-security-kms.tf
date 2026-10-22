locals {
  account_id = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "cmk" {
  statement {
    sid       = "EnableAccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
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
        values   = [local.account_id]
      }

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = [statement.value.via_service]
      }
    }
  }

  statement {
    sid       = "AllowCloudTrailLogEncryption"
    actions   = ["kms:GenerateDataKey*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${local.account_id}:trail/*"]
    }
  }

  statement {
    sid       = "AllowCloudTrailDescribeKey"
    actions   = ["kms:DescribeKey"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }

  statement {
    sid = "AllowCloudWatchLogsEncryption"
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:*"]
    }
  }

  statement {
    sid       = "AllowCloudFrontToReadStaticAssets"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudfront::${local.account_id}:distribution/*"]
    }
  }

  statement {
    sid       = "AllowMacieToScanEncryptedObjects"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/aws-service-role/macie.amazonaws.com/AWSServiceRoleForAmazonMacie"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "AllowAwsResourceGrants"
    actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
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

# Name and description are kept from the first release: changing either forces
# replacement of a security group that is attached to running resources.
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

resource "aws_vpc_security_group_ingress_rule" "rds_from_backend" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.backend_app.id
  description                  = "PostgreSQL from the API fleet."
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_statement_workers" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.statement_workers.id
  description                  = "PostgreSQL (read replica) from statement workers."
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
