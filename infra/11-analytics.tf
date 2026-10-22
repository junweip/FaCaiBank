# Read-only analytics zone off the request path (slide 10):
# DynamoDB PITR export (daily, no load on the live table) -> S3 lake (SSE-KMS)
# -> Glue Python shell job (Parquet) -> Glue crawler/catalog -> Athena -> QuickSight.

locals {
  lake_bucket = "facaibank-${var.env}-analytics-${local.account_id}-${var.aws_region}"
  athena_queries = {
    transfer-volume-by-hour = <<-SQL
      SELECT txn_date, txn_hour_sgt, COUNT(*) AS transfers, SUM(amount) AS sgd_moved
      FROM ledger_entries
      WHERE transaction_type = 'TRANSFER' AND entry_type = 'DEBIT' AND NOT is_clearing_account
      GROUP BY txn_date, txn_hour_sgt
      ORDER BY txn_date DESC, txn_hour_sgt
    SQL
    fraud-score-trend       = <<-SQL
      SELECT txn_date, fraud_model, COUNT(*) AS scored, AVG(fraud_score) AS avg_score,
             approx_percentile(fraud_score, 0.95) AS p95_score,
             SUM(CASE WHEN fraud_score >= 0.6 THEN 1 ELSE 0 END) AS step_up_band
      FROM ledger_entries
      WHERE fraud_score IS NOT NULL
      GROUP BY txn_date, fraud_model
      ORDER BY txn_date DESC
    SQL
    month-end-capacity      = <<-SQL
      SELECT txn_date, MAX(per_hour) AS peak_hour_transactions, MAX(per_hour) / 3600.0 AS peak_tps
      FROM (
        SELECT txn_date, txn_hour_sgt, COUNT(DISTINCT transaction_id) AS per_hour
        FROM ledger_entries
        WHERE NOT is_clearing_account
        GROUP BY txn_date, txn_hour_sgt
      )
      GROUP BY txn_date
      ORDER BY peak_hour_transactions DESC
      LIMIT 31
    SQL
  }
}

