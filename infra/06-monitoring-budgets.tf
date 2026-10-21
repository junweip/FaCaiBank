resource "aws_sns_topic" "alerts" {
  name = "facaibank-${var.env}-infrastructure-alerts"

  tags = {
    Name = "facaibank-${var.env}-infrastructure-alerts"
    Tier = "monitoring"
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AllowCloudWatchAlarms"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "AllowBudgetsNotifications"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

resource "aws_cloudwatch_metric_alarm" "alb_target_p95" {
  alarm_name          = "facaibank-${var.env}-alb-target-p95-latency"
  alarm_description   = "ALB target response time p95 exceeded 200 ms."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0.2
  treat_missing_data  = "notBreaching"
  extended_statistic  = "p95"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  period              = 60
  unit                = "Seconds"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    LoadBalancer = aws_lb.public.arn_suffix
    TargetGroup  = aws_lb_target_group.frontend.arn_suffix
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx_rate" {
  alarm_name          = "facaibank-${var.env}-alb-5xx-error-rate"
  alarm_description   = "ALB 5xx response rate exceeded 1 percent."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "error_rate"
    expression  = "IF(requests > 0, 100 * errors / requests, 0)"
    label       = "ALB 5xx error rate (%)"
    return_data = true
  }

  metric_query {
    id          = "requests"
    return_data = false

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
      unit        = "Count"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
      }
    }
  }

  metric_query {
    id          = "target_errors"
    expression  = "FILL(target_5xx, 0) + FILL(elb_5xx, 0)"
    return_data = false
  }

  metric_query {
    id          = "errors"
    expression  = "target_errors"
    return_data = false
  }

  metric_query {
    id          = "target_5xx"
    return_data = false

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      period      = 60
      stat        = "Sum"
      unit        = "Count"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
        TargetGroup  = aws_lb_target_group.frontend.arn_suffix
      }
    }
  }

  metric_query {
    id          = "elb_5xx"
    return_data = false

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      period      = 60
      stat        = "Sum"
      unit        = "Count"

      dimensions = {
        LoadBalancer = aws_lb.public.arn_suffix
      }
    }
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_cloudwatch_metric_alarm" "dynamodb_stream_iterator_age" {
  alarm_name          = "facaibank-${var.env}-dynamodb-stream-iterator-age"
  alarm_description   = "DynamoDB Stream iterator age exceeded 60 seconds."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "IteratorAgeMilliseconds"
  namespace           = "AWS/DynamoDB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 60000
  unit                = "Milliseconds"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    TableName   = aws_dynamodb_table.ledger.name
    StreamLabel = element(split("/stream/", aws_dynamodb_table.ledger.stream_arn), 1)
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_cloudwatch_metric_alarm" "events_dlq_visible" {
  alarm_name          = "facaibank-${var.env}-events-dlq-visible"
  alarm_description   = "One or more messages are visible in events-dlq."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.events_dlq.name
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_cloudwatch_metric_alarm" "statement_dlq_visible" {
  alarm_name          = "facaibank-${var.env}-statement-dlq-visible"
  alarm_description   = "One or more messages are visible in statement-dlq."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.statement_dlq.name
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_budgets_budget" "monthly" {
  name              = "facaibank-${var.env}-monthly-cost"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_limit)
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2025-01-01_00:00"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 50
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.alerts]
}
