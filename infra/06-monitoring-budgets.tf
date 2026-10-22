# Alert routing follows slide 12: "page" wakes the on-call engineer, "ticket"
# goes to the work queue, "security" pages the security owner, and the budget
# emails its owner. Alert topics carry metadata only (no customer data), so they
# use SNS default encryption rather than the CMK, which CloudWatch, Budgets and
# EventBridge would each need key grants for.

resource "aws_sns_topic" "alerts" {
  name = "facaibank-${var.env}-infrastructure-alerts"

  tags = {
    Name     = "facaibank-${var.env}-page-alerts"
    Tier     = "monitoring"
    Severity = "page"
  }
}

resource "aws_sns_topic" "alerts_ticket" {
  name = "facaibank-${var.env}-ticket-alerts"

  tags = {
    Name     = "facaibank-${var.env}-ticket-alerts"
    Tier     = "monitoring"
    Severity = "ticket"
  }
}

resource "aws_sns_topic" "security_alerts" {
  name = "facaibank-${var.env}-security-alerts"

  tags = {
    Name     = "facaibank-${var.env}-security-alerts"
    Tier     = "monitoring"
    Severity = "security"
  }
}

resource "aws_sns_topic" "ops_summaries" {
  name = "facaibank-${var.env}-ops-summaries"

  tags = {
    Name = "facaibank-${var.env}-ops-summaries"
    Tier = "monitoring"
  }
}

locals {
  # Whether a webhook was supplied is not secret; the URL itself stays sensitive.
  slack_enabled = nonsensitive(var.slack_webhook_url != "")
  alert_topics = {
    page     = aws_sns_topic.alerts
    ticket   = aws_sns_topic.alerts_ticket
    security = aws_sns_topic.security_alerts
  }
}

data "aws_iam_policy_document" "alert_topic" {
  for_each = local.alert_topics

  statement {
    sid       = "AllowAwsServicesToPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [each.value.arn]

    principals {
      type = "Service"
      identifiers = [
        "budgets.amazonaws.com",
        "cloudwatch.amazonaws.com",
        "events.amazonaws.com",
        "events.rds.amazonaws.com",
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "AllowAccountPrincipals"
    effect    = "Allow"
    actions   = ["sns:Publish", "sns:Subscribe", "sns:GetTopicAttributes", "sns:ListSubscriptionsByTopic"]
    resources = [each.value.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  for_each = local.alert_topics

  arn    = each.value.arn
  policy = data.aws_iam_policy_document.alert_topic[each.key].json
}

resource "aws_sns_topic_subscription" "oncall_email" {
  for_each = var.alert_email == "" ? {} : {
    page   = aws_sns_topic.alerts.arn
    ticket = aws_sns_topic.alerts_ticket.arn
    ops    = aws_sns_topic.ops_summaries.arn
  }

  topic_arn = each.value
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "security_email" {
  count = var.security_alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.security_alerts.arn
  protocol  = "email"
  endpoint  = var.security_alert_email
}

# ---------------------------------------------------------------- latency targets (slide 4)

resource "aws_cloudwatch_metric_alarm" "endpoint_latency_p95" {
  for_each = {
    balance  = { threshold = 200, description = "Balance p95 above 200 ms for 5 minutes (slide 4 target)." }
    transfer = { threshold = 1000, description = "Transfer commit p95 above 1 second for 5 minutes (slide 4 target)." }
  }

  alarm_name          = "facaibank-${var.env}-${each.key}-latency-p95"
  alarm_description   = each.value.description
  namespace           = "FaCaiBank/API"
  metric_name         = "Latency"
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    Environment = var.env
    Endpoint    = each.key
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx_rate" {
  alarm_name          = "facaibank-${var.env}-alb-5xx-error-rate"
  alarm_description   = "ALB 5xx response rate exceeded 1 percent."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "error_rate"
    expression  = "IF(requests > 0, 100 * (FILL(target_5xx, 0) + FILL(elb_5xx, 0)) / requests, 0)"
    label       = "ALB 5xx error rate (%)"
    return_data = true
  }

  metric_query {
    id = "requests"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
      }
    }
  }

  metric_query {
    id = "target_5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      period      = 60
      stat        = "Sum"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
      }
    }
  }

  metric_query {
    id = "elb_5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      period      = 60
      stat        = "Sum"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  for_each = {
    frontend = { lb = aws_lb.public, tg = aws_lb_target_group.frontend }
    backend  = { lb = aws_lb.internal, tg = aws_lb_target_group.backend }
  }

  alarm_name          = "facaibank-${var.env}-${each.key}-unhealthy-targets"
  alarm_description   = "At least one ${each.key} instance has failed ALB health checks for 5 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    LoadBalancer = each.value.lb.arn_suffix
    TargetGroup  = each.value.tg.arn_suffix
  }
}

