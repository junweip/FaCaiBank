"""DynamoDB Streams -> SQS outbox publisher with shard leases and checkpoints.

Each worker claims a fair share of the stream's shards through conditional
writes to the lease table, publishes committed outbox events to SQS and
checkpoints the last sequence number it handled. A restarted or replacement
worker resumes from the checkpoint instead of replaying the stream, and adding
workers spreads shards across the fleet.

Delivery is at-least-once: an event published just before a crash is published
again after takeover, and the notifier deduplicates by event ID. Notifications
are order-insensitive, so parent/child shard ordering is not enforced.
"""

import json
import logging
import math
import os
import random
import signal
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

REGION = os.getenv("AWS_REGION", "ap-southeast-1")
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
TABLE_NAME = os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger")
QUEUE_URL = os.getenv("EVENTS_QUEUE_URL", "")
LEASE_TABLE_NAME = os.getenv("OUTBOX_LEASE_TABLE", "")
POLL_INTERVAL_SECONDS = float(os.getenv("STREAM_POLL_INTERVAL_SECONDS", "0.5"))
MAX_BACKOFF_SECONDS = float(os.getenv("MAX_BACKOFF_SECONDS", "30"))
LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "30"))
COORDINATION_INTERVAL_SECONDS = float(os.getenv("OUTBOX_COORDINATION_SECONDS", "10"))
FINISHED_LEASE_TTL_SECONDS = 48 * 3600
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "false").lower() == "true"
METRICS_NAMESPACE = os.getenv("WORKER_METRICS_NAMESPACE", "FaCaiBank/Workers")
WORKER_ID = os.getenv("OUTBOX_WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
deserializer = TypeDeserializer()
running = True


def _stop(_signum, _frame):
    global running
    running = False


def decode_stream_image(image: dict[str, Any]) -> dict[str, Any]:
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def outbox_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("eventName") != "INSERT":
        return None

    image = record.get("dynamodb", {}).get("NewImage", {})
    partition_key = image.get("PK", {}).get("S", "")
    if not partition_key.startswith("OUTBOX#"):
        return None

    item = decode_stream_image(image)
    payload = item.get("payload")
    if not isinstance(payload, dict) or not payload.get("event_id"):
        raise ValueError(f"Outbox record {partition_key} is missing an event payload")
    return payload


def send_with_backoff(sqs, queue_url: str, payload: dict[str, Any]) -> None:
    delay = 1.0
    while running:
        try:
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(payload, separators=(",", ":"), default=str),
            )
            logger.info("Published outbox event_id=%s", payload["event_id"])
            return
        except ClientError:
            logger.exception("Could not publish outbox event_id=%s", payload.get("event_id"))
        except Exception:
            logger.exception("Unexpected error publishing outbox event_id=%s", payload.get("event_id"))

        sleep_for = min(delay, MAX_BACKOFF_SECONDS)
        time.sleep(random.uniform(sleep_for * 0.5, sleep_for))
        delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    raise InterruptedError("Worker stopped before the outbox message was published")


