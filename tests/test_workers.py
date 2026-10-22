import json
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from app.lambda_notifier import handler
from app.workers import outbox_worker, statement_worker


REGION = "ap-southeast-1"


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

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


def conditional_failure(**kwargs):
    raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem")


def stream_record(sequence, pk="OUTBOX#txn-1", event_id="event-1", event_name="INSERT"):
    image = {"PK": {"S": pk}, "SK": {"S": "EVENT"}}
    if pk.startswith("OUTBOX#"):
        image["payload"] = {"M": {"event_id": {"S": event_id}, "amount": {"S": "25.00"}}}
    return {
        "eventName": event_name,
        "dynamodb": {
            "SequenceNumber": sequence,
            "ApproximateCreationDateTime": datetime.now(timezone.utc),
            "NewImage": image,
        },
    }


# ---------------------------------------------------------------- outbox worker


def test_outbox_worker_publishes_only_inserted_outbox_payload():
    record = stream_record("1")

    assert outbox_worker.outbox_payload(record) == {"event_id": "event-1", "amount": "25.00"}
    record["eventName"] = "MODIFY"
    assert outbox_worker.outbox_payload(record) is None
    assert outbox_worker.outbox_payload(stream_record("2", pk="ACCOUNT#a")) is None


def test_outbox_worker_retries_sqs_send_with_exponential_backoff(monkeypatch):
    class FlakySqs:
        def __init__(self):
            self.calls = 0

        def send_message(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "retry"}}, "SendMessage")

    sqs = FlakySqs()
    delays = []
    monkeypatch.setattr(outbox_worker, "running", True)
    monkeypatch.setattr(outbox_worker.time, "sleep", delays.append)
    monkeypatch.setattr(outbox_worker.random, "uniform", lambda low, high: high)

    outbox_worker.send_with_backoff(sqs, "https://sqs.example/events", {"event_id": "event-1"})

    assert sqs.calls == 2
    assert delays == [1.0]


@pytest.fixture
def lease_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="leases",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        yield table


def test_shard_lease_is_exclusive_until_it_expires(lease_table):
    clock = [1_000.0]
    first = outbox_worker.LeaseManager(lease_table, "worker-a", lease_seconds=30, clock=lambda: clock[0])
    second = outbox_worker.LeaseManager(lease_table, "worker-b", lease_seconds=30, clock=lambda: clock[0])

    assert first.acquire("shard-1") is not None
    assert second.acquire("shard-1") is None
    assert first.checkpoint("shard-1", "000123")
    assert not second.checkpoint("shard-1", "000999")

    clock[0] += 31
    takeover = second.acquire("shard-1")
    assert takeover["checkpoint"] == "000123"
    assert not first.renew("shard-1")


def test_finished_shards_are_never_reacquired(lease_table):
    leases = outbox_worker.LeaseManager(lease_table, "worker-a")
    leases.acquire("shard-1")
    leases.finish("shard-1")

    assert outbox_worker.LeaseManager(lease_table, "worker-b").acquire("shard-1") is None
    assert leases.leases()["shard-1"]["finished"] is True


def test_workers_split_shards_fairly(lease_table):
    assert outbox_worker.target_lease_count(4, 1) == 4
    assert outbox_worker.target_lease_count(4, 3) == 2
    assert outbox_worker.target_lease_count(0, 2) == 0

    clock = [1_000.0]
    for worker in ("worker-a", "worker-b"):
        outbox_worker.LeaseManager(lease_table, worker, clock=lambda: clock[0]).heartbeat()
    assert outbox_worker.LeaseManager(lease_table, "worker-a", clock=lambda: clock[0]).active_workers() == 2
    clock[0] += 120
    assert outbox_worker.LeaseManager(lease_table, "worker-a", clock=lambda: clock[0]).active_workers() == 1