# DynamoDB does not publish a stream iterator age; the outbox worker does.
resource "aws_cloudwatch_metric_alarm" "dynamodb_stream_iterator_age" {
  alarm_name          = "facaibank-${var.env}-dynamodb-stream-iterator-age"
  alarm_description   = "Outbox workers are more than 60 seconds behind the ledger stream."
  namespace           = "FaCaiBank/Workers"
  metric_name         = "OutboxIteratorAgeMilliseconds"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 60000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    Environment = var.env
    Service     = "outbox"
  }
}

# ---------------------------------------------------------------- ticket-level alarms

resource "aws_cloudwatch_metric_alarm" "events_dlq_visible" {
  alarm_name          = "facaibank-${var.env}-events-dlq-visible"
  alarm_description   = "One or more notification events reached events-dlq. See docs/runbooks/worker-recovery.md."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]
  ok_actions          = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    QueueName = aws_sqs_queue.events_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "statement_dlq_visible" {
  alarm_name          = "facaibank-${var.env}-statement-dlq-visible"
  alarm_description   = "One or more statement jobs reached statement-dlq. See docs/runbooks/worker-recovery.md."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]
  ok_actions          = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    QueueName = aws_sqs_queue.statement_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "statement_oldest_job" {
  alarm_name          = "facaibank-${var.env}-statement-oldest-job"
  alarm_description   = "A statement job has waited more than 15 minutes."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 900
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    QueueName = aws_sqs_queue.statement_jobs.name
  }
}

resource "aws_cloudwatch_metric_alarm" "notifier_errors" {
  alarm_name          = "facaibank-${var.env}-notifier-errors"
  alarm_description   = "The notification Lambda failed to process messages (they will retry, then go to the DLQ)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    FunctionName = aws_lambda_function.notifier.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "ledger_throttles" {
  alarm_name          = "facaibank-${var.env}-ledger-write-throttles"
  alarm_description   = "DynamoDB throttled ledger writes. Check for hot partitions or account-level limits."
  namespace           = "AWS/DynamoDB"
  metric_name         = "WriteThrottleEvents"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    TableName = aws_dynamodb_table.ledger.name
  }
}

resource "aws_cloudwatch_metric_alarm" "replica_lag" {
  count = var.enable_rds_read_replica ? 1 : 0

  alarm_name          = "facaibank-${var.env}-rds-replica-lag"
  alarm_description   = "RDS read replica is more than 5 seconds behind the primary."
  namespace           = "AWS/RDS"
  metric_name         = "ReplicaLag"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts_ticket.arn]
  ok_actions          = [aws_sns_topic.alerts_ticket.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.replica[0].identifier
  }
}

# Any RDS failover, failure or availability event pages on-call (slide 12).
resource "aws_db_event_subscription" "rds" {
  name      = "facaibank-${var.env}-rds-events"
  sns_topic = aws_sns_topic.alerts.arn

  source_type      = "db-instance"
  source_ids       = compact([aws_db_instance.main.identifier, var.enable_rds_read_replica ? aws_db_instance.replica[0].identifier : ""])
  event_categories = ["availability", "failover", "failure", "low storage"]

  depends_on = [aws_sns_topic_policy.alerts]
}

# ---------------------------------------------------------------- budget

resource "aws_budgets_budget" "monthly" {
  name              = "facaibank-${var.env}-monthly-cost"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_limit)
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2025-01-01_00:00"

  dynamic "notification" {
    for_each = [50, 80, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_alert_emails
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts_ticket.arn]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts_ticket.arn]
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

# ---------------------------------------------------------------- Slack

