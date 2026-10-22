terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.100"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

locals {
  default_tags = {
    Project     = "FaCaiBank"
    Environment = var.env
    Owner       = "Group3"
    ManagedBy   = "terraform"
    AppVersion  = var.app_version
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.default_tags
  }
}

# CloudFront certificates and CLOUDFRONT-scoped WAF ACLs must live in us-east-1.
# Only edge configuration is created there; no customer data leaves Singapore.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.default_tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}
