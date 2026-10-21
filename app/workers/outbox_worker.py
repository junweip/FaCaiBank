import json
import logging
import os
import random
import signal
import time
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

REGION = os.getenv("AWS_REGION", "ap-southeast-1")
TABLE_NAME = os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger")
QUEUE_URL = os.getenv("EVENTS_QUEUE_URL", "")
POLL_INTERVAL_SECONDS = float(os.getenv("STREAM_POLL_INTERVAL_SECONDS", "0.5"))
MAX_BACKOFF_SECONDS = float(os.getenv("MAX_BACKOFF_SECONDS", "30"))
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


def stream_client():
    return boto3.client("dynamodbstreams", region_name=REGION)


def sqs_client():
    return boto3.client("sqs", region_name=REGION)


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


def create_iterators(client, stream_arn: str) -> dict[str, str]:
    iterators = {}
    for shard in discover_shards(client, stream_arn):
        shard_id = shard["ShardId"]
        iterator = client.get_shard_iterator(
            StreamArn=stream_arn,
            ShardId=shard_id,
            ShardIteratorType="TRIM_HORIZON",
        ).get("ShardIterator")
        if iterator:
            iterators[shard_id] = iterator
    return iterators


def poll_shards(streams, sqs, queue_url: str, iterators: dict[str, str]) -> None:
    for shard_id, iterator in list(iterators.items()):
        try:
            response = streams.get_records(ShardIterator=iterator, Limit=100)
        except ClientError:
            logger.exception("Failed reading DynamoDB stream shard %s", shard_id)
            raise

        next_iterator = response.get("NextShardIterator")
        if next_iterator:
            iterators[shard_id] = next_iterator
        else:
            iterators.pop(shard_id, None)

        for record in response.get("Records", []):
            payload = outbox_payload(record)
            if payload is not None:
                send_with_backoff(sqs, queue_url, payload)


def run() -> None:
    queue_url = QUEUE_URL
    if not queue_url:
        raise RuntimeError("EVENTS_QUEUE_URL must be configured")

    streams = stream_client()
    sqs = sqs_client()
    table = boto3.client("dynamodb", region_name=REGION).describe_table(
        TableName=TABLE_NAME
    )["Table"]
    stream_arn = table.get("LatestStreamArn")
    if not stream_arn:
        raise RuntimeError(f"DynamoDB Streams are not enabled for table {TABLE_NAME}")

    iterators = create_iterators(streams, stream_arn)
    known_shards = set(iterators)
    last_discovery = time.monotonic()
    delay = 1.0
    while running:
        try:
            poll_shards(streams, sqs, queue_url, iterators)
            if time.monotonic() - last_discovery >= 60:
                for shard_id, iterator in create_iterators(streams, stream_arn).items():
                    if shard_id not in known_shards:
                        iterators[shard_id] = iterator
                        known_shards.add(shard_id)
                last_discovery = time.monotonic()
            delay = 1.0
            time.sleep(POLL_INTERVAL_SECONDS)
        except InterruptedError:
            break
        except ClientError:
            if not running:
                break
            time.sleep(random.uniform(delay * 0.5, delay))
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    run()
