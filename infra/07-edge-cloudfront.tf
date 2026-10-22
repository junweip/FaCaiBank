# Customers -> Route 53 (optional) -> CloudFront + WAF -> public ALB -> frontend.
# CloudFront terminates TLS with its default certificate when no custom domain is
# configured, so the site is HTTPS-only even without buying a domain. Banking
# API responses are never cached at the edge.

locals {
  custom_domain    = var.domain_name != ""
  origin_domain    = local.custom_domain ? "origin.${var.domain_name}" : aws_lb.public.dns_name
  static_bucket    = "facaibank-${var.env}-static-${local.account_id}-${var.aws_region}"
  cognito_endpoint = "https://cognito-idp.${var.aws_region}.amazonaws.com"
  content_types = {
    css  = "text/css"
    js   = "application/javascript"
    png  = "image/png"
    svg  = "image/svg+xml"
    ico  = "image/x-icon"
    json = "application/json"
  }
  static_assets = [for file in fileset("${path.module}/../app/frontend/assets", "**") : file if file != "config.js"]
}

# ---------------------------------------------------------------- static assets (S3 + OAC)

resource "aws_s3_bucket" "static" {
  bucket        = local.static_bucket
  force_destroy = true

  tags = {
    Name = "facaibank-${var.env}-static"
    Tier = "edge"
  }
}

resource "aws_s3_bucket_public_access_block" "static" {
  bucket                  = aws_s3_bucket.static.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "static" {
  bucket = aws_s3_bucket.static.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "static" {
  bucket = aws_s3_bucket.static.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_object" "static_asset" {
  for_each = toset(local.static_assets)

  bucket        = aws_s3_bucket.static.bucket
  key           = "assets/${each.value}"
  source        = "${path.module}/../app/frontend/assets/${each.value}"
  source_hash   = filemd5("${path.module}/../app/frontend/assets/${each.value}")
  content_type  = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = "public, max-age=300"
}

resource "aws_s3_object" "static_config" {
  bucket        = aws_s3_bucket.static.bucket
  key           = "assets/config.js"
  content       = local.frontend_config
  content_type  = "application/javascript"
  cache_control = "no-cache"
}

resource "aws_cloudfront_origin_access_control" "static" {
  name                              = "facaibank-${var.env}-static"
  description                       = "CloudFront access to the FaCaiBank static assets bucket."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "static" {
  bucket = aws_s3_bucket.static.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontRead"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.static.arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.main.arn } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.static.arn, "${aws_s3_bucket.static.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

# ---------------------------------------------------------------- WAF (CloudFront scope, us-east-1)

resource "aws_wafv2_web_acl" "edge" {
  provider = aws.us_east_1

  name        = "facaibank-${var.env}-edge"
  description = "Edge protection for FaCaiBank: managed rules, injection blocking and per-IP rate limits."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "RateLimitPerIp"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.waf_rate_limit_per_5min
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "facaibank-${var.env}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = {
      AWSManagedRulesAmazonIpReputationList = { priority = 10, overrides = [] }
      # KYC uploads are up to 10 MiB, so the 8 KB body size rule only counts.
      AWSManagedRulesCommonRuleSet         = { priority = 20, overrides = ["SizeRestrictions_BODY"] }
      AWSManagedRulesKnownBadInputsRuleSet = { priority = 30, overrides = [] }
      AWSManagedRulesSQLiRuleSet           = { priority = 40, overrides = [] }
    }

    content {
      name     = rule.key
      priority = rule.value.priority

      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          name        = rule.key
          vendor_name = "AWS"

          dynamic "rule_action_override" {
            for_each = rule.value.overrides

            content {
              name = rule_action_override.value

              action_to_use {
                count {}
              }
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "facaibank-${var.env}-${rule.key}"
        sampled_requests_enabled   = true
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_waf_bot_control ? [1] : []

    content {
      name     = "AWSManagedRulesBotControlRuleSet"
      priority = 50

      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          name        = "AWSManagedRulesBotControlRuleSet"
          vendor_name = "AWS"

          managed_rule_group_configs {
            aws_managed_rules_bot_control_rule_set {
              inspection_level = "COMMON"
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "facaibank-${var.env}-bot-control"
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "facaibank-${var.env}-edge-waf"
    sampled_requests_enabled   = true
  }

  tags = {
    Name = "facaibank-${var.env}-edge-waf"
    Tier = "security"
  }
}

# ---------------------------------------------------------------- headers

resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "facaibank-${var.env}-security-headers"
  comment = "HSTS, CSP and anti-framing headers for every FaCaiBank response."

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = false
      override                   = true
    }

    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self' ${local.cognito_endpoint}",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
      ])
      override = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "camera=(), microphone=(), geolocation=()"
      override = true
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# ---------------------------------------------------------------- distribution

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "FaCaiBank ${var.env}"
  price_class     = "PriceClass_200"
  aliases         = local.custom_domain ? [var.domain_name] : []
  web_acl_id      = aws_wafv2_web_acl.edge.arn
  http_version    = "http2and3"

  origin {
    origin_id   = "web-alb"
    domain_name = local.origin_domain

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = local.custom_domain ? "https-only" : "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  origin {
    origin_id                = "static-assets"
    domain_name              = aws_s3_bucket.static.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.static.id
  }

  default_cache_behavior {
    target_origin_id           = "web-alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  ordered_cache_behavior {
    path_pattern               = "/assets/*"
    target_origin_id           = "static-assets"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  restrictions {
    geo_restriction {
      restriction_type = length(var.geo_allowlist) > 0 ? "whitelist" : "none"
      locations        = var.geo_allowlist
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !local.custom_domain
    acm_certificate_arn            = local.custom_domain ? aws_acm_certificate_validation.edge[0].certificate_arn : null
    ssl_support_method             = local.custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }

  tags = {
    Name = "facaibank-${var.env}-cdn"
    Tier = "edge"
  }
}

# ---------------------------------------------------------------- optional custom domain

resource "aws_acm_certificate" "edge" {
  count    = local.custom_domain ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "origin" {
  count = local.custom_domain ? 1 : 0

  domain_name       = "origin.${var.domain_name}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  certificate_validation = local.custom_domain ? {
    for option in concat(
      tolist(aws_acm_certificate.edge[0].domain_validation_options),
      tolist(aws_acm_certificate.origin[0].domain_validation_options),
    ) : option.domain_name => option
  } : {}
}

resource "aws_route53_record" "certificate_validation" {
  for_each = local.certificate_validation

  zone_id         = var.route53_zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "edge" {
  count    = local.custom_domain ? 1 : 0
  provider = aws.us_east_1

  certificate_arn         = aws_acm_certificate.edge[0].arn
  validation_record_fqdns = [aws_route53_record.certificate_validation[var.domain_name].fqdn]
}

resource "aws_acm_certificate_validation" "origin" {
  count = local.custom_domain ? 1 : 0

  certificate_arn         = aws_acm_certificate.origin[0].arn
  validation_record_fqdns = [aws_route53_record.certificate_validation["origin.${var.domain_name}"].fqdn]
}

resource "aws_route53_record" "site" {
  for_each = local.custom_domain ? toset(["A", "AAAA"]) : toset([])

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = each.value

  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "origin" {
  count = local.custom_domain ? 1 : 0

  zone_id = var.route53_zone_id
  name    = "origin.${var.domain_name}"
  type    = "A"

  alias {
    name                   = aws_lb.public.dns_name
    zone_id                = aws_lb.public.zone_id
    evaluate_target_health = true
  }
}