resource "aws_secretsmanager_secret" "slack_webhook" {
  count = local.slack_enabled ? 1 : 0

  name                    = "facaibank-${var.env}-slack-webhook"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "slack_webhook" {
  count = local.slack_enabled ? 1 : 0

  secret_id     = aws_secretsmanager_secret.slack_webhook[0].id
  secret_string = var.slack_webhook_url
}

data "archive_file" "slack_alerts" {
  type        = "zip"
  source_file = "${path.module}/../app/lambda_slack_alerts/handler.py"
  output_path = "${path.root}/.terraform/lambda_slack_alerts.zip"
}

resource "aws_iam_role" "slack_alerts" {
  count = local.slack_enabled ? 1 : 0

  name = "facaibank-${var.env}-slack-alerts"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "slack_alerts" {
  count = local.slack_enabled ? 1 : 0

  name = "read-webhook"
  role = aws_iam_role.slack_alerts[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.slack_webhook[0].arn },
      { Effect = "Allow", Action = "kms:Decrypt", Resource = aws_kms_key.main.arn },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.slack_alerts[0].arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "slack_alerts" {
  count = local.slack_enabled ? 1 : 0

  name              = "/facaibank/${var.env}/lambda/slack-alerts"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lambda_function" "slack_alerts" {
  count = local.slack_enabled ? 1 : 0

  function_name    = "facaibank-${var.env}-slack-alerts"
  description      = "Forwards page, ticket, security and ops-assistant alerts to Slack."
  role             = aws_iam_role.slack_alerts[0].arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.slack_alerts.output_path
  source_code_hash = data.archive_file.slack_alerts.output_base64sha256
  timeout          = 15

  environment {
    variables = {
      WEBHOOK_SECRET_ARN = aws_secretsmanager_secret.slack_webhook[0].arn
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.slack_alerts[0].name
  }
}

locals {
  slack_topics = !local.slack_enabled ? {} : {
    page     = aws_sns_topic.alerts.arn
    ticket   = aws_sns_topic.alerts_ticket.arn
    security = aws_sns_topic.security_alerts.arn
    ops      = aws_sns_topic.ops_summaries.arn
  }
}

resource "aws_lambda_permission" "slack_alerts" {
  for_each = local.slack_topics

  statement_id  = "AllowSns-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_alerts[0].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = each.value
}

resource "aws_sns_topic_subscription" "slack_alerts" {
  for_each = local.slack_topics

  topic_arn = each.value
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_alerts[0].arn
}

# ---------------------------------------------------------------- dashboard

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "facaibank-${var.env}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "API latency p95 (targets: balance 200 ms, transfer 1 s)"
          region = var.aws_region
          stat   = "p95"
          period = 60
          metrics = [
            ["FaCaiBank/API", "Latency", "Environment", var.env, "Endpoint", "balance"],
            ["FaCaiBank/API", "Latency", "Environment", var.env, "Endpoint", "transfer"],
          ]
          annotations = { horizontal = [{ value = 200, label = "balance target" }, { value = 1000, label = "transfer target" }] }
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Requests and 5xx"
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.public.arn_suffix],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.public.arn_suffix],
            ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", aws_lb.public.arn_suffix],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "Instances in service per fleet"
          region  = var.aws_region
          stat    = "Average"
          period  = 60
          metrics = [for key, asg in aws_autoscaling_group.fleet : ["AWS/AutoScaling", "GroupInServiceInstances", "AutoScalingGroupName", asg.name]]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Background work"
          region = var.aws_region
          period = 60
          metrics = [
            ["FaCaiBank/Workers", "OutboxIteratorAgeMilliseconds", "Environment", var.env, "Service", "outbox", { stat = "Maximum" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events.name, { stat = "Maximum", yAxis = "right" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.statement_jobs.name, { stat = "Maximum", yAxis = "right" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events_dlq.name, { stat = "Maximum", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "Ledger (DynamoDB)"
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.ledger.name],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", aws_dynamodb_table.ledger.name],
            ["AWS/DynamoDB", "TransactionConflict", "TableName", aws_dynamodb_table.ledger.name],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "Accounts database (RDS)"
          region = var.aws_region
          stat   = "Average"
          period = 60
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main.identifier],
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.main.identifier, { yAxis = "right" }],
          ]
        }
      },
    ]
  })
}