def _conditional_failure(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class LeaseManager:
    """Shard leases and worker heartbeats stored in one small DynamoDB table."""

    def __init__(self, table, worker_id: str, lease_seconds: int = LEASE_SECONDS, clock=time.time):
        self.table = table
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.clock = clock

    def _now(self) -> int:
        return int(self.clock())

    def heartbeat(self) -> None:
        expires_at = self._now() + self.lease_seconds
        self.table.put_item(
            Item={"PK": "WORKER", "SK": self.worker_id, "expires_at": expires_at, "ttl": expires_at + 3600}
        )

    def active_workers(self) -> int:
        now = self._now()
        response = self.table.query(KeyConditionExpression=Key("PK").eq("WORKER"), ConsistentRead=True)
        return max(1, sum(1 for item in response.get("Items", []) if int(item.get("expires_at", 0)) > now))

    def leases(self) -> dict[str, dict]:
        response = self.table.query(KeyConditionExpression=Key("PK").eq("LEASE"), ConsistentRead=True)
        return {item["SK"]: item for item in response.get("Items", [])}

    def acquire(self, shard_id: str) -> dict | None:
        now = self._now()
        try:
            response = self.table.update_item(
                Key={"PK": "LEASE", "SK": shard_id},
                UpdateExpression="SET lease_owner = :me, lease_expires_at = :expires",
                ConditionExpression=(
                    "(attribute_not_exists(PK) OR lease_owner = :me OR lease_expires_at < :now) "
                    "AND attribute_not_exists(finished)"
                ),
                ExpressionAttributeValues={":me": self.worker_id, ":expires": now + self.lease_seconds, ":now": now},
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _conditional_failure(error):
                return None
            raise
        logger.info("Worker %s acquired lease on shard %s", self.worker_id, shard_id)
        return response["Attributes"]

    def _owned_update(self, shard_id: str, expression: str, values: dict) -> bool:
        try:
            self.table.update_item(
                Key={"PK": "LEASE", "SK": shard_id},
                UpdateExpression=expression,
                ConditionExpression="lease_owner = :me",
                ExpressionAttributeValues={":me": self.worker_id, **values},
            )
        except ClientError as error:
            if _conditional_failure(error):
                return False
            raise
        return True

    def renew(self, shard_id: str) -> bool:
        return self._owned_update(
            shard_id, "SET lease_expires_at = :expires", {":expires": self._now() + self.lease_seconds}
        )

    def checkpoint(self, shard_id: str, sequence_number: str) -> bool:
        return self._owned_update(
            shard_id,
            "SET checkpoint = :sequence, lease_expires_at = :expires",
            {":sequence": sequence_number, ":expires": self._now() + self.lease_seconds},
        )

    def release(self, shard_id: str) -> bool:
        return self._owned_update(shard_id, "SET lease_expires_at = :zero", {":zero": 0})

    def finish(self, shard_id: str) -> bool:
        try:
            self.table.update_item(
                Key={"PK": "LEASE", "SK": shard_id},
                UpdateExpression="SET finished = :true, lease_expires_at = :zero, #ttl = :ttl",
                ConditionExpression="lease_owner = :me",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":me": self.worker_id,
                    ":true": True,
                    ":zero": 0,
                    ":ttl": self._now() + FINISHED_LEASE_TTL_SECONDS,
                },
            )
        except ClientError as error:
            if _conditional_failure(error):
                return False
            raise
        logger.info("Shard %s fully processed", shard_id)
        return True

    def retire(self) -> None:
        self.table.delete_item(Key={"PK": "WORKER", "SK": self.worker_id})


def target_lease_count(open_shards: int, workers: int) -> int:
    return math.ceil(open_shards / max(1, workers)) if open_shards else 0


class WorkerMetrics:
    def __init__(self, enabled: bool = METRICS_ENABLED, interval: float = 60.0, clock=time.monotonic):
        self.enabled = enabled
        self.interval = interval
        self.clock = clock
        self.max_age_ms = 0.0
        self.published = 0
        self.last_flush = clock()
        self._client = None

    def observe(self, record: dict) -> None:
        created = record.get("dynamodb", {}).get("ApproximateCreationDateTime")
        if isinstance(created, (int, float)):
            created = datetime.fromtimestamp(created, timezone.utc)
        if isinstance(created, datetime):
            age = (datetime.now(timezone.utc) - created).total_seconds() * 1000
            self.max_age_ms = max(self.max_age_ms, age)

    def data(self, owned_shards: int) -> list[dict]:
        dimensions = [{"Name": "Environment", "Value": ENVIRONMENT}, {"Name": "Service", "Value": "outbox"}]
        return [
            {
                "MetricName": "OutboxIteratorAgeMilliseconds",
                "Dimensions": dimensions,
                "Value": round(self.max_age_ms, 1),
                "Unit": "Milliseconds",
            },
            {"MetricName": "OutboxEventsPublished", "Dimensions": dimensions, "Value": self.published, "Unit": "Count"},
            {"MetricName": "OutboxOwnedShards", "Dimensions": dimensions, "Value": owned_shards, "Unit": "Count"},
        ]

    def maybe_flush(self, owned_shards: int) -> None:
        if self.clock() - self.last_flush < self.interval:
            return
        data = self.data(owned_shards)
        self.max_age_ms, self.published, self.last_flush = 0.0, 0, self.clock()
        if not self.enabled:
            return
        try:
            if self._client is None:
                self._client = boto3.client("cloudwatch", region_name=REGION)
            self._client.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=data)
        except Exception:
            logger.warning("Could not publish outbox metrics", exc_info=True)


def discover_shards(client, stream_arn: str) -> list[dict[str, Any]]:
    shards = []
    exclusive_start_shard_id = None
    while True:
        request = {"StreamArn": stream_arn, "Limit": 100}
        if exclusive_start_shard_id:
            request["ExclusiveStartShardId"] = exclusive_start_shard_id
        response = client.describe_stream(**request)["StreamDescription"]
        shards.extend(response.get("Shards", []))
        if not response.get("LastEvaluatedShardId"):
            return shards
        exclusive_start_shard_id = response["LastEvaluatedShardId"]