resource "aws_s3_bucket" "lake" {
  count = var.enable_analytics ? 1 : 0

  bucket        = local.lake_bucket
  force_destroy = true

  tags = {
    Name = "facaibank-${var.env}-analytics-lake"
    Tier = "analytics"
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  count = var.enable_analytics ? 1 : 0

  bucket                  = aws_s3_bucket.lake[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  count = var.enable_analytics ? 1 : 0

  bucket = aws_s3_bucket.lake[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  count = var.enable_analytics ? 1 : 0

  bucket = aws_s3_bucket.lake[0].id

  rule {
    id     = "expire-raw-exports"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    expiration {
      days = 7
    }
  }

  rule {
    id     = "expire-query-results"
    status = "Enabled"

    filter {
      prefix = "athena-results/"
    }

    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "lake" {
  count = var.enable_analytics ? 1 : 0

  bucket = aws_s3_bucket.lake[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.lake[0].arn, "${aws_s3_bucket.lake[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ---------------------------------------------------------------- daily export (Scheduler universal target)

resource "aws_iam_role" "analytics_export" {
  count = var.enable_analytics ? 1 : 0

  name = "facaibank-${var.env}-analytics-export"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "analytics_export" {
  count = var.enable_analytics ? 1 : 0

  name = "export-ledger"
  role = aws_iam_role.analytics_export[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "dynamodb:ExportTableToPointInTime", Resource = aws_dynamodb_table.ledger.arn },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:PutObjectAcl"]
        Resource = "${aws_s3_bucket.lake[0].arn}/raw/*"
      },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_scheduler_schedule" "analytics_export" {
  count = var.enable_analytics ? 1 : 0

  name                         = "facaibank-${var.env}-ledger-export"
  description                  = "Daily point-in-time export of the ledger to the analytics lake (no read load on the table)."
  schedule_expression          = "cron(0 1 * * ? *)"
  schedule_expression_timezone = "Asia/Singapore"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:dynamodb:exportTableToPointInTime"
    role_arn = aws_iam_role.analytics_export[0].arn
    input = jsonencode({
      TableArn       = aws_dynamodb_table.ledger.arn
      S3Bucket       = aws_s3_bucket.lake[0].bucket
      S3Prefix       = "raw/"
      ExportFormat   = "DYNAMODB_JSON"
      S3SseAlgorithm = "KMS"
      S3SseKmsKeyId  = aws_kms_key.main.arn
      S3BucketOwner  = local.account_id
    })
  }
}

# ---------------------------------------------------------------- Glue job, catalog and crawler

resource "aws_s3_object" "glue_script" {
  count = var.enable_analytics ? 1 : 0

  bucket                 = aws_s3_bucket.artifacts.bucket
  key                    = "glue/ledger_to_parquet-${filemd5("${path.module}/../app/analytics/ledger_to_parquet.py")}.py"
  source                 = "${path.module}/../app/analytics/ledger_to_parquet.py"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn
}

resource "aws_iam_role" "glue" {
  count = var.enable_analytics ? 1 : 0

  name = "facaibank-${var.env}-glue"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "glue.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  count = var.enable_analytics ? 1 : 0

  role       = aws_iam_role.glue[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue" {
  count = var.enable_analytics ? 1 : 0

  name = "lake-access"
  role = aws_iam_role.glue[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.lake[0].arn },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.lake[0].arn}/*"
      },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/glue/*" },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_glue_catalog_database" "analytics" {
  count = var.enable_analytics ? 1 : 0

  name        = "facaibank_${var.env}_analytics"
  description = "Read-only analytics copy of the FaCaiBank ledger."
}

resource "aws_glue_security_configuration" "analytics" {
  count = var.enable_analytics ? 1 : 0

  name = "facaibank-${var.env}-analytics"

  encryption_configuration {
    cloudwatch_encryption {
      cloudwatch_encryption_mode = "DISABLED"
    }

    job_bookmarks_encryption {
      job_bookmarks_encryption_mode = "DISABLED"
    }

    s3_encryption {
      s3_encryption_mode = "SSE-KMS"
      kms_key_arn        = aws_kms_key.main.arn
    }
  }
}

resource "aws_glue_job" "ledger_to_parquet" {
  count = var.enable_analytics ? 1 : 0

  name                   = "facaibank-${var.env}-ledger-to-parquet"
  description            = "Flatten the latest DynamoDB export into partitioned Parquet."
  role_arn               = aws_iam_role.glue[0].arn
  glue_version           = "3.0"
  max_capacity           = 0.0625
  timeout                = 30
  security_configuration = aws_glue_security_configuration.analytics[0].name

  command {
    name            = "pythonshell"
    python_version  = "3.9"
    script_location = "s3://${aws_s3_object.glue_script[0].bucket}/${aws_s3_object.glue_script[0].key}"
  }

  default_arguments = {
    "library-set"      = "analytics"
    "--LAKE_BUCKET"    = aws_s3_bucket.lake[0].bucket
    "--RAW_PREFIX"     = "raw/"
    "--CURATED_PREFIX" = "curated/ledger_entries/"
    "--KMS_KEY_ARN"    = aws_kms_key.main.arn
  }
}

resource "aws_glue_trigger" "ledger_to_parquet" {
  count = var.enable_analytics ? 1 : 0

  name     = "facaibank-${var.env}-ledger-to-parquet-daily"
  type     = "SCHEDULED"
  schedule = "cron(0 19 * * ? *)" # 03:00 SGT, two hours after the export starts

  actions {
    job_name = aws_glue_job.ledger_to_parquet[0].name
  }
}

resource "aws_glue_crawler" "ledger" {
  count = var.enable_analytics ? 1 : 0

  name                   = "facaibank-${var.env}-ledger-crawler"
  database_name          = aws_glue_catalog_database.analytics[0].name
  role                   = aws_iam_role.glue[0].arn
  schedule               = "cron(30 19 * * ? *)"
  security_configuration = aws_glue_security_configuration.analytics[0].name

  s3_target {
    path = "s3://${aws_s3_bucket.lake[0].bucket}/curated/ledger_entries/"
  }

  configuration = jsonencode({
    Version  = 1.0
    Grouping = { TableGroupingPolicy = "CombineCompatibleSchemas" }
  })
}

# ---------------------------------------------------------------- Athena and QuickSight

resource "aws_athena_workgroup" "analytics" {
  count = var.enable_analytics ? 1 : 0

  name          = "facaibank-${var.env}-analytics"
  description   = "Risk and capacity analytics over the ledger lake."
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 1073741824

    result_configuration {
      output_location = "s3://${aws_s3_bucket.lake[0].bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = aws_kms_key.main.arn
      }
    }
  }
}

resource "aws_athena_named_query" "analytics" {
  for_each = var.enable_analytics ? local.athena_queries : {}

  name      = each.key
  workgroup = aws_athena_workgroup.analytics[0].name
  database  = aws_glue_catalog_database.analytics[0].name
  query     = each.value
}

resource "aws_quicksight_data_source" "athena" {
  count = var.enable_analytics && var.quicksight_user_arn != "" ? 1 : 0

  data_source_id = "facaibank-${var.env}-athena"
  name           = "FaCaiBank ${var.env} ledger (Athena)"
  type           = "ATHENA"

  parameters {
    athena {
      work_group = aws_athena_workgroup.analytics[0].name
    }
  }

  permission {
    principal = var.quicksight_user_arn
    actions = [
      "quicksight:DescribeDataSource",
      "quicksight:DescribeDataSourcePermissions",
      "quicksight:PassDataSource",
      "quicksight:UpdateDataSource",
      "quicksight:DeleteDataSource",
      "quicksight:UpdateDataSourcePermissions",
    ]
  }
}
