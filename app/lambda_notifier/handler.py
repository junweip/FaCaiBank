import json
import logging
import os
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_table():
    table_name = os.environ["TABLE_NAME"]
    return boto3.resource("dynamodb").Table(table_name)


def get_sns_client():
    return boto3.client("sns")


def format_transfer_alert(payload: dict) -> str:
    amount = payload.get("amount", "unknown")
    try:
        amount = f"${Decimal(str(amount)):,.2f} {payload.get('currency', 'USD')}"
    except (ArithmeticError, ValueError):
        amount = f"{amount} {payload.get('currency', 'USD')}"

    return "\n".join(
        [
            "FaCaiBank transfer alert",
            f"Amount: {amount}",
            f"From account: {payload.get('sender_account_id', 'unknown')}",
            f"To account: {payload.get('receiver_account_id', 'unknown')}",
            f"Transfer ID: {payload.get('transfer_id', 'unknown')}",
            f"Event ID: {payload['event_id']}",
            f"Time: {payload.get('created_at', 'unknown')}",
        ]
    )


def process_record(record: dict, table=None, sns_client=None) -> None:
    payload = json.loads(record["body"])
    if not isinstance(payload, dict):
        raise ValueError("SQS message body must be a JSON object")

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("SQS message body must contain a non-empty event_id")

    table = table or get_table()
    key = {
        "PK": f"DEDUPE#{event_id}",
        "SK": "NOTIFICATION",
    }

    if payload.get("force_fail") is True:
        if _skip_processed_notification(table, key, event_id):
            return
        raise RuntimeError(f"Forced notification failure for event_id={event_id}")

    try:
        table.put_item(
            Item={**key, "status": "PENDING"},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            if _skip_processed_notification(table, key, event_id):
                return
            raise RuntimeError(
                f"Notification event_id={event_id} has an unresolved dedupe claim"
            ) from error
        raise

    try:
        sns_client = sns_client or get_sns_client()
        sns_client.publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Message=format_transfer_alert(payload),
            Subject="FaCaiBank transfer alert",
            MessageAttributes={
                "event_id": {
                    "DataType": "String",
                    "StringValue": event_id,
                }
            },
        )
    except Exception:
        _release_dedupe_claim(table, key, event_id)
        raise

    table.update_item(
        Key=key,
        UpdateExpression="SET #status = :sent",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":sent": "SENT"},
    )


def _skip_processed_notification(table, key: dict, event_id: str) -> bool:
    existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not existing:
        return False
    if existing.get("status") == "SENT":
        logger.info("Skipping duplicate notification event_id=%s", event_id)
        return True
    raise RuntimeError(f"Notification event_id={event_id} is still pending")


def _release_dedupe_claim(table, key: dict, event_id: str) -> None:
    try:
        table.delete_item(
            Key=key,
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError:
        logger.exception("Unable to clear deduplication claim for event_id=%s", event_id)


def lambda_handler(event, context):
    failures = []

    for record in event.get("Records", []):
        try:
            process_record(record)
        except Exception:
            logger.exception("Unable to process SQS message %s", record.get("messageId", "unknown"))
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
