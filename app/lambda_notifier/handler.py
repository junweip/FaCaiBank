"""SQS events -> per-customer SNS alerts, deduplicated by event ID and recipient.

Each outbox event lists its recipients (sender, receiver or account holder).
Every alert carries a ``customer_sub`` message attribute, so a customer's SNS
email subscription (filter policy on that attribute) only receives their own
alerts. Delivery may repeat; a DEDUPE record per event and recipient stops
duplicates.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)
DEDUPE_TTL_SECONDS = 30 * 24 * 3600
SINGAPORE = timezone(timedelta(hours=8))

HEADLINES = {
    ("TRANSFER_COMPLETED", "SENDER"): "You sent {amount} to account ending {receiver}.",
    ("TRANSFER_COMPLETED", "RECEIVER"): "You received {amount} from account ending {sender}.",
    ("DEPOSIT_COMPLETED", "ACCOUNT_HOLDER"): "{amount} from {bank} was deposited into your account ending {account}.",
    ("WITHDRAWAL_COMPLETED", "ACCOUNT_HOLDER"): "{amount} was withdrawn from your account ending {account} to {bank}.",
    ("WITHDRAWAL_REVERSED", "ACCOUNT_HOLDER"): (
        "Your withdrawal of {amount} to {bank} was declined and the money is back in your account ending {account}."
    ),
}


def get_table():
    table_name = os.environ["TABLE_NAME"]
    return boto3.resource("dynamodb").Table(table_name)


def get_sns_client():
    return boto3.client("sns")


def _ending(account_id) -> str:
    return str(account_id or "unknown")[-4:]


def _amount(payload: dict) -> str:
    amount = payload.get("amount", "unknown")
    currency = payload.get("currency", "SGD")
    try:
        return f"{currency} {Decimal(str(amount)):,.2f}"
    except (ArithmeticError, ValueError):
        return f"{currency} {amount}"


def _local_time(value) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "unknown")
    return moment.astimezone(SINGAPORE).strftime("%d %b %Y, %H:%M SGT")


def recipients(payload: dict) -> list[dict]:
    listed = payload.get("recipients")
    if isinstance(listed, list):
        valid = [recipient for recipient in listed if isinstance(recipient, dict) and recipient.get("role")]
        if valid:
            return valid
    return [{"role": "ALL"}]


def format_alert(payload: dict, recipient: dict) -> tuple[str, str]:
    event_type = str(payload.get("event_type", "ACCOUNT_ACTIVITY"))
    template = HEADLINES.get((event_type, recipient.get("role")), "FaCaiBank account activity: {amount}.")
    headline = template.format(
        amount=_amount(payload),
        sender=_ending(payload.get("sender_account_id")),
        receiver=_ending(payload.get("receiver_account_id")),
        account=_ending(recipient.get("account_id") or payload.get("account_id")),
        bank=payload.get("bank_code", "your bank"),
    )
    message = "\n".join(
        [
            headline,
            "",
            f"Amount: {_amount(payload)}",
            f"Transaction ID: {payload.get('transaction_id') or payload.get('transfer_id', 'unknown')}",
            f"Time: {_local_time(payload.get('created_at'))}",
            f"Event ID: {payload['event_id']}",
            "",
            "If you don't recognise this, contact FaCaiBank immediately.",
        ]
    )
    subject = "FaCaiBank alert: " + event_type.replace("_", " ").lower()
    return subject[:100], message


def process_record(record: dict, table=None, sns_client=None) -> None:
    payload = json.loads(record["body"])
    if not isinstance(payload, dict):
        raise ValueError("SQS message body must be a JSON object")

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("SQS message body must contain a non-empty event_id")

    table = table or get_table()
    for recipient in recipients(payload):
        _notify(payload, recipient, table, sns_client)


def _notify(payload: dict, recipient: dict, table, sns_client) -> None:
    event_id = payload["event_id"]
    key = {"PK": f"DEDUPE#{event_id}", "SK": f"NOTIFICATION#{recipient['role']}"}

    if payload.get("force_fail") is True:
        if _skip_processed_notification(table, key, event_id):
            return
        raise RuntimeError(f"Forced notification failure for event_id={event_id}")

    try:
        table.put_item(
            Item={**key, "status": "PENDING", "expires_at": int(time.time()) + DEDUPE_TTL_SECONDS},
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

    subject, message = format_alert(payload, recipient)
    attributes = {
        "event_id": {"DataType": "String", "StringValue": event_id},
        "event_type": {"DataType": "String", "StringValue": str(payload.get("event_type", "UNKNOWN"))},
    }
    if recipient.get("customer_sub"):
        attributes["customer_sub"] = {"DataType": "String", "StringValue": recipient["customer_sub"]}
    try:
        sns_client = sns_client or get_sns_client()
        sns_client.publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Message=message,
            Subject=subject,
            MessageAttributes=attributes,
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
        logger.info("Skipping duplicate notification event_id=%s recipient=%s", event_id, key["SK"])
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