class FakeStreams:
    def __init__(self, shards, records):
        self.shards = shards
        self.records = records
        self.iterator_requests = []

    def describe_stream(self, **kwargs):
        return {"StreamDescription": {"Shards": [{"ShardId": shard} for shard in self.shards]}}

    def get_shard_iterator(self, **kwargs):
        self.iterator_requests.append(kwargs)
        return {"ShardIterator": f"it-{kwargs['ShardId']}"}

    def get_records(self, ShardIterator, Limit):
        shard_id = ShardIterator.removeprefix("it-")
        return {"Records": self.records.pop(shard_id, []), "NextShardIterator": ShardIterator}


class RecordingSqs:
    def __init__(self):
        self.bodies = []

    def send_message(self, **kwargs):
        self.bodies.append(json.loads(kwargs["MessageBody"]))


def test_restarted_worker_resumes_after_its_checkpoint(lease_table, monkeypatch):
    monkeypatch.setattr(outbox_worker, "running", True)
    streams = FakeStreams(["shard-1"], {"shard-1": [stream_record("100", event_id="e1"), stream_record("101", event_id="e2")]})
    sqs = RecordingSqs()
    first = outbox_worker.OutboxPublisher(
        streams, sqs, outbox_worker.LeaseManager(lease_table, "worker-a"), "queue", "stream-arn"
    )

    first.coordinate()
    first.poll()
    first.shutdown()

    assert [body["event_id"] for body in sqs.bodies] == ["e1", "e2"]
    assert streams.iterator_requests[0]["ShardIteratorType"] == "TRIM_HORIZON"

    replacement = outbox_worker.OutboxPublisher(
        streams, sqs, outbox_worker.LeaseManager(lease_table, "worker-b"), "queue", "stream-arn"
    )
    replacement.coordinate()

    assert streams.iterator_requests[-1]["ShardIteratorType"] == "AFTER_SEQUENCE_NUMBER"
    assert streams.iterator_requests[-1]["SequenceNumber"] == "101"


def test_new_worker_takes_released_shards_when_the_fleet_grows(lease_table, monkeypatch):
    monkeypatch.setattr(outbox_worker, "running", True)
    streams = FakeStreams(["shard-1", "shard-2"], {})
    first = outbox_worker.OutboxPublisher(
        streams, RecordingSqs(), outbox_worker.LeaseManager(lease_table, "worker-a"), "queue", "arn"
    )
    first.coordinate()
    assert len(first.iterators) == 2

    second = outbox_worker.OutboxPublisher(
        streams, RecordingSqs(), outbox_worker.LeaseManager(lease_table, "worker-b"), "queue", "arn"
    )
    second.coordinate()
    first.coordinate()
    second.coordinate()

    assert len(first.iterators) == 1
    assert len(second.iterators) == 1
    assert set(first.iterators).isdisjoint(second.iterators)


