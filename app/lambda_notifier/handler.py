import json
import logging
import os

import boto3
from botocore.exceptions import ClientError


dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")
table = dynamodb.Table(os.environ["TABLE_NAME"])
topic_arn = os.environ["TOPIC_ARN"]
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def process_record(record):
    payload = json.loads(record["body"])
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("SQS message body must contain a non-empty event_id")

    key = {
        "PK": f"DEDUPE#{event_id}",
        "SK": "NOTIFICATION",
    }

    try:
        table.put_item(
            Item={**key, "status": "PENDING"},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise

    try:
        sns.publish(
            TopicArn=topic_arn,
            Message=json.dumps(payload, separators=(",", ":")),
            Subject="FaCaiBank customer notification",
            MessageAttributes={
                "event_id": {
                    "DataType": "String",
                    "StringValue": event_id,
                }
            },
        )
    except Exception:
        try:
            table.delete_item(
                Key=key,
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError:
            logger.exception("Unable to clear deduplication claim for event %s", event_id)
        raise

    table.update_item(
        Key=key,
        UpdateExpression="SET #status = :sent",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":sent": "SENT"},
    )


def lambda_handler(event, context):
    failures = []

    for record in event.get("Records", []):
        try:
            process_record(record)
        except Exception:
            logger.exception("Unable to process SQS message %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
