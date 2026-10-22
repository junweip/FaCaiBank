# Bedrock ops assistant (slide 18): summarises page and security alerts for the
# on-call engineer, masks PII with a Bedrock Guardrail, and only advises: its
# role cannot change infrastructure, so humans approve every fix.
#
# Before enabling: request Anthropic model access in the Bedrock console and run
# scripts/build_lambdas.sh so the Anthropic SDK is packaged with the function.

locals {
  ops_assistant_build = "${path.module}/../build/lambda_ops_assistant"
}

resource "aws_bedrock_guardrail" "ops" {
  count = var.enable_bedrock_assistant ? 1 : 0

  name                      = "facaibank-${var.env}-ops"
  description               = "Masks customer PII and secrets in alert summaries."
  blocked_input_messaging   = "This alert contained content that cannot be sent to the assistant."
  blocked_outputs_messaging = "The assistant's answer was withheld by the guardrail."

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = ["EMAIL", "PHONE", "NAME", "ADDRESS", "US_BANK_ACCOUNT_NUMBER", "CREDIT_DEBIT_CARD_NUMBER", "PASSWORD", "USERNAME"]

      content {
        type   = pii_entities_config.value
        action = "ANONYMIZE"
      }
    }

    pii_entities_config {
      type   = "AWS_SECRET_KEY"
      action = "BLOCK"
    }

    regexes_config {
      name        = "singapore-nric"
      description = "Singapore NRIC/FIN numbers"
      pattern     = "[STFGM][0-9]{7}[A-Z]"
      action      = "ANONYMIZE"
    }

    regexes_config {
      name        = "account-identifier"
      description = "FaCaiBank account and customer UUIDs"
      pattern     = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
      action      = "ANONYMIZE"
    }
  }
}

resource "aws_bedrock_guardrail_version" "ops" {
  count = var.enable_bedrock_assistant ? 1 : 0

  guardrail_arn = aws_bedrock_guardrail.ops[0].guardrail_arn
  description   = "Pinned for the ops assistant."
}

data "archive_file" "ops_assistant" {
  count = var.enable_bedrock_assistant ? 1 : 0

  type        = "zip"
  source_dir  = local.ops_assistant_build
  output_path = "${path.root}/.terraform/lambda_ops_assistant.zip"
}

resource "aws_cloudwatch_log_group" "ops_assistant" {
  count = var.enable_bedrock_assistant ? 1 : 0

  name              = "/facaibank/${var.env}/lambda/ops-assistant"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_iam_role" "ops_assistant" {
  count = var.enable_bedrock_assistant ? 1 : 0

  name = "facaibank-${var.env}-ops-assistant"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "ops_assistant" {
  count = var.enable_bedrock_assistant ? 1 : 0

  name = "summarise-only"
  role = aws_iam_role.ops_assistant[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      { Effect = "Allow", Action = "bedrock:ApplyGuardrail", Resource = aws_bedrock_guardrail.ops[0].guardrail_arn },
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.ops_summaries.arn },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.ops_assistant[0].arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "ops_assistant" {
  count = var.enable_bedrock_assistant ? 1 : 0

  function_name    = "facaibank-${var.env}-ops-assistant"
  description      = "Summarises alarms and findings with Claude on Amazon Bedrock for the on-call engineer."
  role             = aws_iam_role.ops_assistant[0].arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.ops_assistant[0].output_path
  source_code_hash = data.archive_file.ops_assistant[0].output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
    variables = {
      BEDROCK_MODEL_ID          = var.bedrock_model_id
      BEDROCK_FALLBACK_MODEL_ID = var.bedrock_fallback_model_id
      BEDROCK_REGION            = var.aws_region
      GUARDRAIL_ID              = aws_bedrock_guardrail.ops[0].guardrail_id
      GUARDRAIL_VERSION         = aws_bedrock_guardrail_version.ops[0].version
      OPS_TOPIC_ARN             = aws_sns_topic.ops_summaries.arn
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.ops_assistant[0].name
  }
}

resource "aws_lambda_permission" "ops_assistant" {
  for_each = var.enable_bedrock_assistant ? { page = aws_sns_topic.alerts.arn, security = aws_sns_topic.security_alerts.arn } : {}

  statement_id  = "AllowSns-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ops_assistant[0].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = each.value
}

resource "aws_sns_topic_subscription" "ops_assistant" {
  for_each = aws_lambda_permission.ops_assistant

  topic_arn = each.value.source_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ops_assistant[0].arn
}
