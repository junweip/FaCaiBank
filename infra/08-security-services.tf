# Audit and detection (slide 11): CloudTrail into the Object Lock audit bucket,
# security-change alarms from the trail, and GuardDuty, Security Hub, Macie and
# AWS Config. GuardDuty, Security Hub, Macie and Config are account-level
# singletons: disable the matching variable if the account already has them.

# ---------------------------------------------------------------- CloudTrail

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  name              = "/facaibank/${var.env}/cloudtrail"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_iam_role" "cloudtrail_logs" {
  count = var.enable_cloudtrail ? 1 : 0

  name = "facaibank-${var.env}-cloudtrail-logs"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "cloudtrail_logs" {
  count = var.enable_cloudtrail ? 1 : 0

  name = "deliver-to-cloudwatch"
  role = aws_iam_role.cloudtrail_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"
    }]
  })
}

resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail ? 1 : 0

  name                          = "facaibank-${var.env}-trail"
  s3_bucket_name                = aws_s3_bucket.audit.id
  kms_key_id                    = aws_kms_key.main.arn
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  cloud_watch_logs_group_arn    = "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"
  cloud_watch_logs_role_arn     = aws_iam_role.cloudtrail_logs[0].arn

  event_selector {
    read_write_type           = "All"
    include_management_events = true

    # Object-level access to KYC documents and statements (who read what, when).
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${aws_s3_bucket.kyc_statements.arn}/"]
    }
  }

  depends_on = [aws_s3_bucket_policy.audit, aws_iam_role_policy.cloudtrail_logs]
}

