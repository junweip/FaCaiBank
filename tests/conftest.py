import os
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
os.environ["EXTERNAL_BANK_LATENCY_MS"] = "0"

from app.backend_api import auth, cache, config, db, fraud, ledger, main  # noqa: E402


REGION = "ap-southeast-1"
TABLE = "test-ledger"
BUCKET = "test-kyc-statements"


class FakeCursor:
    def __init__(self, database):
        self.database = database
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, parameters=None):
        self.database.statements.append((" ".join(statement.split()), parameters))
        if self.database.error:
            error, self.database.error = self.database.error, None
            raise error
        self._result = self.database.results.pop(0) if self.database.results else None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class FakeDatabase:
    def __init__(self):
        self.statements = []
        self.results = []
        self.error = None

    @contextmanager
    def connection(self):
        yield self

    def cursor(self):
        return FakeCursor(self)


def make_user(subject="user-a", auth_age_seconds=10):
    now = int(time.time())
    return auth.AuthenticatedUser(
        subject=subject,
        username=subject,
        auth_time=now - auth_age_seconds,
        token_id=f"jti-{uuid.uuid4()}",
        expires_at=now + 3600,
        access_token="test-access-token",
    )


def make_profile(subject="user-a", kyc_status="VERIFIED_MOCK", accounts=None):
    return {
        "customer_id": str(uuid.uuid5(uuid.NAMESPACE_URL, subject)),
        "full_name": f"Customer {subject}",
        "email": f"{subject}@example.com",
        "kyc_status": kyc_status,
        "kyc_verified_at": None,
        "alert_subscription_arn": None,
        "created_at": "2026-10-01T00:00:00+00:00",
        "accounts": accounts or [],
    }


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        settings = config.Settings(
            ledger_table_name=TABLE,
            kyc_statements_bucket=BUCKET,
            aws_region=REGION,
            cognito_user_pool_id="ap-southeast-1_testpool",
            cognito_app_client_id="test-client",
            metrics_enabled=False,
            external_bank_latency_ms=0,
            fraud_endpoint_name="",
        )
        monkeypatch.setattr(config, "settings", settings)
        for factory in (ledger.dynamodb_client, cache.get_cache, main.s3_client, main.sqs_client, main.sns_client):
            factory.cache_clear()
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
        )
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield dynamodb
        for factory in (ledger.dynamodb_client, cache.get_cache, main.s3_client, main.sqs_client, main.sns_client):
            factory.cache_clear()


@pytest.fixture
def bank(aws, monkeypatch):
    """API test harness: in-memory AWS, a fake PostgreSQL and switchable users."""

    class Bank:
        def __init__(self):
            self.profiles = {}
            self.database = FakeDatabase()
            self.user = make_user()

        def as_user(self, subject, auth_age_seconds=10):
            self.user = make_user(subject, auth_age_seconds)
            return self.user

        def add_customer(self, subject, kyc_status="VERIFIED_MOCK"):
            self.profiles[subject] = make_profile(subject, kyc_status)
            return self.profiles[subject]

        def add_account(self, subject, balance="0.00"):
            profile = self.profiles.get(subject) or self.add_customer(subject)
            account_id = str(uuid.uuid4())
            ledger.create_account_balance(account_id, subject, profile["customer_id"], "EVERYDAY", "SGD")
            if Decimal(balance):
                aws.update_item(
                    TableName=TABLE,
                    Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "BALANCE"}},
                    UpdateExpression="SET #b = :b",
                    ExpressionAttributeNames={"#b": "balance"},
                    ExpressionAttributeValues={":b": {"N": balance}},
                )
            profile["accounts"].append({"account_id": account_id, "account_type": "EVERYDAY"})
            return account_id

        def balance(self, account_id):
            item = ledger.get_balance_item(account_id, consistent=True)
            return Decimal(str(item["balance"])) if item else None

        def items(self, prefix):
            response = aws.scan(TableName=TABLE)
            return [
                ledger.from_av(item)
                for item in response["Items"]
                if item["PK"]["S"].startswith(prefix)
            ]

    harness = Bank()
    monkeypatch.setattr(main, "load_profile", lambda subject, use_cache=True: harness.profiles.get(subject))
    monkeypatch.setattr(db, "primary", harness.database)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    main.app.dependency_overrides[auth.current_user] = lambda: harness.user
    yield harness
    main.app.dependency_overrides.clear()
