"""PostgreSQL access for customer and account metadata (never balances).

Connections come from a bounded pool. Credentials are read from the RDS-managed
Secrets Manager secret and re-read when the pool can no longer connect, so the
service keeps working after RDS rotates the master password.
"""

import json
import logging
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import boto3
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import config


logger = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_LOCK_ID = 458_003
CREDENTIAL_TTL_SECONDS = 300


def parse_database_endpoint(endpoint: str, default_port: int) -> tuple[str, int]:
    host, separator, port_text = endpoint.rpartition(":")
    if separator and port_text.isdigit():
        return host, int(port_text)
    return endpoint, default_port


@lru_cache(maxsize=1)
def secrets_manager():
    return boto3.client("secretsmanager", region_name=config.settings.aws_region)


class CredentialCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._value: dict[str, str] | None = None
        self._loaded_at = 0.0

    def get(self) -> dict[str, str]:
        with self._lock:
            if self._value is None or time.monotonic() - self._loaded_at > CREDENTIAL_TTL_SECONDS:
                self._value = self._load()
                self._loaded_at = time.monotonic()
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._value = None

    @staticmethod
    def _load() -> dict[str, str]:
        secret_arn = config.settings.database_secret_arn
        if not secret_arn:
            raise HTTPException(status_code=503, detail="DATABASE_SECRET_ARN is not configured")
        secret_string = secrets_manager().get_secret_value(SecretId=secret_arn).get("SecretString")
        try:
            secret = json.loads(secret_string or "")
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=503, detail="Database credentials secret is invalid") from error
        if not isinstance(secret, dict) or not secret.get("username") or not secret.get("password"):
            raise HTTPException(status_code=503, detail="Database credentials secret is missing fields")
        return {"username": secret["username"], "password": secret["password"]}


class Database:
    def __init__(self, role: str):
        self.role = role
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()
        self._credentials = CredentialCache()

    def _endpoint(self) -> str:
        settings = config.settings
        endpoint = settings.database_read_host if self.role == "reader" else ""
        endpoint = endpoint or settings.database_host
        if not endpoint:
            raise HTTPException(status_code=503, detail="DATABASE_HOST is not configured")
        return endpoint

    def _connection_kwargs(self) -> dict:
        settings = config.settings
        host, port = parse_database_endpoint(self._endpoint(), settings.database_port)
        credentials = self._credentials.get()
        return {
            "host": host,
            "port": port,
            "dbname": settings.database_name,
            "user": credentials["username"],
            "password": credentials["password"],
            "connect_timeout": 5,
            "sslmode": "require",
            "row_factory": dict_row,
            "application_name": f"facaibank-api-{self.role}",
        }

    def _get_pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    kwargs=self._connection_kwargs,
                    min_size=1,
                    max_size=config.settings.database_pool_size,
                    timeout=5,
                    max_lifetime=1800,
                    name=f"facaibank-{self.role}",
                    open=True,
                )
            return self._pool

    @contextmanager
    def connection(self):
        pool = self._get_pool()
        try:
            conn = pool.getconn()
        except PoolTimeout:
            logger.warning("Database pool %s timed out; refreshing credentials", self.role)
            self._credentials.invalidate()
            conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)


primary = Database("primary")
reader = Database("reader")

_migrations_applied = False
_migrations_lock = threading.Lock()


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations(conn) -> list[str]:
    """Apply pending SQL migrations exactly once across all API instances."""
    applied_now = []
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cursor.execute("SELECT version FROM schema_migrations")
        done = {row["version"] for row in cursor.fetchall()}
        for path in migration_files():
            if path.stem in done:
                continue
            logger.info("Applying database migration %s", path.name)
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied_now.append(path.stem)
    conn.commit()
    return applied_now


def ensure_schema() -> None:
    global _migrations_applied
    if _migrations_applied:
        return
    with _migrations_lock:
        if _migrations_applied:
            return
        with primary.connection() as conn:
            apply_migrations(conn)
        _migrations_applied = True