def test_worker_metrics_report_iterator_age_and_volume():
    metrics = outbox_worker.WorkerMetrics(enabled=False, interval=60, clock=lambda: 0)
    old = stream_record("1")
    old["dynamodb"]["ApproximateCreationDateTime"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    metrics.observe(old)
    metrics.published = 3

    data = {datum["MetricName"]: datum for datum in metrics.data(owned_shards=2)}

    assert data["OutboxIteratorAgeMilliseconds"]["Value"] > 60_000
    assert data["OutboxEventsPublished"]["Value"] == 3
    assert data["OutboxOwnedShards"]["Value"] == 2


# ---------------------------------------------------------------- notifier Lambda


def transfer_event(**overrides):
    payload = {
        "event_id": "event-1",
        "event_type": "TRANSFER_COMPLETED",
        "transaction_id": "transfer-1",
        "sender_account_id": "aaaa-1111",
        "receiver_account_id": "bbbb-2222",
        "amount": "42.50",
        "currency": "SGD",
        "created_at": "2026-10-21T12:00:00+00:00",
        "recipients": [
            {"role": "SENDER", "customer_sub": "sub-a", "account_id": "aaaa-1111"},
            {"role": "RECEIVER", "customer_sub": "sub-b", "account_id": "bbbb-2222"},
        ],
    }
    payload.update(overrides)
    return payload


def test_lambda_sends_one_targeted_alert_per_recipient(monkeypatch):
    table = FakeDynamoTable()
    sns = FakeSns()
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:customer-notifications")

    handler.process_record({"body": json.dumps(transfer_event())}, table=table, sns_client=sns)

    assert [call["Item"]["SK"] for call in table.put_calls] == ["NOTIFICATION#SENDER", "NOTIFICATION#RECEIVER"]
    assert all(call["Item"]["PK"] == "DEDUPE#event-1" for call in table.put_calls)
    sender_alert, receiver_alert = sns.publish_calls
    assert sender_alert["MessageAttributes"]["customer_sub"]["StringValue"] == "sub-a"
    assert "You sent SGD 42.50 to account ending 2222" in sender_alert["Message"]
    assert "You received SGD 42.50 from account ending 1111" in receiver_alert["Message"]
    assert "21 Oct 2026, 20:00 SGT" in sender_alert["Message"]
    assert [call["ExpressionAttributeValues"] for call in table.update_calls] == [{":sent": "SENT"}] * 2


def test_lambda_formats_withdrawal_reversal():
    subject, message = handler.format_alert(
        {"event_id": "e", "event_type": "WITHDRAWAL_REVERSED", "amount": "25000", "currency": "SGD", "bank_code": "DBS"},
        {"role": "ACCOUNT_HOLDER", "account_id": "acct-9876"},
    )

    assert "declined" in message and "ending 9876" in message and "DBS" in message
    assert subject == "FaCaiBank alert: withdrawal reversed"


def test_lambda_skips_duplicate_notification(monkeypatch):
    table = FakeDynamoTable()
    sns = FakeSns()
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:alerts")
    table.put_item = conditional_failure
    table.get_item = lambda **kwargs: {"Item": {"status": "SENT"}}

    handler.process_record({"body": json.dumps(transfer_event())}, table=table, sns_client=sns)

    assert sns.publish_calls == []


def test_lambda_retries_unresolved_pending_dedupe_claim():
    table = FakeDynamoTable()
    table.put_item = conditional_failure
    table.get_item = lambda **kwargs: {"Item": {"status": "PENDING"}}

    with pytest.raises(RuntimeError, match="still pending"):
        handler.process_record({"body": json.dumps({"event_id": "event-pending"})}, table=table, sns_client=FakeSns())


def test_lambda_releases_claim_when_sns_publish_fails(monkeypatch):
    table = FakeDynamoTable()
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:alerts")

    class BrokenSns:
        def publish(self, **kwargs):
            raise ClientError({"Error": {"Code": "Throttled", "Message": "slow down"}}, "Publish")

    with pytest.raises(ClientError):
        handler.process_record({"body": json.dumps(transfer_event())}, table=table, sns_client=BrokenSns())
    assert table.delete_calls[0]["Key"] == {"PK": "DEDUPE#event-1", "SK": "NOTIFICATION#SENDER"}


def test_lambda_force_fail_returns_batch_failure_without_dedupe_claim(monkeypatch):
    table = FakeDynamoTable()
    monkeypatch.setattr(handler, "get_table", lambda: table)
    record = {"messageId": "sqs-message-1", "body": json.dumps({"event_id": "event-fail", "force_fail": True})}

    response = handler.lambda_handler({"Records": [record]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "sqs-message-1"}]}
    assert table.get_calls[0]["Key"] == {"PK": "DEDUPE#event-fail", "SK": "NOTIFICATION#ALL"}
    assert table.put_calls == []


