import json
import uuid
from decimal import Decimal

import boto3
from fastapi.testclient import TestClient

from app.backend_api import config, fraud, ledger, main
from conftest import REGION


client = TestClient(main.app)


def key():
    return f"test-{uuid.uuid4()}"


def transfer(sender, receiver, amount, idempotency_key=None):
    return client.post(
        "/transfers",
        headers={"Idempotency-Key": idempotency_key or key()},
        json={"sender_account_id": sender, "receiver_account_id": receiver, "amount": amount},
    )


def total_ledger_balance(bank):
    return sum(Decimal(str(item["balance"])) for item in bank.items("ACCOUNT#") if item["SK"] == "BALANCE")


def test_health_reports_version():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "version" in response.json()


def test_banking_endpoints_require_a_bearer_token():
    response = client.get(f"/accounts/{uuid.uuid4()}/balance")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_open_account_creates_owned_zero_balance_and_metadata(bank):
    bank.add_customer("user-a")
    bank.database.results = [{"user_id": "c"}, {"count": 0}, None]

    response = client.post("/accounts", json={"account_type": "SAVINGS", "nickname": " Rainy  day "})

    assert response.status_code == 201
    account_id = response.json()["account_id"]
    item = ledger.get_balance_item(account_id, consistent=True)
    assert item["owner_sub"] == "user-a"
    assert item["balance"] == Decimal("0")
    assert item["currency"] == "SGD"
    statement, parameters = bank.database.statements[-1]
    assert statement.startswith("INSERT INTO accounts")
    assert parameters[3:5] == ("SAVINGS", "Rainy day")


