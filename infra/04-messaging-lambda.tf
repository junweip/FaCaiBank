resource "aws_sqs_queue" "events_dlq" {
  name                              = "events-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = "facaibank-${var.env}-events-dlq"
    Tier = "messaging"
  }
}

resource "aws_sqs_queue" "events" {
  name                              = "events-queue"
  visibility_timeout_seconds        = var.lambda_timeout * 6
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.events_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name = "facaibank-${var.env}-events-queue"
    Tier = "messaging"
  }
}

resource "aws_sqs_queue" "statement_dlq" {
  name                              = "statement-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = "facaibank-${var.env}-statement-dlq"
    Tier = "messaging"
  }
}

resource "aws_sqs_queue" "statement_jobs" {
  name                              = "statement-jobs-queue"
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.statement_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name = "facaibank-${var.env}-statement-jobs-queue"
    Tier = "messaging"
  }
}

resource "aws_sns_topic" "notifications" {
  name              = "customer-notifications"
  kms_master_key_id = aws_kms_key.main.arn

  tags = {
    Name = "facaibank-${var.env}-customer-notifications"
    Tier = "messaging"
  }
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/facaibank/${var.env}/lambda/notifier"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_iam_role" "notifier" {
  name = "facaibank-${var.env}-lambda-notifier"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "facaibank-${var.env}-lambda-notifier"
    Tier = "app"
  }
}

resource "aws_iam_role_policy" "notifier" {
  name = "facaibank-${var.env}-lambda-notifier-access"
  role = aws_iam_role.notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.events.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = aws_dynamodb_table.ledger.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.notifications.arn
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

data "archive_file" "notifier" {
  type        = "zip"
  source_file = "${path.module}/../app/lambda_notifier/handler.py"
  output_path = "${path.root}/.terraform/lambda_notifier.zip"
}

resource "aws_lambda_function" "notifier" {
  function_name    = "facaibank-${var.env}-lambda-notifier"
  description      = "Deduplicates ledger notifications and publishes them to SNS."
  role             = aws_iam_role.notifier.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.ledger.name
      TOPIC_ARN  = aws_sns_topic.notifications.arn
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.notifier.name
  }

  depends_on = [aws_iam_role_policy.notifier]

  tags = {
    Name = "facaibank-${var.env}-lambda-notifier"
    Tier = "app"
  }
}

resource "aws_lambda_event_source_mapping" "events" {
  event_source_arn        = aws_sqs_queue.events.arn
  function_name           = aws_lambda_function.notifier.arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = true
}

# Demo inbox that receives every customer alert (no filter policy). Customers
# subscribe themselves through the API with a filter on their own identity.
resource "aws_sns_topic_subscription" "notification_inbox" {
  count = var.notification_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# ---------------------------------------------------------------- monthly statements

resource "aws_iam_role" "statement_scheduler" {
  count = var.enable_monthly_statements ? 1 : 0

  name = "facaibank-${var.env}-statement-scheduler"
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

resource "aws_iam_role_policy" "statement_scheduler" {
  count = var.enable_monthly_statements ? 1 : 0

  name = "send-monthly-run"
  role = aws_iam_role.statement_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.statement_jobs.arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

resource "aws_scheduler_schedule" "monthly_statements" {
  count = var.enable_monthly_statements ? 1 : 0

  name                         = "facaibank-${var.env}-monthly-statements"
  description                  = "Queue last month's statement for every account at 02:00 SGT on the 1st."
  schedule_expression          = "cron(0 2 1 * ? *)"
  schedule_expression_timezone = "Asia/Singapore"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sqs_queue.statement_jobs.arn
    role_arn = aws_iam_role.statement_scheduler[0].arn
    input    = jsonencode({ type = "MONTHLY_RUN" })

    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}
