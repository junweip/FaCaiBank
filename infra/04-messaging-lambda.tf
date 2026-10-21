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