class OutboxPublisher:
    def __init__(self, streams, sqs, leases: LeaseManager, queue_url: str, stream_arn: str, metrics=None):
        self.streams = streams
        self.sqs = sqs
        self.leases = leases
        self.queue_url = queue_url
        self.stream_arn = stream_arn
        self.metrics = metrics or WorkerMetrics(enabled=False)
        self.iterators: dict[str, str] = {}

    def _iterator_for(self, shard_id: str, checkpoint: str | None) -> str | None:
        request = {"StreamArn": self.stream_arn, "ShardId": shard_id}
        if checkpoint:
            try:
                return self.streams.get_shard_iterator(
                    **request, ShardIteratorType="AFTER_SEQUENCE_NUMBER", SequenceNumber=checkpoint
                ).get("ShardIterator")
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "TrimmedDataAccessException":
                    raise
                logger.warning("Checkpoint for shard %s was trimmed; resuming from the oldest record", shard_id)
        return self.streams.get_shard_iterator(**request, ShardIteratorType="TRIM_HORIZON").get("ShardIterator")

    def coordinate(self) -> None:
        self.leases.heartbeat()
        shard_ids = [shard["ShardId"] for shard in discover_shards(self.streams, self.stream_arn)]
        leases = self.leases.leases()
        open_shards = [shard_id for shard_id in shard_ids if not leases.get(shard_id, {}).get("finished")]

        for shard_id in list(self.iterators):
            if not self.leases.renew(shard_id):
                logger.warning("Lost lease on shard %s", shard_id)
                self.iterators.pop(shard_id, None)

        target = target_lease_count(len(open_shards), self.leases.active_workers())
        for shard_id in sorted(self.iterators)[target:]:
            self.leases.release(shard_id)
            self.iterators.pop(shard_id, None)
            logger.info("Released shard %s to rebalance the fleet", shard_id)

        candidates = [shard_id for shard_id in open_shards if shard_id not in self.iterators]
        random.shuffle(candidates)
        for shard_id in candidates:
            if len(self.iterators) >= target:
                break
            lease = self.leases.acquire(shard_id)
            if lease is None:
                continue
            iterator = self._iterator_for(shard_id, lease.get("checkpoint"))
            if iterator:
                self.iterators[shard_id] = iterator
            else:
                self.leases.finish(shard_id)

    def poll(self) -> None:
        for shard_id, iterator in list(self.iterators.items()):
            try:
                response = self.streams.get_records(ShardIterator=iterator, Limit=100)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") not in {
                    "ExpiredIteratorException",
                    "TrimmedDataAccessException",
                }:
                    raise
                checkpoint = self.leases.leases().get(shard_id, {}).get("checkpoint")
                refreshed = self._iterator_for(shard_id, checkpoint)
                if refreshed:
                    self.iterators[shard_id] = refreshed
                continue

            last_sequence = None
            for record in response.get("Records", []):
                try:
                    payload = outbox_payload(record)
                except ValueError:
                    logger.exception("Skipping malformed outbox record in shard %s", shard_id)
                    payload = None
                if payload is not None:
                    send_with_backoff(self.sqs, self.queue_url, payload)
                    self.metrics.published += 1
                self.metrics.observe(record)
                last_sequence = record["dynamodb"]["SequenceNumber"]

            if last_sequence and not self.leases.checkpoint(shard_id, last_sequence):
                logger.warning("Lost lease on shard %s before checkpointing", shard_id)
                self.iterators.pop(shard_id, None)
                continue
            next_iterator = response.get("NextShardIterator")
            if next_iterator:
                self.iterators[shard_id] = next_iterator
            else:
                self.leases.finish(shard_id)
                self.iterators.pop(shard_id, None)

    def shutdown(self) -> None:
        for shard_id in list(self.iterators):
            self.leases.release(shard_id)
        self.iterators.clear()
        self.leases.retire()


def run() -> None:
    if not QUEUE_URL:
        raise RuntimeError("EVENTS_QUEUE_URL must be configured")
    if not LEASE_TABLE_NAME:
        raise RuntimeError("OUTBOX_LEASE_TABLE must be configured")

    streams = boto3.client("dynamodbstreams", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)
    table = boto3.client("dynamodb", region_name=REGION).describe_table(TableName=TABLE_NAME)["Table"]
    stream_arn = table.get("LatestStreamArn")
    if not stream_arn:
        raise RuntimeError(f"DynamoDB Streams are not enabled for table {TABLE_NAME}")

    lease_table = boto3.resource("dynamodb", region_name=REGION).Table(LEASE_TABLE_NAME)
    publisher = OutboxPublisher(
        streams, sqs, LeaseManager(lease_table, WORKER_ID), QUEUE_URL, stream_arn, WorkerMetrics()
    )
    logger.info("Outbox worker %s starting on %s", WORKER_ID, stream_arn)
    last_coordination = 0.0
    delay = 1.0
    try:
        while running:
            try:
                if time.monotonic() - last_coordination >= COORDINATION_INTERVAL_SECONDS:
                    publisher.coordinate()
                    last_coordination = time.monotonic()
                publisher.poll()
                publisher.metrics.maybe_flush(len(publisher.iterators))
                delay = 1.0
                time.sleep(POLL_INTERVAL_SECONDS)
            except InterruptedError:
                break
            except ClientError:
                logger.exception("Outbox worker AWS error; backing off")
                if not running:
                    break
                time.sleep(random.uniform(delay * 0.5, delay))
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    finally:
        publisher.shutdown()
        logger.info("Outbox worker %s stopped and released its leases", WORKER_ID)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    run()
