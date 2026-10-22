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
  backup_retention_period       = var.db_backup_retention_days
  backup_window                 = "18:00-19:00"
  maintenance_window            = "sun:19:30-sun:20:30"
  auto_minor_version_upgrade    = true
  deletion_protection           = var.deletion_protection
  skip_final_snapshot           = !var.deletion_protection
  final_snapshot_identifier     = var.deletion_protection ? "facaibank-${var.env}-postgres-final" : null
  copy_tags_to_snapshot         = true
  apply_immediately             = true

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

  stream_enabled              = true
  stream_view_type            = "NEW_IMAGE"
  deletion_protection_enabled = var.deletion_protection

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  # Continuous backups: restore to any second in the last 35 days (RPO 0 for
  # committed writes) and the source for analytics exports.
  point_in_time_recovery {
    enabled = true
  }

  # Idempotency, dedupe and outbox records expire; ledger entries never set it.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name = "facaibank-ledger"
    Tier = "data"
  }
}

resource "aws_dynamodb_table" "outbox_leases" {
  name         = "facaibank-${var.env}-outbox-leases"
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

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "facaibank-${var.env}-outbox-leases"
    Tier = "data"
  }
}

locals {
  kyc_bucket       = "facaibank-${var.env}-kyc-statements-${local.account_id}-${var.aws_region}"
  audit_bucket     = "facaibank-${var.env}-cloudtrail-audit-${local.account_id}-${var.aws_region}"
  artifacts_bucket = "facaibank-${var.env}-artifacts-${local.account_id}-${var.aws_region}"
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
      days          = var.audit_retention_days
      storage_class = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
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
      mode = var.audit_object_lock_mode
      days = var.audit_retention_days
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
      days          = 90
      storage_class = "DEEP_ARCHIVE"
    }

    expiration {
      days = var.audit_retention_days
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
    sid       = "AllowConfigBucketChecks"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn]

    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "AllowConfigDelivery"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/config/AWSLogs/${local.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
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

# Deployment bundles, Glue scripts and model artifacts are kept apart from
# customer KYC data so instance roles never need access to the KYC bucket root.
resource "aws_s3_bucket" "artifacts" {
  bucket        = local.artifacts_bucket
  force_destroy = true

  tags = {
    Name = "facaibank-${var.env}-artifacts"
    Tier = "deployment"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-old-bundles"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
