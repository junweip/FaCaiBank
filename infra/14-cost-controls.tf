# Optional office-hours schedule for development stacks (cost management).
# Scales every fleet to zero and stops RDS overnight (Singapore time); NAT,
# ALBs, CloudFront and the serverless pieces keep running. Do not enable on a
# stack that must meet the availability target.

locals {
  office_hours_fleets = var.enable_office_hours_schedule ? aws_autoscaling_group.fleet : {}
  stop_rds_overnight  = var.enable_office_hours_schedule && !var.enable_rds_read_replica
}

resource "aws_autoscaling_schedule" "night" {
  for_each = local.office_hours_fleets

  scheduled_action_name  = "facaibank-${var.env}-${each.key}-night"
  autoscaling_group_name = each.value.name
  recurrence             = "0 23 * * *"
  time_zone              = "Asia/Singapore"
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
}

resource "aws_autoscaling_schedule" "morning" {
  for_each = local.office_hours_fleets

  scheduled_action_name  = "facaibank-${var.env}-${each.key}-morning"
  autoscaling_group_name = each.value.name
  recurrence             = "0 8 * * 1-5"
  time_zone              = "Asia/Singapore"
  min_size               = var.asg_min_size
  max_size               = var.asg_max_size
  desired_capacity       = var.asg_desired_capacity
}

resource "aws_iam_role" "rds_schedule" {
  count = local.stop_rds_overnight ? 1 : 0

  name = "facaibank-${var.env}-rds-schedule"
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

resource "aws_iam_role_policy" "rds_schedule" {
  count = local.stop_rds_overnight ? 1 : 0

  name = "stop-start-rds"
  role = aws_iam_role.rds_schedule[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["rds:StopDBInstance", "rds:StartDBInstance"]
      Resource = aws_db_instance.main.arn
    }]
  })
}

resource "aws_scheduler_schedule" "rds" {
  for_each = local.stop_rds_overnight ? {
    stop  = { expression = "cron(0 23 * * ? *)", action = "stopDBInstance" }
    start = { expression = "cron(30 7 ? * MON-FRI *)", action = "startDBInstance" }
  } : {}

  name                         = "facaibank-${var.env}-rds-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = "Asia/Singapore"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:${each.value.action}"
    role_arn = aws_iam_role.rds_schedule[0].arn
    input    = jsonencode({ DbInstanceIdentifier = aws_db_instance.main.identifier })
  }
}