def test_account_limits_are_two_per_type(bank):
    bank.add_customer("user-a")
    bank.profiles["user-a"]["accounts"] = [
        {"account_id": "a1", "account_type": "EVERYDAY"},
        {"account_id": "a2", "account_type": "EVERYDAY"},
        {"account_id": "a3", "account_type": "SAVINGS"},
    ]

    blocked = client.post("/accounts", json={"account_type": "EVERYDAY"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "You can hold up to 2 everyday accounts"

    # A concurrent request that slipped past the cached profile is stopped by the locked count.
    bank.database.results = [{"user_id": "c"}, {"count": 2}]
    raced = client.post("/accounts", json={"account_type": "SAVINGS"})
    assert raced.status_code == 409
    assert bank.database.statements[0][0].endswith("FOR UPDATE")
    assert bank.items("ACCOUNT#") == []


def test_open_account_requires_a_profile(bank):
    response = client.post("/accounts", json={})

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_required"


def test_balance_is_strongly_consistent_and_owner_only(bank, monkeypatch):
    account_id = bank.add_account("user-a", "25.50")
    calls = []
    original = ledger.get_balance_item
    monkeypatch.setattr(
        ledger,
        "get_balance_item",
        lambda account, consistent=False: calls.append(consistent) or original(account, consistent),
    )

    response = client.get(f"/accounts/{account_id}/balance")
    assert response.status_code == 200
    assert response.json()["balance"] == "25.50"
    assert calls == [True]

    bank.as_user("user-b")
    assert client.get(f"/accounts/{account_id}/balance").status_code == 404
    assert client.get("/accounts/not-a-uuid/balance").status_code == 404


def test_deposit_credits_account_and_posts_clearing_leg(bank):
    account_id = bank.add_account("user-a")
    idempotency_key = key()

    response = client.post(
        f"/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount": "500.00", "bank_code": "dbs", "external_account": "123456789"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["external_reference"].startswith("MOCK-DBS-")
    assert bank.balance(account_id) == Decimal("500.00")
    assert total_ledger_balance(bank) == Decimal("0")
    outbox = bank.items("OUTBOX#")
    assert outbox[0]["payload"]["event_type"] == "DEPOSIT_COMPLETED"
    assert outbox[0]["payload"]["recipients"][0]["customer_sub"] == "user-a"

    replay = client.post(
        f"/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount": "500.00", "bank_code": "DBS", "external_account": "123456789"},
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == body
    assert bank.balance(account_id) == Decimal("500.00")

    mismatch = client.post(
        f"/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount": "600.00", "bank_code": "DBS", "external_account": "123456789"},
    )
    assert mismatch.status_code == 409


def test_deposit_declined_by_partner_bank_writes_nothing(bank):
    account_id = bank.add_account("user-a")

    response = client.post(
        f"/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": key()},
        json={"amount": "50.00", "bank_code": "OCBC", "external_account": "111110000"},
    )

    assert response.status_code == 422
    assert "closed" in response.json()["detail"]
    assert bank.balance(account_id) == Decimal("0")
    assert bank.items("OUTBOX#") == []


def test_transfer_commits_both_legs_outbox_and_idempotency_atomically(bank):
    sender = bank.add_account("user-a", "300.00")
    receiver = bank.add_account("user-b")
    idempotency_key = key()

    response = transfer(sender, receiver, "120.50", idempotency_key)

    assert response.status_code == 201
    body = response.json()
    assert body["fraud_model"] == "rules-fallback"
    assert bank.balance(sender) == Decimal("179.50")
    assert bank.balance(receiver) == Decimal("120.50")
    legs = [item for item in bank.items("ACCOUNT#") if item["SK"].startswith("TXN#")]
    assert sorted(leg["entry_type"] for leg in legs) == ["CREDIT", "DEBIT"]
    assert {leg["transaction_id"] for leg in legs} == {body["transaction_id"]}
    event = bank.items("OUTBOX#")[0]["payload"]
    assert [recipient["customer_sub"] for recipient in event["recipients"]] == ["user-a", "user-b"]
    record = bank.items("IDEMPOTENCY#user-a#")[0]
    assert record["transaction_id"] == body["transaction_id"]

    replay = transfer(sender, receiver, "120.50", idempotency_key)
    assert replay.status_code == 201
    assert replay.json()["transaction_id"] == body["transaction_id"]
    assert bank.balance(sender) == Decimal("179.50")


def test_transfer_rejects_insufficient_funds_without_partial_writes(bank):
    sender = bank.add_account("user-a", "40.00")
    receiver = bank.add_account("user-b")

    response = transfer(sender, receiver, "40.01")

    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient funds"
    assert bank.balance(sender) == Decimal("40.00")
    assert bank.balance(receiver) == Decimal("0")
    assert bank.items("OUTBOX#") == []


def test_transfer_cannot_spend_from_someone_elses_account(bank):
    victim = bank.add_account("user-b", "900.00")
    attacker = bank.add_account("user-a")

    response = transfer(victim, attacker, "10.00")

    assert response.status_code == 404
    assert bank.balance(victim) == Decimal("900.00")


def test_transfer_to_unknown_account_is_rejected(bank):
    sender = bank.add_account("user-a", "100.00")

    response = transfer(sender, str(uuid.uuid4()), "10.00")

    assert response.status_code == 404
    assert response.json()["detail"] == "Recipient account was not found"


def test_transfer_requires_verified_identity(bank):
    bank.add_customer("user-a", kyc_status="PENDING")
    sender = bank.add_account("user-a", "100.00")
    receiver = bank.add_account("user-b")

    response = transfer(sender, receiver, "10.00")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "kyc_required"


def test_transfer_requires_idempotency_key(bank):
    response = client.post(
        "/transfers",
        json={"sender_account_id": str(uuid.uuid4()), "receiver_account_id": str(uuid.uuid4()), "amount": "1.00"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Idempotency-Key header is required"


def test_fraud_screening_blocks_before_any_write(bank):
    sender = bank.add_account("user-a", "50000.00")
    receiver = bank.add_account("user-b")

    response = transfer(sender, receiver, "15000.00")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "fraud_blocked"
    assert bank.balance(sender) == Decimal("50000.00")
    assert bank.items("OUTBOX#") == []


def test_high_value_transfer_requires_recent_sign_in(bank):
    sender = bank.add_account("user-a", "5000.00")
    receiver = bank.add_account("user-b")
    bank.as_user("user-a", auth_age_seconds=3600)
    idempotency_key = key()

    challenged = transfer(sender, receiver, "1500.00", idempotency_key)

    assert challenged.status_code == 401
    assert challenged.json()["detail"]["code"] == "step_up_required"
    assert "insufficient_user_authentication" in challenged.headers["WWW-Authenticate"]
    assert bank.balance(sender) == Decimal("5000.00")

    bank.as_user("user-a", auth_age_seconds=5)
    retried = transfer(sender, receiver, "1500.00", idempotency_key)
    assert retried.status_code == 201
    assert bank.balance(receiver) == Decimal("1500.00")


def test_sagemaker_score_is_used_when_endpoint_answers(bank, monkeypatch):
    sender = bank.add_account("user-a", "200.00")
    receiver = bank.add_account("user-b")
    monkeypatch.setattr(config, "settings", config.Settings(**{**config.settings.__dict__, "fraud_endpoint_name": "fraud"}))
    monkeypatch.setattr(fraud, "model_score", lambda features: 0.12)

    response = transfer(sender, receiver, "20.00")

    assert response.status_code == 201
    assert response.json()["fraud_model"] == "sagemaker:fraud"
    assert response.json()["fraud_score"] == 0.12


def test_withdrawal_completes_and_settles_through_clearing(bank):
    account_id = bank.add_account("user-a", "800.00")

    response = client.post(
        f"/accounts/{account_id}/withdrawals",
        headers={"Idempotency-Key": key()},
        json={"amount": "300.00", "bank_code": "UOB", "external_account": "987654321"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED"
    assert bank.balance(account_id) == Decimal("500.00")
    debit = next(item for item in bank.items(f"ACCOUNT#{account_id}") if item["SK"].startswith("TXN#"))
    assert debit["status"] == "COMPLETED"
    assert debit["external_reference"] == response.json()["external_reference"]
    assert bank.items("OUTBOX#")[0]["payload"]["event_type"] == "WITHDRAWAL_COMPLETED"


def test_declined_withdrawal_is_reversed_and_replayed(bank):
    account_id = bank.add_account("user-a", "30000.00")
    bank.as_user("user-a", auth_age_seconds=5)
    idempotency_key = key()
    payload = {"amount": "25000.00", "bank_code": "DBS", "external_account": "123456789"}

    response = client.post(
        f"/accounts/{account_id}/withdrawals", headers={"Idempotency-Key": idempotency_key}, json=payload
    )

    assert response.status_code == 422
    assert response.json()["status"] == "REVERSED"
    assert bank.balance(account_id) == Decimal("30000.00")
    entries = [item for item in bank.items(f"ACCOUNT#{account_id}") if item["SK"].startswith("TXN#")]
    assert sorted((entry["transaction_type"], entry["status"]) for entry in entries) == [
        ("REVERSAL", "COMPLETED"),
        ("WITHDRAWAL", "REVERSED"),
    ]
    assert bank.items("OUTBOX#")[0]["payload"]["event_type"] == "WITHDRAWAL_REVERSED"
    assert total_ledger_balance(bank) == Decimal("30000.00")

    replay = client.post(
        f"/accounts/{account_id}/withdrawals", headers={"Idempotency-Key": idempotency_key}, json=payload
    )
    assert replay.status_code == 422
    assert replay.headers["Idempotent-Replayed"] == "true"


def test_ledger_stays_zero_sum_across_a_customer_journey(bank):
    first = bank.add_account("user-a")
    second = bank.add_account("user-b")
    client.post(
        f"/accounts/{first}/deposits",
        headers={"Idempotency-Key": key()},
        json={"amount": "900.00", "bank_code": "DBS", "external_account": "123456789"},
    )
    transfer(first, second, "250.00")
    bank.as_user("user-b")
    client.post(
        f"/accounts/{second}/withdrawals",
        headers={"Idempotency-Key": key()},
        json={"amount": "100.00", "bank_code": "OCBC", "external_account": "123456789"},
    )

    assert bank.balance(first) == Decimal("650.00")
    assert bank.balance(second) == Decimal("150.00")
    assert total_ledger_balance(bank) == Decimal("0")


def test_transaction_history_is_owner_only_and_eventually_consistent(bank, monkeypatch):
    sender = bank.add_account("user-a", "100.00")
    receiver = bank.add_account("user-b")
    transfer(sender, receiver, "9.50")
    calls = []
    original_query = ledger.dynamodb_client().query
    monkeypatch.setattr(
        ledger.dynamodb_client(), "query", lambda **kwargs: calls.append(kwargs) or original_query(**kwargs)
    )

    response = client.get(f"/accounts/{sender}/transactions")

    assert response.status_code == 200
    entry = response.json()["transactions"][0]
    assert entry["amount"] == "9.50"
    assert entry["entry_type"] == "DEBIT"
    assert "fraud_score" not in entry
    assert calls[0]["ConsistentRead"] is False
    bank.as_user("user-b")
    assert client.get(f"/accounts/{sender}/transactions").status_code == 404


def test_kyc_upload_checks_content_and_stores_encrypted_document(bank):
    bank.add_customer("user-a", kyc_status="PENDING")
    bank.database.results = [{"user_id": "customer-a"}]

    rejected = client.post("/kyc/upload", files={"file": ("id.pdf", b"not a pdf", "application/pdf")})
    accepted = client.post("/kyc/upload", files={"file": ("id.pdf", b"%PDF-1.7 demo", "application/pdf")})

    assert rejected.status_code == 415
    assert accepted.status_code == 200
    document_key = accepted.json()["document_key"]
    head = boto3.client("s3", region_name=REGION).head_object(Bucket="test-kyc-statements", Key=document_key)
    assert head["ServerSideEncryption"] == "aws:kms"
    assert bank.database.statements[0][0].startswith("UPDATE customers SET kyc_status = 'VERIFIED_MOCK'")


def test_statement_request_is_queued_tracked_and_downloadable(bank, monkeypatch):
    account_id = bank.add_account("user-a")
    queue_url = boto3.client("sqs", region_name=REGION).create_queue(QueueName="statement-jobs")["QueueUrl"]
    monkeypatch.setattr(config, "settings", config.Settings(**{**config.settings.__dict__, "statement_queue_url": queue_url}))

    response = client.post("/statements/request", json={"account_id": account_id, "start_date": "2026-10-01"})

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    message = boto3.client("sqs", region_name=REGION).receive_message(QueueUrl=queue_url)["Messages"][0]
    assert json.loads(message["Body"])["owner_sub"] == "user-a"
    queued = client.get(f"/accounts/{account_id}/statements/{job_id}").json()
    assert queued["status"] == "QUEUED"
    assert "download_url" not in queued

    ledger.dynamodb_client().update_item(
        TableName="test-ledger",
        Key=ledger.to_av(ledger.statement_key(account_id, job_id)),
        UpdateExpression="SET #s = :s, object_key = :k, completed_at = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=ledger.to_av(
            {":s": "COMPLETED", ":k": f"statements/{account_id}/{job_id}.pdf", ":c": "2026-10-22T00:00:00+00:00"}
        ),
    )
    completed = client.get(f"/accounts/{account_id}/statements/{job_id}").json()
    assert completed["status"] == "COMPLETED"
    assert "X-Amz-Signature" in completed["download_url"]
    assert completed["download_url_expires_in"] == 900
    assert client.get(f"/accounts/{account_id}/statements").json()["statements"][0]["job_id"] == job_id

    bank.as_user("user-b")
    assert client.get(f"/accounts/{account_id}/statements/{job_id}").status_code == 404


def test_alert_subscription_filters_on_the_customers_identity(bank, monkeypatch):
    bank.add_customer("user-a")
    topic_arn = boto3.client("sns", region_name=REGION).create_topic(Name="customer-notifications")["TopicArn"]
    monkeypatch.setattr(
        config, "settings", config.Settings(**{**config.settings.__dict__, "notifications_topic_arn": topic_arn})
    )

    response = client.post("/me/alerts")

    assert response.status_code == 202
    subscription_arn = bank.database.statements[-1][1][0]
    attributes = boto3.client("sns", region_name=REGION).get_subscription_attributes(
        SubscriptionArn=subscription_arn
    )["Attributes"]
    assert json.loads(attributes["FilterPolicy"]) == {"customer_sub": ["user-a"]}
    assert attributes["Endpoint"] == "user-a@example.com"


def test_logout_revokes_the_access_token(bank):
    from app.backend_api import cache

    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert cache.get_cache().exists(cache.revoked_key(bank.user.token_id))


def test_closing_an_extra_account_sweeps_its_balance_and_keeps_the_ledger_balanced(bank):
    keep = bank.add_account("user-a", "100.00")
    extra = bank.add_account("user-a", "40.00")

    swept = ledger.close_account_into(extra, keep, "user-a")

    assert swept == Decimal("40.00")
    assert bank.balance(keep) == Decimal("140.00")
    assert bank.balance(extra) is None
    closure_legs = [item for item in bank.items("ACCOUNT#") if item.get("transaction_type") == "ACCOUNT_CLOSURE"]
    assert sorted(leg["entry_type"] for leg in closure_legs) == ["CREDIT", "DEBIT"]