def test_lambda_rejects_malformed_messages_for_the_dlq():
    response = handler.lambda_handler({"Records": [{"messageId": "m1", "body": "{\"no_event\": true}"}]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


# ---------------------------------------------------------------- statement worker


LEDGER_ITEMS = [
    {
        "PK": "ACCOUNT#account-1",
        "SK": "TXN#2026-10-21#transfer-1",
        "transaction_type": "TRANSFER",
        "entry_type": "DEBIT",
        "transaction_id": "transfer-1",
        "amount": Decimal("12.50"),
        "created_at": "2026-10-21T12:00:00+00:00",
    },
    {
        "PK": "ACCOUNT#account-1",
        "SK": "TXN#2026-10-02#deposit-1",
        "transaction_type": "DEPOSIT",
        "entry_type": "CREDIT",
        "bank_code": "DBS",
        "transaction_id": "deposit-1",
        "amount": Decimal("500.00"),
        "created_at": "2026-10-02T01:00:00+00:00",
    },
]


def test_statement_worker_uploads_encrypted_pdf_and_marks_job_complete(monkeypatch):
    table = FakeDynamoTable(items=LEDGER_ITEMS)
    s3 = FakeS3()
    monkeypatch.setattr(statement_worker, "BUCKET_NAME", "kyc-statements")
    job = {
        "job_id": "job-1",
        "account_id": "account-1",
        "owner_sub": "sub-a",
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
    }

    result = statement_worker.process_job(
        job, table=table, s3=s3, holder_lookup=lambda account: {"full_name": "Ari Chen", "account_type": "EVERYDAY"}
    )

    assert result == {"job_id": "job-1", "object_key": "statements/account-1/job-1.pdf", "transaction_count": 2}
    assert table.query_calls[0]["ConsistentRead"] is False
    upload = s3.put_calls[0]
    assert upload["ServerSideEncryption"] == "aws:kms"
    assert upload["ContentType"] == "application/pdf"
    assert upload["Body"].startswith(b"%PDF")
    record = table.put_calls[0]["Item"]
    assert (record["PK"], record["SK"], record["status"], record["owner_sub"]) == (
        "ACCOUNT#account-1",
        "STATEMENT#job-1",
        "COMPLETED",
        "sub-a",
    )


def test_statement_rows_describe_each_transaction_in_singapore_time():
    rows = statement_worker.statement_rows(LEDGER_ITEMS)

    assert [row["description"] for row in rows] == ["Deposit from DBS", "Transfer sent"]
    assert rows[1]["amount"] == "-12.50"
    assert rows[1]["date"] == "21 Oct 2026 20:00"


def test_statement_pdf_contains_holder_and_totals():
    rows = statement_worker.statement_rows(LEDGER_ITEMS)

    content = statement_worker.render_statement_pdf(
        {"account_id": "account-1", "start_date": "2026-10-01", "end_date": "2026-10-31"},
        {"full_name": "Zoë Tan"},
        rows,
        compress=False,
    )

    assert b"(Zo\xeb Tan)" in content
    assert b"Net change" in content
    assert b"487.50" in content


def test_statement_worker_rejects_invalid_date_range():
    with pytest.raises(ValueError, match="on or after"):
        statement_worker.process_job(
            {"job_id": "job-1", "account_id": "account-1", "start_date": "2026-10-31", "end_date": "2026-10-01"},
            table=FakeDynamoTable(),
            s3=FakeS3(),
        )


def test_monthly_run_queues_one_job_per_account_once(monkeypatch):
    class BatchSqs:
        def __init__(self):
            self.entries = []

        def send_message_batch(self, **kwargs):
            self.entries.extend(kwargs["Entries"])
            return {"Successful": kwargs["Entries"], "Failed": []}

    table = FakeDynamoTable()
    sqs = BatchSqs()
    monkeypatch.setattr(statement_worker, "QUEUE_URL", "https://sqs.example/statement-jobs")
    accounts = [{"account_id": f"acct-{index}", "owner_sub": f"sub-{index}"} for index in range(12)]

    queued = statement_worker.fan_out_monthly(table, sqs, lambda: accounts, today=date(2026, 11, 1))

    assert queued == 12
    job = json.loads(sqs.entries[0]["MessageBody"])
    assert (job["start_date"], job["end_date"], job["type"]) == ("2026-10-01", "2026-10-31", "MONTHLY")

    table.put_item = conditional_failure
    again = BatchSqs()
    assert statement_worker.fan_out_monthly(table, again, lambda: accounts, today=date(2026, 11, 1)) == 0
    assert again.entries == []


def test_previous_month_handles_year_boundaries():
    assert statement_worker.previous_month(date(2027, 1, 1)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert statement_worker.previous_month(date(2026, 3, 15)) == (date(2026, 2, 1), date(2026, 2, 28))
