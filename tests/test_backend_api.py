from decimal import Decimal
from uuid import UUID

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.backend_api import main


client = TestClient(main.app)


class FakeDynamoClient:
    def __init__(self, transact_error=None, items=None):
        self.transact_error = transact_error
        self.items = items or {}
        self.transact_calls = []
        self.get_calls = []

    def transact_write_items(self, **kwargs):
        self.transact_calls.append(kwargs)
        if self.transact_error:
            raise self.transact_error

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        partition_key = kwargs["Key"]["PK"]["S"]
        return {"Item": self.items.get(partition_key)}


def configure_dynamo(monkeypatch, fake_client):
    monkeypatch.setattr(
        main,
        "settings",
        main.Settings(ledger_table_name="test-ledger"),
    )
    monkeypatch.setattr(main, "dynamodb_client", lambda: fake_client)


class FakeCursor:
    def __init__(self, result=None):
        self.statements = []
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        pass


def test_transfer_submits_one_atomic_transaction(monkeypatch):
    fake_client = FakeDynamoClient()
    configure_dynamo(monkeypatch, fake_client)
    sender = str(UUID("86a88f7e-f836-495a-991a-22b74fae3331"))
    receiver = str(UUID("20f6582e-2cd2-4bd7-af7d-796302be57c0"))

    response = client.post(
        "/transfers",
        headers={"Idempotency-Key": "transfer-001"},
        json={
            "sender_account_id": sender,
            "receiver_account_id": receiver,
            "amount": "12.35",
        },
    )

    assert response.status_code == 201
    assert len(fake_client.transact_calls) == 1
    operations = fake_client.transact_calls[0]["TransactItems"]
    assert [next(iter(operation)) for operation in operations] == [
        "Update",
        "Update",
        "Put",
        "Put",
        "Put",
        "Put",
    ]
    assert operations[0]["Update"]["ConditionExpression"].find("#balance >= :amount") >= 0
    assert operations[-1]["Put"]["Item"]["PK"]["S"] == "IDEMPOTENCY#transfer-001"


