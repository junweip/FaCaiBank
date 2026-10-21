resource "aws_db_subnet_group" "main" {
  name       = "facaibank-${var.env}-data"
  subnet_ids = aws_subnet.private_data[*].id

  tags = {
    Name = "facaibank-${var.env}-data"
    Tier = "data"
  }
}

resource "aws_db_instance" "main" {
  identifier                    = "facaibank-${var.env}-postgres"
  engine                        = "postgres"
  instance_class                = var.db_instance_class
  allocated_storage             = var.db_allocated_storage
  db_name                       = var.db_name
  username                      = var.db_username
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn
  db_subnet_group_name          = aws_db_subnet_group.main.name
  vpc_security_group_ids        = [aws_security_group.rds.id]
  multi_az                      = var.rds_multi_az
  publicly_accessible           = false
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.main.arn
  backup_retention_period       = 7
  auto_minor_version_upgrade    = true
  deletion_protection           = false
  skip_final_snapshot           = true
  copy_tags_to_snapshot         = true

  tags = {
    Name = "facaibank-${var.env}-postgres"
    Tier = "data"
  }
}

resource "aws_dynamodb_table" "ledger" {
  name         = "facaibank-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = {
    Name = "facaibank-ledger"
    Tier = "data"
  }
}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  kyc_bucket   = "facaibank-${var.env}-kyc-statements-${local.account_id}-${var.aws_region}"
  audit_bucket = "facaibank-${var.env}-cloudtrail-audit-${local.account_id}-${var.aws_region}"
}

resource "aws_s3_bucket" "kyc_statements" {
  bucket = local.kyc_bucket

  tags = {
    Name = "facaibank-${var.env}-kyc-statements"
    Tier = "data"
  }
}

resource "aws_s3_bucket_public_access_block" "kyc_statements" {
  bucket                  = aws_s3_bucket.kyc_statements.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kyc_statements" {
  bucket = aws_s3_bucket.kyc_statements.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "kyc_statements" {
  bucket = aws_s3_bucket.kyc_statements.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "kyc_statements" {
  bucket = aws_s3_bucket.kyc_statements.id

  rule {
    id     = "archive-after-five-years"
    status = "Enabled"

    filter {}

    transition {
      days          = 1825
      storage_class = "GLACIER"
    }
  }
}

resource "aws_s3_bucket" "audit" {
  bucket              = local.audit_bucket
  object_lock_enabled = true

  tags = {
    Name = "facaibank-${var.env}-cloudtrail-audit"
    Tier = "audit"
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 1825
    }
  }

  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "retain-five-years-in-deep-archive"
    status = "Enabled"

    filter {}

    transition {
      days          = 1
      storage_class = "DEEP_ARCHIVE"
    }

    expiration {
      days = 1825
    }
  }
}

data "aws_iam_policy_document" "audit_bucket" {
  statement {
    sid    = "AllowCloudTrailBucketAclCheck"
    effect = "Allow"

    actions = ["s3:GetBucketAcl"]
    resources = [
      aws_s3_bucket.audit.arn,
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/*"]
    }
  }

  statement {
    sid    = "AllowCloudTrailLogDelivery"
    effect = "Allow"

    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.audit.arn}/AWSLogs/${local.account_id}/*",
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/*"]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit_bucket.json
}

data "aws_iam_policy_document" "kyc_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.kyc_statements.arn, "${aws_s3_bucket.kyc_statements.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "kyc_statements" {
  bucket = aws_s3_bucket.kyc_statements.id
  policy = data.aws_iam_policy_document.kyc_bucket.json
}
