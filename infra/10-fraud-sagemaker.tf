# SageMaker serverless endpoint that scores every transfer before it commits
# (slide 9). The API falls back to conservative rules if the endpoint is
# disabled, cold or failing, so a transfer is never committed unscored.

data "archive_file" "fraud_model" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  type        = "tar.gz"
  source_dir  = "${path.module}/../app/fraud_model/model_package"
  output_path = "${path.root}/.terraform/fraud_model.tar.gz"
}

resource "aws_s3_object" "fraud_model" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  bucket                 = aws_s3_bucket.artifacts.bucket
  key                    = "models/fraud/${data.archive_file.fraud_model[0].output_md5}/model.tar.gz"
  source                 = data.archive_file.fraud_model[0].output_path
  source_hash            = data.archive_file.fraud_model[0].output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn
}

data "aws_sagemaker_prebuilt_ecr_image" "sklearn" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  repository_name = "sagemaker-scikit-learn"
  image_tag       = "1.2-1-cpu-py3"
}

resource "aws_iam_role" "sagemaker" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  name = "facaibank-${var.env}-sagemaker-fraud"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "sagemaker.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "sagemaker" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  name = "serve-fraud-model"
  role = aws_iam_role.sagemaker[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.artifacts.arn}/models/fraud/*" },
      { Effect = "Allow", Action = "kms:Decrypt", Resource = aws_kms_key.main.arn },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/sagemaker/*"
      },
      { Effect = "Allow", Action = "cloudwatch:PutMetricData", Resource = "*" },
    ]
  })
}

resource "aws_sagemaker_model" "fraud" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  name               = "facaibank-${var.env}-fraud-${substr(data.archive_file.fraud_model[0].output_md5, 0, 8)}"
  execution_role_arn = aws_iam_role.sagemaker[0].arn

  primary_container {
    image          = data.aws_sagemaker_prebuilt_ecr_image.sklearn[0].registry_path
    model_data_url = "s3://${aws_s3_object.fraud_model[0].bucket}/${aws_s3_object.fraud_model[0].key}"
    environment = {
      SAGEMAKER_PROGRAM             = "inference.py"
      SAGEMAKER_SUBMIT_DIRECTORY    = "/opt/ml/model/code"
      SAGEMAKER_CONTAINER_LOG_LEVEL = "20"
      SAGEMAKER_REGION              = var.aws_region
    }
  }

  depends_on = [aws_iam_role_policy.sagemaker]
}

resource "aws_sagemaker_endpoint_configuration" "fraud" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  name_prefix = "facaibank-${var.env}-fraud-"

  production_variants {
    variant_name = "primary"
    model_name   = aws_sagemaker_model.fraud[0].name

    serverless_config {
      memory_size_in_mb = 1024
      max_concurrency   = 5
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_sagemaker_endpoint" "fraud" {
  count = var.enable_sagemaker_fraud ? 1 : 0

  name                 = local.fraud_endpoint
  endpoint_config_name = aws_sagemaker_endpoint_configuration.fraud[0].name
}
