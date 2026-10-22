# Address changes from the first release. These keep the existing frontend and
# backend fleets, roles and launch templates in place instead of recreating
# them under the same names. The single "workers" fleet becomes the outbox fleet;
# its new name means AWS still replaces it.

moved {
  from = aws_autoscaling_group.frontend
  to   = aws_autoscaling_group.fleet["frontend"]
}

moved {
  from = aws_autoscaling_group.backend
  to   = aws_autoscaling_group.fleet["backend"]
}

moved {
  from = aws_autoscaling_group.workers
  to   = aws_autoscaling_group.fleet["outbox"]
}

moved {
  from = aws_launch_template.frontend
  to   = aws_launch_template.service["frontend"]
}

moved {
  from = aws_launch_template.backend
  to   = aws_launch_template.service["backend"]
}

moved {
  from = aws_launch_template.workers
  to   = aws_launch_template.service["outbox"]
}

moved {
  from = aws_iam_role.frontend_instance
  to   = aws_iam_role.instance["frontend"]
}

moved {
  from = aws_iam_role.backend_instance
  to   = aws_iam_role.instance["backend"]
}

moved {
  from = aws_iam_role.worker_instance
  to   = aws_iam_role.instance["outbox"]
}

moved {
  from = aws_iam_instance_profile.frontend
  to   = aws_iam_instance_profile.instance["frontend"]
}

moved {
  from = aws_iam_instance_profile.backend
  to   = aws_iam_instance_profile.instance["backend"]
}

moved {
  from = aws_iam_instance_profile.workers
  to   = aws_iam_instance_profile.instance["outbox"]
}

moved {
  from = aws_iam_role_policy.worker_instance
  to   = aws_iam_role_policy.outbox_instance
}
