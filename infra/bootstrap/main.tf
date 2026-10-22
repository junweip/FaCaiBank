# One-time setup, applied with local state by one team member:
#   cd infra/bootstrap && terraform init && terraform apply
# Creates the shared Terraform state bucket and lock table, and the GitHub
# Actions OIDC deploy role (no long-lived AWS keys in GitHub).

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.100"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "FaCaiBank"
      Owner     = "Group3"
      ManagedBy = "terraform-bootstrap"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub Actions."
  type        = string
  default     = "junweip/FaCaiBank"
}

variable "github_environments" {
  description = "GitHub environments whose jobs may assume the deploy role."
  type        = list(string)
  default     = ["dev"]
}

variable "create_github_oidc_provider" {
  description = "Set false if the account already has the token.actions.githubusercontent.com provider."
  type        = bool
  default     = true
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# ---------------------------------------------------------------- state bucket and lock table

resource "aws_s3_bucket" "state" {
  bucket = "facaibank-tfstate-${local.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "keep-90-days-of-state-history"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_dynamodb_table" "locks" {
  name         = "facaibank-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ---------------------------------------------------------------- GitHub Actions deploy role

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

locals {
  github_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "deploy" {
  name                 = "facaibank-github-deploy"
  description          = "Assumed by GitHub Actions deploy jobs for ${var.github_repository}."
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [for environment in var.github_environments : "repo:${var.github_repository}:environment:${environment}"]
        }
      }
    }]
  })
}

# PowerUserAccess covers every service the stack uses except IAM; IAM rights are
# limited to FaCaiBank-named roles, policies and instance profiles.
resource "aws_iam_role_policy_attachment" "deploy_power_user" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "deploy_iam" {
  name = "facaibank-scoped-iam"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageFaCaiBankRoles"
        Effect = "Allow"
        Action = [
          "iam:AttachRolePolicy", "iam:CreateRole", "iam:DeleteRole", "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy", "iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole", "iam:ListRolePolicies", "iam:PassRole", "iam:PutRolePolicy",
          "iam:TagRole", "iam:UntagRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole",
        ]
        Resource = "arn:aws:iam::${local.account_id}:role/facaibank-*"
      },
      {
        Sid    = "ManageFaCaiBankInstanceProfiles"
        Effect = "Allow"
        Action = [
          "iam:AddRoleToInstanceProfile", "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
          "iam:GetInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile",
        ]
        Resource = "arn:aws:iam::${local.account_id}:instance-profile/facaibank-*"
      },
      {
        Sid      = "ServiceLinkedRoles"
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "arn:aws:iam::${local.account_id}:role/aws-service-role/*"
      },
      {
        Sid      = "ReadIam"
        Effect   = "Allow"
        Action   = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicies", "iam:ListRoles"]
        Resource = "*"
      },
    ]
  })
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "lock_table" {
  value = aws_dynamodb_table.locks.name
}

output "deploy_role_arn" {
  description = "Store as the AWS_DEPLOY_ROLE_ARN secret in the GitHub 'dev' environment."
  value       = aws_iam_role.deploy.arn
}

output "backend_hcl" {
  description = "Contents for infra/backend.hcl."
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.state.bucket}"
    key            = "facaibank/dev/terraform.tfstate"
    region         = "${var.aws_region}"
    dynamodb_table = "${aws_dynamodb_table.locks.name}"
    encrypt        = true
  EOT
}
