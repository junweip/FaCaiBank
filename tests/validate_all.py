"""Run live FaCaiBank ACID, idempotency, and SQS/DLQ validation checks.

This script performs real AWS/API operations. It requires an explicit --execute
flag and writes its evidence as JSON under tests/evidence by default.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


LOGGER = logging.getLogger("facaibank-validation")
TRANSFER_COUNT = 5
TRANSFER_AMOUNT = Decimal("1.00")
POLL_INTERVAL_SECONDS = 5


def api_request(
    api_url: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach API {api_url}: {error.reason}") from error

    with response:
        body = response.read()
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as error:
            raise RuntimeError(f"API returned invalid JSON for {path}") from error
        return response.status, parsed


def read_balance(table, account_id: str) -> Decimal:
    result = table.get_item(
        Key={"PK": f"ACCOUNT#{account_id}", "SK": "BALANCE"},
        ConsistentRead=True,
    )
    item = result.get("Item")
    if item is None:
        raise AssertionError(f"Balance item missing for account {account_id}")
    return Decimal(str(item["balance"])).quantize(Decimal("0.01"))


def read_transactions(table, account_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    request = {
        "KeyConditionExpression": Key("PK").eq(f"ACCOUNT#{account_id}")
        & Key("SK").begins_with("TXN#"),
        "ConsistentRead": True,
    }
    while True:
        response = table.query(**request)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        request["ExclusiveStartKey"] = last_key


def read_idempotency_record(table, idempotency_key: str) -> dict[str, Any] | None:
    result = table.get_item(
        Key={"PK": f"IDEMPOTENCY#{idempotency_key}", "SK": "TRANSFER"},
        ConsistentRead=True,
    )
    return result.get("Item")


def send_transfer(
    api_url: str,
    table,
    sender_account_id: str,
    receiver_account_id: str,
    amount: Decimal,
    idempotency_key: str,
    max_attempts: int = 6,
) -> dict[str, Any]:
    payload = {
        "sender_account_id": sender_account_id,
        "receiver_account_id": receiver_account_id,
        "amount": format(amount, ".2f"),
    }
    for attempt in range(max_attempts):
        status, result = api_request(
            api_url,
            "/transfers",
            payload,
            {"Idempotency-Key": idempotency_key},
        )
        if status == 201:
            return result

        detail = str(result.get("detail", ""))
        if status == 409 and "already been used" in detail.lower():
            record = read_idempotency_record(table, idempotency_key)
            if record:
                return {
                    "transfer_id": record["transfer_id"],
                    "status": "COMPLETED",
                    "recovered_after_retry": True,
                }
        if status == 409 and "could not be completed" in detail.lower():
            if attempt + 1 < max_attempts:
                time.sleep(min(0.25 * (2**attempt), 3))
                continue
        raise RuntimeError(f"Transfer request failed with HTTP {status}: {detail}")
    raise RuntimeError("Concurrent transfer did not succeed within the retry limit")


def verify_concurrent_transfers(
    api_url: str,
    table,
    sender_account_id: str,
    receiver_account_id: str,
    run_id: str,
) -> dict[str, Any]:
    initial_total = read_balance(table, sender_account_id) + read_balance(
        table, receiver_account_id
    )
    keys = [f"validation-{run_id}-concurrent-{index}" for index in range(TRANSFER_COUNT)]
    transfer_ids: list[str] = []
    with ThreadPoolExecutor(max_workers=TRANSFER_COUNT) as executor:
        futures = [
            executor.submit(
                send_transfer,
                api_url,
                table,
                sender_account_id,
                receiver_account_id,
                TRANSFER_AMOUNT,
                key,
            )
            for key in keys
        ]
        for future in as_completed(futures):
            transfer_ids.append(str(future.result()["transfer_id"]))

    sender_items = read_transactions(table, sender_account_id)
    receiver_items = read_transactions(table, receiver_account_id)
    sender_entries = [
        item
        for item in sender_items
        if item.get("transfer_id") in transfer_ids and item.get("entry_type") == "DEBIT"
    ]
    receiver_entries = [
        item
        for item in receiver_items
        if item.get("transfer_id") in transfer_ids and item.get("entry_type") == "CREDIT"
    ]
    debit_total = sum((Decimal(str(item["amount"])) for item in sender_entries), Decimal("0"))
    credit_total = sum((Decimal(str(item["amount"])) for item in receiver_entries), Decimal("0"))
    ending_total = read_balance(table, sender_account_id) + read_balance(
        table, receiver_account_id
    )

    if len(set(transfer_ids)) != TRANSFER_COUNT:
        raise AssertionError("Concurrent requests did not produce five unique transfers")
    if len(sender_entries) != TRANSFER_COUNT or len(receiver_entries) != TRANSFER_COUNT:
        raise AssertionError(
            "Strongly consistent ledger query did not return five matched debit/credit legs"
        )
    if debit_total != TRANSFER_AMOUNT * TRANSFER_COUNT:
        raise AssertionError(f"Unexpected debit sum: {debit_total}")
    if credit_total != debit_total:
        raise AssertionError(f"Ledger debits ({debit_total}) differ from credits ({credit_total})")
    if ending_total != initial_total:
        raise AssertionError(f"Transfer changed combined account funds: {initial_total} -> {ending_total}")

    return {
        "status": "PASS",
        "concurrent_requests": TRANSFER_COUNT,
        "successful_transfers": TRANSFER_COUNT,
        "sender_account_id": sender_account_id,
        "receiver_account_id": receiver_account_id,
        "debit_total": format(debit_total, ".2f"),
        "credit_total": format(credit_total, ".2f"),
        "combined_balance_before": format(initial_total, ".2f"),
        "combined_balance_after": format(ending_total, ".2f"),
        "consistency": "Strongly consistent balance reads and base-table ledger queries",
        "rpo_claim": "RPO 0 for committed DynamoDB transactions observed by this check",
    }


def verify_idempotent_replay(
    api_url: str,
    table,
    sender_account_id: str,
    receiver_account_id: str,
    run_id: str,
) -> dict[str, Any]:
    idempotency_key = f"validation-{run_id}-replay"
    balance_before = read_balance(table, sender_account_id)
    payload = {
        "sender_account_id": sender_account_id,
        "receiver_account_id": receiver_account_id,
        "amount": format(TRANSFER_AMOUNT, ".2f"),
    }
    first_status, first_result = api_request(
        api_url,
        "/transfers",
        payload,
        {"Idempotency-Key": idempotency_key},
    )
    if first_status != 201:
        raise RuntimeError(f"Initial idempotency test transfer failed: {first_result}")

    replay_status, replay_result = api_request(
        api_url,
        "/transfers",
        payload,
        {"Idempotency-Key": idempotency_key},
    )
    if replay_status != 409 or "already been used" not in str(
        replay_result.get("detail", "")
    ).lower():
        raise AssertionError(
            f"Replay was not rejected as an idempotent duplicate: HTTP {replay_status} {replay_result}"
        )

    balance_after = read_balance(table, sender_account_id)
    record = read_idempotency_record(table, idempotency_key)
    if balance_before - balance_after != TRANSFER_AMOUNT:
        raise AssertionError(
            f"Replay changed balance more than once: before={balance_before}, after={balance_after}"
        )
    if not record or record.get("transfer_id") != first_result.get("transfer_id"):
        raise AssertionError("Idempotency record does not match the original transfer")

    sender_entries = [
        item
        for item in read_transactions(table, sender_account_id)
        if item.get("transfer_id") == first_result["transfer_id"]
        and item.get("entry_type") == "DEBIT"
    ]
    if len(sender_entries) != 1:
        raise AssertionError(f"Expected one debit ledger item, found {len(sender_entries)}")

    return {
        "status": "PASS",
        "idempotency_key": idempotency_key,
        "first_request_status": first_status,
        "replay_status": replay_status,
        "transfer_id": first_result["transfer_id"],
        "balance_before": format(balance_before, ".2f"),
        "balance_after": format(balance_after, ".2f"),
        "deducted_once": True,
    }


def wait_until(description: str, check, timeout_seconds: int) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except ClientError as error:
            last_error = error
            LOGGER.warning("Waiting for %s: %s", description, error)
        time.sleep(POLL_INTERVAL_SECONDS)
    detail = f": {last_error}" if last_error else ""
    raise TimeoutError(f"Timed out waiting for {description}{detail}")


def find_log_messages(logs, log_group: str, marker: str, start_time_ms: int) -> list[str]:
    response = logs.filter_log_events(
        logGroupName=log_group,
        startTime=start_time_ms,
        filterPattern=f'"{marker}"',
        limit=100,
    )
    return [event.get("message", "") for event in response.get("events", [])]


def receive_poison_from_dlq(sqs, queue_url: str, marker: str) -> dict[str, Any] | None:
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=10,
        VisibilityTimeout=30,
        AttributeNames=["ApproximateReceiveCount"],
    )
    for message in response.get("Messages", []):
        try:
            body = json.loads(message["Body"])
        except json.JSONDecodeError:
            body = {}
        if body.get("validation_poison_id") == marker:
            sqs.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=message["ReceiptHandle"],
                VisibilityTimeout=0,
            )
            return {
                "message_id": message["MessageId"],
                "receive_count_at_dlq": message.get("Attributes", {}).get(
                    "ApproximateReceiveCount"
                ),
            }
        sqs.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=message["ReceiptHandle"],
            VisibilityTimeout=0,
        )
    return None


def verify_sqs_deduplication_and_dlq(args, clients, run_id: str) -> dict[str, Any]:
    sqs = clients["sqs"]
    dynamodb = clients["dynamodb"]
    logs = clients["logs"]
    cloudwatch = clients["cloudwatch"]
    table = dynamodb.Table(args.ledger_table)
    baseline_queue = sqs.get_queue_attributes(
        QueueUrl=args.events_dlq_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )["Attributes"]
    if any(
        int(baseline_queue.get(attribute, "0")) > 0
        for attribute in (
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        )
    ):
        raise RuntimeError("Events DLQ must be empty before test C can prove its own DLQ routing")
    baseline_alarms = cloudwatch.describe_alarms(AlarmNames=[args.dlq_alarm_name]).get(
        "MetricAlarms", []
    )
    if not baseline_alarms:
        raise RuntimeError(f"DLQ alarm {args.dlq_alarm_name} was not found")
    baseline_alarm_state = baseline_alarms[0]["StateValue"]
    if baseline_alarm_state == "ALARM":
        raise RuntimeError("DLQ alarm is already ALARM; clear the existing incident before test C")

    duplicate_event_id = f"validation-duplicate-{run_id}"
    duplicate_payload = {
        "event_id": duplicate_event_id,
        "event_type": "TRANSFER_COMPLETED",
        "transfer_id": f"validation-transfer-{run_id}",
        "sender_account_id": args.sender_account_id,
        "receiver_account_id": args.receiver_account_id,
        "amount": "1.00",
        "currency": "USD",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    log_start = int(time.time() * 1000)
    duplicate_messages = [
        sqs.send_message(
            QueueUrl=args.events_queue_url,
            MessageBody=json.dumps(duplicate_payload, separators=(",", ":")),
        )["MessageId"]
        for _ in range(2)
    ]

    def notification_sent():
        result = table.get_item(
            Key={"PK": f"DEDUPE#{duplicate_event_id}", "SK": "NOTIFICATION"},
            ConsistentRead=True,
        )
        item = result.get("Item")
        return item if item and item.get("status") == "SENT" else None

    dedupe_item = wait_until(
        "duplicate event notification to complete",
        notification_sent,
        args.timeout_seconds,
    )
    duplicate_logs = wait_until(
        "Lambda duplicate-skip log entry",
        lambda: find_log_messages(
            logs,
            args.lambda_log_group,
            duplicate_event_id,
            log_start,
        ),
        args.timeout_seconds,
    )
    if not any("Skipping duplicate notification" in message for message in duplicate_logs):
        raise AssertionError("Lambda did not log a duplicate notification skip")

    poison_marker = f"validation-poison-{run_id}"
    poison_message_id = sqs.send_message(
        QueueUrl=args.events_queue_url,
        MessageBody=json.dumps({"validation_poison_id": poison_marker}),
    )["MessageId"]
    poison_sent_at = datetime.now(timezone.utc)
    dlq_message = wait_until(
        "malformed poison pill to arrive in events DLQ",
        lambda: receive_poison_from_dlq(sqs, args.events_dlq_url, poison_marker),
        args.timeout_seconds,
    )
    def poison_failure_logs():
        matching = [
            message
            for message in find_log_messages(
                logs,
                args.lambda_log_group,
                poison_message_id,
                log_start,
            )
            if f"Unable to process SQS message {poison_message_id}" in message
        ]
        return matching if len(matching) >= 3 else None

    poison_logs = wait_until(
        "three Lambda poison-pill failures",
        poison_failure_logs,
        args.timeout_seconds,
    )
    if len(poison_logs) != 3:
        raise AssertionError(
            f"Expected exactly three failed Lambda deliveries, observed {len(poison_logs)}"
        )
    alarm = wait_until(
        "events DLQ CloudWatch alarm to enter ALARM",
        lambda: next(
            (
                item
                for item in cloudwatch.describe_alarms(
                    AlarmNames=[args.dlq_alarm_name]
                ).get("MetricAlarms", [])
                if item["StateValue"] == "ALARM"
            ),
            None,
        ),
        args.timeout_seconds,
    )
    if alarm["StateUpdatedTimestamp"] < poison_sent_at:
        raise AssertionError("DLQ alarm was already in ALARM before the poison pill was sent")

    return {
        "status": "PASS",
        "duplicate_event_id": duplicate_event_id,
        "duplicate_sqs_message_ids": duplicate_messages,
        "dedupe_status": dedupe_item["status"],
        "duplicate_skip_logged": True,
        "poison_marker": poison_marker,
        "poison_message_id": poison_message_id,
        "poison_message_found_in_dlq": dlq_message is not None,
        "poison_lambda_failure_logs": len(poison_logs),
        "dlq_receive_count": dlq_message.get("receive_count_at_dlq"),
        "alarm_name": alarm["AlarmName"],
        "alarm_state": alarm["StateValue"],
        "alarm_state_updated_at": alarm["StateUpdatedTimestamp"].isoformat(),
        "alarm_state_before_test": baseline_alarm_state,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Confirm that live API and AWS operations may be performed.")
    parser.add_argument("--api-url", default=os.getenv("FACAI_API_URL"))
    parser.add_argument("--sender-account-id", default=os.getenv("FACAI_SENDER_ACCOUNT_ID"))
    parser.add_argument("--receiver-account-id", default=os.getenv("FACAI_RECEIVER_ACCOUNT_ID"))
    parser.add_argument("--ledger-table", default=os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger"))
    parser.add_argument("--events-queue-url", default=os.getenv("EVENTS_QUEUE_URL"))
    parser.add_argument("--events-dlq-url", default=os.getenv("EVENTS_DLQ_URL"))
    parser.add_argument(
        "--lambda-log-group",
        default=os.getenv("LAMBDA_LOG_GROUP", "/aws/lambda/facaibank-dev-lambda-notifier"),
    )
    parser.add_argument(
        "--dlq-alarm-name",
        default=os.getenv("DLQ_ALARM_NAME", "facaibank-dev-events-dlq-visible"),
    )
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--evidence-file",
        type=Path,
        help="Evidence JSON output path (default: tests/evidence/validation-<UTC timestamp>.json).",
    )
    args = parser.parse_args(argv)

    if not args.execute:
        parser.error("live checks require the explicit --execute flag")
    required = {
        "--api-url": args.api_url,
        "--sender-account-id": args.sender_account_id,
        "--receiver-account-id": args.receiver_account_id,
        "--events-queue-url": args.events_queue_url,
        "--events-dlq-url": args.events_dlq_url,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(f"missing required values: {', '.join(missing)}")
    if args.timeout_seconds < 30:
        parser.error("--timeout-seconds must be at least 30")
    return args


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    session = boto3.Session(region_name=args.region)
    clients = {
        "dynamodb": session.resource("dynamodb"),
        "sqs": session.client("sqs"),
        "logs": session.client("logs"),
        "cloudwatch": session.client("cloudwatch"),
    }
    table = clients["dynamodb"].Table(args.ledger_table)
    run_id = uuid.uuid4().hex[:12]
    evidence = {
        "project": "FaCaiBank",
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "region": args.region,
        "api_url": args.api_url,
        "tests": {},
    }
    evidence["tests"]["A_concurrent_acid"] = verify_concurrent_transfers(
        args.api_url,
        table,
        args.sender_account_id,
        args.receiver_account_id,
        run_id,
    )
    evidence["tests"]["B_idempotency_replay"] = verify_idempotent_replay(
        args.api_url,
        table,
        args.sender_account_id,
        args.receiver_account_id,
        run_id,
    )
    evidence["tests"]["C_sqs_deduplication_and_dlq"] = verify_sqs_deduplication_and_dlq(
        args,
        clients,
        run_id,
    )
    evidence["status"] = "PASS"
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    return evidence


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        evidence = run_validation(args)
    except Exception as error:
        LOGGER.exception("Live validation failed")
        evidence = {
            "project": "FaCaiBank",
            "status": "FAIL",
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
        }
        evidence_path = args.evidence_file or (
            Path(__file__).parent
            / "evidence"
            / f"validation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        output = json.dumps(evidence, indent=2, default=str)
        print(output, file=sys.stderr)
        print(f"Failure evidence saved to {evidence_path}", file=sys.stderr)
        return 1

    evidence_path = args.evidence_file or (
        Path(__file__).parent
        / "evidence"
        / f"validation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, default=str))
    print(f"Evidence saved to {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
