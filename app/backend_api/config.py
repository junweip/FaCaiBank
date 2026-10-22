import os
from dataclasses import dataclass
from decimal import Decimal


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ENVIRONMENT", "dev")
    app_version: str = os.getenv("APP_VERSION", "local")
    aws_region: str = os.getenv("AWS_REGION", "ap-southeast-1")

    database_host: str = os.getenv("DATABASE_HOST", "")
    database_read_host: str = os.getenv("DATABASE_READ_HOST", "")
    database_name: str = os.getenv("DATABASE_NAME", "facaibank")
    database_port: int = int(os.getenv("DATABASE_PORT", "5432"))
    database_secret_arn: str = os.getenv("DATABASE_SECRET_ARN", "")
    database_pool_size: int = int(os.getenv("DATABASE_POOL_SIZE", "10"))

    ledger_table_name: str = os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger")
    kyc_statements_bucket: str = os.getenv("KYC_STATEMENTS_BUCKET", "")
    statement_queue_url: str = os.getenv("STATEMENT_QUEUE_URL", "")
    notifications_topic_arn: str = os.getenv("NOTIFICATIONS_TOPIC_ARN", "")

    cognito_user_pool_id: str = os.getenv("COGNITO_USER_POOL_ID", "")
    cognito_app_client_id: str = os.getenv("COGNITO_APP_CLIENT_ID", "")

    redis_host: str = os.getenv("REDIS_HOST", "")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_auth_secret_arn: str = os.getenv("REDIS_AUTH_SECRET_ARN", "")

    fraud_endpoint_name: str = os.getenv("FRAUD_ENDPOINT_NAME", "")
    fraud_timeout_seconds: float = float(os.getenv("FRAUD_TIMEOUT_SECONDS", "2.0"))
    fraud_block_threshold: Decimal = Decimal(os.getenv("FRAUD_BLOCK_THRESHOLD", "0.90"))
    fraud_step_up_threshold: Decimal = Decimal(os.getenv("FRAUD_STEP_UP_THRESHOLD", "0.60"))

    step_up_amount: Decimal = Decimal(os.getenv("STEP_UP_AMOUNT", "1000.00"))
    step_up_max_age_seconds: int = int(os.getenv("STEP_UP_MAX_AGE_SECONDS", "300"))

    metrics_enabled: bool = _bool("METRICS_ENABLED")
    metrics_namespace: str = os.getenv("METRICS_NAMESPACE", "FaCaiBank/API")

    external_bank_latency_ms: int = int(os.getenv("EXTERNAL_BANK_LATENCY_MS", "80"))
    clearing_shards: int = int(os.getenv("CLEARING_SHARDS", "10"))
    statement_url_ttl_seconds: int = int(os.getenv("STATEMENT_URL_TTL_SECONDS", "900"))
    idempotency_ttl_days: int = int(os.getenv("IDEMPOTENCY_TTL_DAYS", "30"))

    currency: str = os.getenv("CURRENCY", "SGD")
    cors_origins: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    max_upload_bytes: int = 10 * 1024 * 1024


settings = Settings()