def test_transfer_requires_idempotency_key():
    response = client.post(
        "/transfers",
        json={
            "sender_account_id": "sender",
            "receiver_account_id": "receiver",
            "amount": "10.00",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Idempotency-Key header is required"


def test_account_creation_links_customer_and_stores_starting_balance(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)

    class FakeTable:
        def put_item(self, **kwargs):
            self.kwargs = kwargs

    table = FakeTable()
    monkeypatch.setattr(main, "connect_database", lambda: connection)
    monkeypatch.setattr(main, "ensure_database_schema", lambda connection: None)
    monkeypatch.setattr(main, "dynamodb_table", lambda: table)

    response = client.post(
        "/accounts",
        json={"full_name": "Ari Chen", "email": "ARI@example.com"},
    )

    assert response.status_code == 201
    assert [query.split(" ", 2)[2].split(" ", 1)[0] for query, _ in cursor.statements] == [
        "customers",
        "accounts",
    ]
    assert table.kwargs["Item"]["balance"] == Decimal("1000.00")
    assert table.kwargs["ConditionExpression"] == "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    assert response.json()["kyc_status"] == "PENDING"


def test_duplicate_idempotency_key_is_rejected_without_partial_writes(monkeypatch):
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Canceled"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )
    fake_client = FakeDynamoClient(
        transact_error=error,
        items={"IDEMPOTENCY#already-used": {"PK": {"S": "IDEMPOTENCY#already-used"}}},
    )
    configure_dynamo(monkeypatch, fake_client)

    response = client.post(
        "/transfers",
        headers={"Idempotency-Key": "already-used"},
        json={
            "sender_account_id": "sender",
            "receiver_account_id": "receiver",
            "amount": "10.00",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Idempotency-Key has already been used"
    assert len(fake_client.transact_calls) == 1
    assert len(fake_client.get_calls) == 1
    assert fake_client.get_calls[0]["ConsistentRead"] is True


def test_insufficient_funds_is_rejected_after_transaction_cancellation(monkeypatch):
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Canceled"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )
    fake_client = FakeDynamoClient(
        transact_error=error,
        items={"ACCOUNT#sender": {"balance": {"N": "4.00"}}},
    )
    configure_dynamo(monkeypatch, fake_client)

    response = client.post(
        "/transfers",
        headers={"Idempotency-Key": "not-enough"},
        json={
            "sender_account_id": "sender",
            "receiver_account_id": "receiver",
            "amount": "10.00",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient funds"
    assert len(fake_client.transact_calls) == 1


def test_mock_fraud_screen_blocks_high_value_transfer(monkeypatch):
    fake_client = FakeDynamoClient()
    configure_dynamo(monkeypatch, fake_client)

    response = client.post(
        "/transfers",
        headers={"Idempotency-Key": "high-risk"},
        json={
            "sender_account_id": "sender",
            "receiver_account_id": "receiver",
            "amount": "10000.00",
        },
    )

    assert response.status_code == 403
    assert fake_client.transact_calls == []


def test_balance_read_uses_strong_consistency(monkeypatch):
    class FakeTable:
        def get_item(self, **kwargs):
            self.kwargs = kwargs
            return {"Item": {"balance": Decimal("25.50"), "currency": "USD"}}

    table = FakeTable()
    monkeypatch.setattr(main, "dynamodb_table", lambda: table)

    response = client.get("/accounts/account-1/balance")

    assert response.status_code == 200
    assert response.json()["balance"] == "25.50"
    assert table.kwargs["ConsistentRead"] is True


def test_database_endpoint_accepts_terraform_output_with_port():
    assert main.parse_database_endpoint(
        "facaibank.example.ap-southeast-1.rds.amazonaws.com:5432",
        5432,
    ) == ("facaibank.example.ap-southeast-1.rds.amazonaws.com", 5432)


def test_transaction_history_read_is_eventually_consistent(monkeypatch):
    class FakeTable:
        def query(self, **kwargs):
            self.kwargs = kwargs
            return {"Items": [{"amount": Decimal("9.50"), "entry_type": "DEBIT"}]}

    table = FakeTable()
    monkeypatch.setattr(main, "dynamodb_table", lambda: table)

    response = client.get("/accounts/account-1/transactions")

    assert response.status_code == 200
    assert response.json()["transactions"][0]["amount"] == "9.50"
    assert table.kwargs["ConsistentRead"] is False


def test_kyc_upload_uses_explicit_kms_encryption_and_updates_status(monkeypatch):
    cursor = FakeCursor(result={"account_id": "account-1"})
    connection = FakeConnection(cursor)

    class FakeS3:
        def put_object(self, **kwargs):
            self.kwargs = kwargs

    fake_s3 = FakeS3()
    monkeypatch.setattr(
        main,
        "settings",
        main.Settings(kyc_statements_bucket="test-kyc-bucket"),
    )
    monkeypatch.setattr(main, "s3_client", lambda: fake_s3)
    monkeypatch.setattr(main, "connect_database", lambda: connection)
    monkeypatch.setattr(main, "ensure_database_schema", lambda connection: None)

    response = client.post(
        "/kyc/upload?account_id=account-1",
        files={"file": ("identity.pdf", b"demo-document", "application/pdf")},
    )

    assert response.status_code == 200
    assert fake_s3.kwargs["ServerSideEncryption"] == "aws:kms"
    assert fake_s3.kwargs["Bucket"] == "test-kyc-bucket"
    assert "UPDATE accounts SET kyc_status = 'VERIFIED_MOCK'" in cursor.statements[0][0]
    assert response.json()["kyc_status"] == "VERIFIED_MOCK"


def test_statement_request_enqueues_job(monkeypatch):
    class FakeSqs:
        def send_message(self, **kwargs):
            self.kwargs = kwargs

    fake_sqs = FakeSqs()
    monkeypatch.setattr(
        main,
        "settings",
        main.Settings(statement_queue_url="https://sqs.example/statement-jobs"),
    )
    monkeypatch.setattr(main, "sqs_client", lambda: fake_sqs)

    response = client.post(
        "/statements/request",
        json={"account_id": "account-1"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert fake_sqs.kwargs["QueueUrl"].endswith("/statement-jobs")
    assert response.json()["job_id"] in fake_sqs.kwargs["MessageBody"]
