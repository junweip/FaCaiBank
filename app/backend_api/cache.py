"""Hot-read and session cache.

ElastiCache (Valkey/Redis) is used when REDIS_HOST is configured; otherwise each
API instance keeps a small in-process cache. Balances are never cached: they are
always read from the DynamoDB ledger with strong consistency.
"""

import json
import logging
import threading
import time
from functools import lru_cache
from typing import Any

import boto3

from . import config


logger = logging.getLogger(__name__)


class MemoryCache:
    backend = "memory"

    def __init__(self, max_items: int = 10_000):
        self._items: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._max_items = max_items

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                self._items.pop(key, None)
                return None
        return json.loads(value)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        encoded = json.dumps(value, default=str)
        with self._lock:
            if len(self._items) >= self._max_items:
                self._evict_expired()
                if len(self._items) >= self._max_items:
                    self._items.pop(next(iter(self._items)))
            self._items[key] = (time.monotonic() + ttl_seconds, encoded)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._items.pop(key, None)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for key in [key for key, (expires_at, _) in self._items.items() if expires_at < now]:
            self._items.pop(key, None)


class RedisCache:
    """Redis-compatible cache that fails open: an outage behaves like a cache miss."""

    backend = "elasticache"

    def __init__(self, client):
        self._client = client

    def get(self, key: str) -> Any | None:
        try:
            value = self._client.get(key)
        except Exception:
            logger.warning("Cache read failed for %s", key, exc_info=True)
            return None
        return json.loads(value) if value is not None else None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            self._client.set(key, json.dumps(value, default=str), ex=ttl_seconds)
        except Exception:
            logger.warning("Cache write failed for %s", key, exc_info=True)

    def delete(self, *keys: str) -> None:
        try:
            self._client.delete(*keys)
        except Exception:
            logger.warning("Cache delete failed for %s", keys, exc_info=True)

    def exists(self, key: str) -> bool:
        try:
            return bool(self._client.exists(key))
        except Exception:
            logger.warning("Cache lookup failed for %s", key, exc_info=True)
            return False


def _redis_password() -> str | None:
    secret_arn = config.settings.redis_auth_secret_arn
    if not secret_arn:
        return None
    secrets = boto3.client("secretsmanager", region_name=config.settings.aws_region)
    secret = secrets.get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        return json.loads(secret)["auth_token"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return secret


@lru_cache(maxsize=1)
def get_cache():
    settings = config.settings
    if not settings.redis_host:
        return MemoryCache()

    import redis

    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=_redis_password(),
        ssl=True,
        socket_timeout=0.25,
        socket_connect_timeout=0.5,
        health_check_interval=30,
        decode_responses=True,
    )
    logger.info("Using ElastiCache at %s:%s", settings.redis_host, settings.redis_port)
    return RedisCache(client)


def profile_key(subject: str) -> str:
    return f"profile:{subject}"


def owner_key(account_id: str) -> str:
    return f"owner:{account_id}"


def revoked_key(token_id: str) -> str:
    return f"revoked:{token_id}"
