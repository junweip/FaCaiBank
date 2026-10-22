# Staff access through IAM Identity Center (slide 18): SSO with MFA, short
# sessions and no long-lived IAM user keys. Identity Center must already be
# enabled in the account (console: IAM Identity Center > Enable), and MFA is
# enforced in its settings ("Every time they sign in").

data "aws_ssoadmin_instances" "main" {
  count = var.enable_identity_center ? 1 : 0
}

locals {
  sso_instance_arn  = var.enable_identity_center ? tolist(data.aws_ssoadmin_instances.main[0].arns)[0] : ""
  identity_store_id = var.enable_identity_center ? tolist(data.aws_ssoadmin_instances.main[0].identity_store_ids)[0] : ""

  staff_roles = {
    Admins = {
      description      = "Platform administrators (break-glass changes)."
      managed_policy   = "arn:aws:iam::aws:policy/AdministratorAccess"
      session_duration = "PT2H"
      deny_customer    = false
    }
    Developers = {
      description      = "Engineers who deploy and operate FaCaiBank. No access to customer documents."
      managed_policy   = "arn:aws:iam::aws:policy/PowerUserAccess"
      session_duration = "PT4H"
      deny_customer    = true
    }
    ReadOnly = {
      description      = "Support and graders: read configuration and metrics, never customer data."
      managed_policy   = "arn:aws:iam::aws:policy/ReadOnlyAccess"
      session_duration = "PT4H"
      deny_customer    = true
    }
    SecurityAuditors = {
      description      = "Security reviewers: Security Hub, GuardDuty, CloudTrail and Config."
      managed_policy   = "arn:aws:iam::aws:policy/SecurityAudit"
      session_duration = "PT4H"
      deny_customer    = true
    }
  }
}

data "aws_iam_policy_document" "deny_customer_data" {
  statement {
    sid       = "DenyCustomerDocuments"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.kyc_statements.arn}/*"]
  }

  statement {
    sid       = "DenyLedgerItemReads"
    effect    = "Deny"
    actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:ExportTableToPointInTime"]
    resources = [aws_dynamodb_table.ledger.arn]
  }

  statement {
    sid       = "DenyKeyDestruction"
    effect    = "Deny"
    actions   = ["kms:ScheduleKeyDeletion", "kms:DisableKey"]
    resources = ["*"]
  }
}

resource "aws_ssoadmin_permission_set" "staff" {
  for_each = var.enable_identity_center ? local.staff_roles : {}

  name             = "FaCaiBank${each.key}"
  description      = each.value.description
  instance_arn     = local.sso_instance_arn
  session_duration = each.value.session_duration
}

resource "aws_ssoadmin_managed_policy_attachment" "staff" {
  for_each = aws_ssoadmin_permission_set.staff

  instance_arn       = local.sso_instance_arn
  permission_set_arn = each.value.arn
  managed_policy_arn = local.staff_roles[each.key].managed_policy
}

resource "aws_ssoadmin_permission_set_inline_policy" "deny_customer_data" {
  for_each = { for key, role in aws_ssoadmin_permission_set.staff : key => role if local.staff_roles[key].deny_customer }

  instance_arn       = local.sso_instance_arn
  permission_set_arn = each.value.arn
  inline_policy      = data.aws_iam_policy_document.deny_customer_data.json
}

resource "aws_identitystore_group" "staff" {
  for_each = var.enable_identity_center ? local.staff_roles : {}

  identity_store_id = local.identity_store_id
  display_name      = "FaCaiBank-${var.env}-${each.key}"
  description       = each.value.description
}

resource "aws_ssoadmin_account_assignment" "staff" {
  for_each = aws_ssoadmin_permission_set.staff

  instance_arn       = local.sso_instance_arn
  permission_set_arn = each.value.arn
  principal_id       = aws_identitystore_group.staff[each.key].group_id
  principal_type     = "GROUP"
  target_id          = local.account_id
  target_type        = "AWS_ACCOUNT"
}
