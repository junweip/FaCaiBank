# Optional data-tier components from slide 8: ElastiCache for sessions and hot
# reads (balances always skip it) and an asynchronous RDS read replica in AZ 3
# for statements and account reports.

resource "random_password" "redis_auth" {
  count = var.enable_elasticache ? 1 : 0

  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth" {
  count = var.enable_elasticache ? 1 : 0

  name                    = "facaibank-${var.env}-redis-auth"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  count = var.enable_elasticache ? 1 : 0

  secret_id     = aws_secretsmanager_secret.redis_auth[0].id
  secret_string = jsonencode({ auth_token = random_password.redis_auth[0].result })
}

resource "aws_security_group" "cache" {
  count = var.enable_elasticache ? 1 : 0

  name        = "facaibank-${var.env}-cache"
  description = "Valkey access from the API fleet only."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "facaibank-${var.env}-cache"
    Tier = "data"
  }
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_backend" {
  count = var.enable_elasticache ? 1 : 0

  security_group_id            = aws_security_group.cache[0].id
  referenced_security_group_id = aws_security_group.backend_app.id
  description                  = "Valkey (TLS) from the API fleet."
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

resource "aws_elasticache_subnet_group" "main" {
  count = var.enable_elasticache ? 1 : 0

  name       = "facaibank-${var.env}-cache"
  subnet_ids = aws_subnet.private_data[*].id
}

resource "aws_elasticache_replication_group" "main" {
  count = var.enable_elasticache ? 1 : 0

  replication_group_id        = "facaibank-${var.env}-cache"
  description                 = "FaCaiBank sessions and hot reads (primary in AZ 1, replica in AZ 2)."
  engine                      = "valkey"
  engine_version              = "8.0"
  node_type                   = var.elasticache_node_type
  num_cache_clusters          = 2
  preferred_cache_cluster_azs = slice(data.aws_availability_zones.available.names, 0, 2)
  automatic_failover_enabled  = true
  multi_az_enabled            = true
  port                        = 6379
  subnet_group_name           = aws_elasticache_subnet_group.main[0].name
  security_group_ids          = [aws_security_group.cache[0].id]
  at_rest_encryption_enabled  = true
  kms_key_id                  = aws_kms_key.main.arn
  transit_encryption_enabled  = true
  auth_token                  = random_password.redis_auth[0].result
  snapshot_retention_limit    = 1
  apply_immediately           = true

  tags = {
    Name = "facaibank-${var.env}-cache"
    Tier = "data"
  }
}

resource "aws_db_instance" "replica" {
  count = var.enable_rds_read_replica ? 1 : 0

  identifier                 = "facaibank-${var.env}-postgres-replica"
  replicate_source_db        = aws_db_instance.main.identifier
  instance_class             = var.db_instance_class
  availability_zone          = data.aws_availability_zones.available.names[2]
  vpc_security_group_ids     = [aws_security_group.rds.id]
  publicly_accessible        = false
  storage_encrypted          = true
  kms_key_id                 = aws_kms_key.main.arn
  auto_minor_version_upgrade = true
  backup_retention_period    = 0
  skip_final_snapshot        = true
  apply_immediately          = true

  tags = {
    Name = "facaibank-${var.env}-postgres-replica"
    Tier = "data"
  }
}