locals {
  security_metric_filters = {
    root-account-usage = {
      pattern     = "{ $.userIdentity.type = \"Root\" && $.userIdentity.invokedBy NOT EXISTS && $.eventType != \"AwsServiceEvent\" }"
      description = "The root user signed in or made an API call."
    }
    iam-policy-changes = {
      pattern     = "{ ($.eventSource = \"iam.amazonaws.com\") && (($.eventName = Create*) || ($.eventName = Delete*) || ($.eventName = Put*) || ($.eventName = Attach*) || ($.eventName = Detach*) || ($.eventName = Update*) || ($.eventName = AddUserToGroup) || ($.eventName = RemoveUserFromGroup)) }"
      description = "An IAM user, role, group or policy was changed."
    }
    kms-key-changes = {
      pattern     = "{ ($.eventSource = \"kms.amazonaws.com\") && (($.eventName = DisableKey) || ($.eventName = ScheduleKeyDeletion) || ($.eventName = PutKeyPolicy) || ($.eventName = CancelKeyDeletion) || ($.eventName = DisableKeyRotation)) }"
      description = "A KMS key was disabled, scheduled for deletion or had its policy changed."
    }
    cloudtrail-changes = {
      pattern     = "{ ($.eventName = CreateTrail) || ($.eventName = UpdateTrail) || ($.eventName = DeleteTrail) || ($.eventName = StartLogging) || ($.eventName = StopLogging) || ($.eventName = PutEventSelectors) }"
      description = "CloudTrail configuration was changed."
    }
    console-signin-without-mfa = {
      pattern     = "{ ($.eventName = \"ConsoleLogin\") && ($.additionalEventData.MFAUsed != \"Yes\") && ($.userIdentity.type = \"IAMUser\") && ($.responseElements.ConsoleLogin = \"Success\") }"
      description = "An IAM user signed in to the console without MFA."
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "security" {
  for_each = var.enable_cloudtrail ? local.security_metric_filters : {}

  name           = "facaibank-${var.env}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.cloudtrail[0].name
  pattern        = each.value.pattern

  metric_transformation {
    name          = each.key
    namespace     = "FaCaiBank/Security"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "security" {
  for_each = aws_cloudwatch_log_metric_filter.security

  alarm_name          = "facaibank-${var.env}-security-${each.key}"
  alarm_description   = "${local.security_metric_filters[each.key].description} See docs/runbooks/security-incident.md."
  namespace           = "FaCaiBank/Security"
  metric_name         = each.key
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.security_alerts.arn]
}

# ---------------------------------------------------------------- GuardDuty

resource "aws_guardduty_detector" "main" {
  count = var.enable_guardduty ? 1 : 0

  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"
}

resource "aws_guardduty_detector_feature" "main" {
  for_each = var.enable_guardduty ? toset(["S3_DATA_EVENTS", "RDS_LOGIN_EVENTS", "LAMBDA_NETWORK_LOGS"]) : toset([])

  detector_id = aws_guardduty_detector.main[0].id
  name        = each.value
  status      = "ENABLED"
}

# ---------------------------------------------------------------- Security Hub

resource "aws_securityhub_account" "main" {
  count = var.enable_security_hub ? 1 : 0

  enable_default_standards  = false
  control_finding_generator = "SECURITY_CONTROL"
  auto_enable_controls      = true
}

resource "aws_securityhub_standards_subscription" "fsbp" {
  count = var.enable_security_hub ? 1 : 0

  standards_arn = "arn:aws:securityhub:${var.aws_region}::standards/aws-foundational-security-best-practices/v/1.0.0"
  depends_on    = [aws_securityhub_account.main]
}

# ---------------------------------------------------------------- Macie

resource "aws_macie2_account" "main" {
  count = var.enable_macie ? 1 : 0

  finding_publishing_frequency = "FIFTEEN_MINUTES"
  status                       = "ENABLED"
}

resource "aws_macie2_classification_job" "kyc" {
  count = var.enable_macie ? 1 : 0

  name     = "facaibank-${var.env}-kyc-weekly"
  job_type = "SCHEDULED"

  schedule_frequency {
    weekly_schedule = "MONDAY"
  }

  s3_job_definition {
    bucket_definitions {
      account_id = local.account_id
      buckets    = [aws_s3_bucket.kyc_statements.id]
    }
  }

  depends_on = [aws_macie2_account.main]

  lifecycle {
    ignore_changes = [job_status]
  }
}

# ---------------------------------------------------------------- AWS Config

# Config snapshots go to their own bucket: AWS Config cannot deliver to a bucket
# with an Object Lock default retention, which the CloudTrail audit bucket has.
resource "aws_s3_bucket" "config" {
  count = var.enable_config ? 1 : 0

  bucket        = "facaibank-${var.env}-config-${local.account_id}-${var.aws_region}"
  force_destroy = true

  tags = {
    Name = "facaibank-${var.env}-config"
    Tier = "audit"
  }
}

resource "aws_s3_bucket_public_access_block" "config" {
  count = var.enable_config ? 1 : 0

  bucket                  = aws_s3_bucket.config[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  count = var.enable_config ? 1 : 0

  bucket = aws_s3_bucket.config[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "config" {
  count = var.enable_config ? 1 : 0

  bucket = aws_s3_bucket.config[0].id

  rule {
    id     = "expire-config-history"
    status = "Enabled"

    filter {}

    expiration {
      days = 365
    }
  }
}

resource "aws_s3_bucket_policy" "config" {
  count = var.enable_config ? 1 : 0

  bucket = aws_s3_bucket.config[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.config[0].arn, "${aws_s3_bucket.config[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_role" "config" {
  count = var.enable_config ? 1 : 0

  name = "facaibank-${var.env}-config"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "config.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "config" {
  count = var.enable_config ? 1 : 0

  role       = aws_iam_role.config[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

resource "aws_iam_role_policy" "config_delivery" {
  count = var.enable_config ? 1 : 0

  name = "deliver-to-audit-bucket"
  role = aws_iam_role.config[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.config[0].arn}/config/*" },
      { Effect = "Allow", Action = ["s3:GetBucketAcl", "s3:ListBucket"], Resource = aws_s3_bucket.config[0].arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_config_configuration_recorder" "main" {
  count = var.enable_config ? 1 : 0

  name     = "facaibank-${var.env}"
  role_arn = aws_iam_role.config[0].arn

  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }

  # Daily snapshots keep Config affordable for a prototype.
  recording_mode {
    recording_frequency = "DAILY"
  }
}

resource "aws_config_delivery_channel" "main" {
  count = var.enable_config ? 1 : 0

  name           = "facaibank-${var.env}"
  s3_bucket_name = aws_s3_bucket.config[0].id
  s3_key_prefix  = "config"
  s3_kms_key_arn = aws_kms_key.main.arn

  depends_on = [aws_config_configuration_recorder.main, aws_s3_bucket_policy.config, aws_iam_role_policy.config_delivery]
}

resource "aws_config_configuration_recorder_status" "main" {
  count = var.enable_config ? 1 : 0

  name       = aws_config_configuration_recorder.main[0].name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.main]
}

resource "aws_config_config_rule" "managed" {
  for_each = var.enable_config ? toset([
    "CLOUD_TRAIL_ENABLED",
    "CLOUDTRAIL_S3_DATAEVENTS_ENABLED",
    "DYNAMODB_PITR_ENABLED",
    "DYNAMODB_TABLE_ENCRYPTED_KMS",
    "EC2_EBS_ENCRYPTION_BY_DEFAULT",
    "EC2_IMDSV2_CHECK",
    "IAM_ROOT_ACCESS_KEY_CHECK",
    "RDS_INSTANCE_PUBLIC_ACCESS_CHECK",
    "RDS_STORAGE_ENCRYPTED",
    "S3_BUCKET_LEVEL_PUBLIC_ACCESS_PROHIBITED",
    "S3_BUCKET_SSL_REQUESTS_ONLY",
    "S3_DEFAULT_ENCRYPTION_KMS",
    "VPC_FLOW_LOGS_ENABLED",
  ]) : toset([])

  name = "facaibank-${var.env}-${lower(replace(each.value, "_", "-"))}"

  source {
    owner             = "AWS"
    source_identifier = each.value
  }

  depends_on = [aws_config_configuration_recorder.main]
}

# ---------------------------------------------------------------- findings -> security topic

resource "aws_cloudwatch_event_rule" "security_findings" {
  for_each = {
    guardduty = {
      enabled = var.enable_guardduty
      pattern = { source = ["aws.guardduty"], "detail-type" = ["GuardDuty Finding"], detail = { severity = [{ numeric = [">=", 7] }] } }
    }
    securityhub = {
      enabled = var.enable_security_hub
      pattern = {
        source        = ["aws.securityhub"]
        "detail-type" = ["Security Hub Findings - Imported"]
        detail = { findings = {
          Severity    = { Label = ["HIGH", "CRITICAL"] }
          Workflow    = { Status = ["NEW"] }
          RecordState = ["ACTIVE"]
        } }
      }
    }
    macie = {
      enabled = var.enable_macie
      pattern = { source = ["aws.macie"], "detail-type" = ["Macie Finding"] }
    }
  }

  name          = "facaibank-${var.env}-${each.key}-findings"
  description   = "Route high-severity ${each.key} findings to the security topic."
  event_pattern = jsonencode(each.value.pattern)
  state         = each.value.enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "security_findings" {
  for_each = aws_cloudwatch_event_rule.security_findings

  rule = each.value.name
  arn  = aws_sns_topic.security_alerts.arn
}
