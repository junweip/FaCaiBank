import json
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from app.lambda_notifier import handler
from app.workers import outbox_worker, statement_worker


class FakeDynamoTable:
    def __init__(self, items=None):
        self.items = items or []
        self.put_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.query_calls = []
        self.get_calls = []

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)

    def delete_item(self, **kwargs):
        self.delete_calls.append(kwargs)

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        return {}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"Items": self.items}


class FakeSns:
    def __init__(self):
        self.publish_calls = []

    def publish(self, **kwargs):
        self.publish_calls.append(kwargs)


class FakeS3:
    def __init__(self):
        self.put_calls = []
        self.url_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def generate_presigned_url(self, operation, **kwargs):
        self.url_calls.append((operation, kwargs))
        return "https://example.test/signed-statement"


def test_outbox_worker_publishes_only_inserted_outbox_payload():
    record = {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "PK": {"S": "OUTBOX#transfer-1"},
                "SK": {"S": "EVENT"},
                "payload": {
                    "M": {
                        "event_id": {"S": "event-1"},
                        "amount": {"S": "25.00"},
                    }
                },
            }
        },
    }

    assert outbox_worker.outbox_payload(record) == {
        "event_id": "event-1",
        "amount": "25.00",
    }
    record["eventName"] = "MODIFY"
    assert outbox_worker.outbox_payload(record) is None
    record["eventName"] = "INSERT"
    record["dynamodb"]["NewImage"]["PK"] = {"S": "ACCOUNT#account-1"}
    assert outbox_worker.outbox_payload(record) is None


def test_outbox_worker_retries_sqs_send_with_exponential_backoff(monkeypatch):
    class FlakySqs:
        def __init__(self):
            self.calls = 0

        def send_message(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ClientError(
                    {"Error": {"Code": "ServiceUnavailable", "Message": "retry"}},
                    "SendMessage",
                )

    sqs = FlakySqs()
    delays = []
    monkeypatch.setattr(outbox_worker, "running", True)
    monkeypatch.setattr(outbox_worker.time, "sleep", delays.append)
    monkeypatch.setattr(outbox_worker.random, "uniform", lambda low, high: high)

    outbox_worker.send_with_backoff(
        sqs,
        "https://sqs.example/events",
        {"event_id": "event-1"},
    )

    assert sqs.calls == 2
    assert delays == [1.0]


def test_lambda_formats_transfer_alert_and_marks_dedupe_record(monkeypatch):
    table = FakeDynamoTable()
    sns = FakeSns()
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:alerts")
    payload = {
        "event_id": "event-1",
        "transfer_id": "transfer-1",
        "sender_account_id": "account-a",
        "receiver_account_id": "account-b",
        "amount": "42.50",
        "currency": "USD",
        "created_at": "2026-10-21T12:00:00Z",
    }

    handler.process_record({"body": json.dumps(payload)}, table=table, sns_client=sns)

    assert table.put_calls[0]["Item"] == {
        "PK": "DEDUPE#event-1",
        "SK": "NOTIFICATION",
        "status": "PENDING",
    }
    assert table.update_calls[0]["ExpressionAttributeValues"] == {":sent": "SENT"}
    assert "Amount: $42.50 USD" in sns.publish_calls[0]["Message"]
    assert "Transfer ID: transfer-1" in sns.publish_calls[0]["Message"]


def test_lambda_skips_duplicate_notification(monkeypatch):
    table = FakeDynamoTable()
    sns = FakeSns()
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:alerts")

    def existing_item(**kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "duplicate",
                }
            },
            "PutItem",
        )

    table.put_item = existing_item
    table.get_item = lambda **kwargs: {"Item": {"status": "SENT"}}
    handler.process_record(
        {"body": json.dumps({"event_id": "event-duplicate"})},
        table=table,
        sns_client=sns,
    )

    assert sns.publish_calls == []


def test_lambda_retries_unresolved_pending_dedupe_claim():
    table = FakeDynamoTable()

    def existing_item(**kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "duplicate",
                }
            },
            "PutItem",
        )

    table.put_item = existing_item
    table.get_item = lambda **kwargs: {"Item": {"status": "PENDING"}}

    with pytest.raises(RuntimeError, match="still pending"):
        handler.process_record(
            {"body": json.dumps({"event_id": "event-pending"})},
            table=table,
            sns_client=FakeSns(),
        )


def test_lambda_force_fail_returns_batch_failure_without_dedupe_claim(monkeypatch):
    table = FakeDynamoTable()
    monkeypatch.setattr(handler, "get_table", lambda: table)
    record = {
        "messageId": "sqs-message-1",
        "body": json.dumps({"event_id": "event-fail", "force_fail": True}),
    }

    response = handler.lambda_handler({"Records": [record]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "sqs-message-1"}]}
    assert table.get_calls[0]["Key"]["PK"] == "DEDUPE#event-fail"
    assert table.put_calls == []
    assert table.delete_calls == []


def test_statement_worker_queries_ledger_uploads_encrypted_html_and_stores_url(monkeypatch):
    table = FakeDynamoTable(
        items=[
            {
                "PK": "ACCOUNT#account-1",
                "SK": "TXN#2026-10-21#transfer-1",
                "entry_type": "DEBIT",
                "transfer_id": "transfer-1",
                "amount": Decimal("12.50"),
                "created_at": "2026-10-21T12:00:00+00:00",
            }
        ]
    )
    s3 = FakeS3()
    monkeypatch.setattr(statement_worker, "BUCKET_NAME", "kyc-statements")
    job = {
        "job_id": "job-1",
        "account_id": "account-1",
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
    }

    result = statement_worker.process_job(job, table=table, s3=s3)

    assert result["download_url"] == "https://example.test/signed-statement"
    assert table.query_calls[0]["ConsistentRead"] is False
    upload = s3.put_calls[0]
    assert upload["ServerSideEncryption"] == "aws:kms"
    assert upload["Bucket"] == "kyc-statements"
    assert b"-$12.50" in upload["Body"]
    assert table.put_calls[0]["Item"]["PK"] == "STATEMENT#job-1"
    assert table.put_calls[0]["Item"]["download_url"] == result["download_url"]
    assert s3.url_calls[0][0] == "get_object"


def test_statement_html_escapes_transaction_fields():
    content = statement_worker.statement_html(
        {"account_id": "<account>"},
        [
            {
                "entry_type": "CREDIT",
                "transfer_id": "<script>alert(1)</script>",
                "amount": Decimal("2.00"),
                "created_at": "2026-10-21T12:00:00Z",
            }
        ],
    )

    assert "&lt;account&gt;" in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "<script>alert(1)</script>" not in content


def test_statement_worker_rejects_invalid_date_range():
    with pytest.raises(ValueError, match="on or after"):
        statement_worker.process_job(
            {
                "job_id": "job-1",
                "account_id": "account-1",
                "start_date": "2026-10-31",
                "end_date": "2026-10-01",
            },
            table=FakeDynamoTable(),
            s3=FakeS3(),
        )
