import base64
import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import failover_test
import live_support
import load_test
import restore_test
import validate_all


class FakeTable:
    def __init__(self, balances, idempotency_records=None, queries=None):
        self.balances = list(balances)
        self.idempotency_records = idempotency_records or {}
        self.queries = list(queries or [])

    def get_item(self, **kwargs):
        partition_key = kwargs["Key"]["PK"]
        if partition_key.startswith("ACCOUNT#"):
            return {"Item": {"balance": self.balances.pop(0)}}
        item = self.idempotency_records.get(partition_key)
        return {"Item": item} if item else {}

    def query(self, **kwargs):
        return {"Items": self.queries.pop(0)}


class FakeApi:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, path, body=None, headers=None, **kwargs):
        self.calls.append((method, path, body, headers))
        return next(self.responses)


def test_totp_matches_rfc_6238_reference_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()

    assert live_support.totp(secret, for_time=59) == "287082"
    assert live_support.totp(secret, for_time=1111111109) == "081804"


def test_token_subject_reads_the_jwt_payload():
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "abc-123"}).encode()).decode().rstrip("=")

    assert validate_all.token_subject(f"header.{payload}.signature") == "abc-123"


def test_idempotency_check_requires_true_replay(monkeypatch):
    transfer_id = "transfer-original"
    table = FakeTable(
        balances=[Decimal("100.00"), Decimal("99.00")],
        idempotency_records={"IDEMPOTENCY#sub-a#validation-test-run-replay": {"transaction_id": transfer_id}},
        queries=[[{"transaction_id": transfer_id, "entry_type": "DEBIT", "amount": Decimal("1.00")}]],
    )
    api = FakeApi(
        [
            (201, {"transaction_id": transfer_id}, {}),
            (201, {"transaction_id": transfer_id}, {"Idempotent-Replayed": "true"}),
            (409, {"detail": "Idempotency-Key was already used for a different request"}, {}),
        ]
    )

    result = validate_all.verify_idempotent_replay(api, table, "sub-a", "sender", "receiver", "test-run")

    assert result["status"] == "PASS"
    assert result["deducted_once"] is True
    assert result["replay_header_idempotent_replayed"] == "true"
    assert result["different_body_same_key_status"] == 409


def test_idempotency_check_fails_when_replay_is_not_marked(monkeypatch):
    table = FakeTable(balances=[Decimal("100.00")])
    api = FakeApi([(201, {"transaction_id": "t"}, {}), (201, {"transaction_id": "t"}, {})])

    with pytest.raises(AssertionError, match="original result"):
        validate_all.verify_idempotent_replay(api, table, "sub-a", "sender", "receiver", "run")


def test_concurrent_check_reconciles_transfer_legs_and_balances(monkeypatch):
    transfer_ids = [f"transfer-{index}" for index in range(validate_all.TRANSFER_COUNT)]
    leg = lambda kind: [  # noqa: E731
        {"transaction_id": transfer_id, "entry_type": kind, "amount": validate_all.TRANSFER_AMOUNT}
        for transfer_id in transfer_ids
    ]
    table = FakeTable(
        balances=[Decimal("100.00"), Decimal("50.00"), Decimal("95.00"), Decimal("55.00")],
        queries=[leg("DEBIT"), leg("CREDIT")],
    )
    transfer_index = iter(transfer_ids)
    monkeypatch.setattr(validate_all, "send_transfer", lambda *args, **kwargs: {"transaction_id": next(transfer_index)})

    result = validate_all.verify_concurrent_transfers(object(), table, "sender", "receiver", "test-run")

    assert result["status"] == "PASS"
    assert result["debit_total"] == "5.00"
    assert result["combined_balance_before"] == result["combined_balance_after"]


def test_load_test_summary_flags_targets():
    samples = {
        "balance": [(float(ms), 200) for ms in range(1, 101)],
        "transfer": [(1500.0, 201), (400.0, 201), (300.0, 500)],
    }

    summary = load_test.summarise(samples, duration=10)

    assert summary["total_requests"] == 103
    assert summary["endpoints"]["balance"]["p95_ms"] == 95.0
    assert summary["endpoints"]["balance"]["meets_target"] is True
    assert summary["endpoints"]["transfer"]["meets_target"] is False
    assert summary["error_rate_percent"] == round(100 / 103, 3)
    report = load_test.markdown_report(
        {"run_id": "r1", "config": {"rps": 10, "duration_seconds": 10, "users": 2}, "results": summary, "scaling": {"asg": {"start": 1, "peak": 3, "end": 2}}}
    )
    assert "| balance | 100 |" in report and "| asg | 1 | 3 | 2 |" in report


def test_prober_measures_the_longest_outage():
    probe = failover_test.Prober("x", lambda: 200)
    probe.results = [(0, True), (1, False), (2, False), (5, True), (6, False), (7, True)]

    report = probe.report()

    assert report["longest_outage_seconds"] == 4
    assert report["failed_probes"] == 3
    assert report["within_rto"] is True


def test_restore_fingerprint_counts_items_and_zero_sum_balances():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-1")
        table = dynamodb.create_table(
            TableName="ledger",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        table.put_item(Item={"PK": "ACCOUNT#a", "SK": "BALANCE", "balance": Decimal("500")})
        table.put_item(Item={"PK": "ACCOUNT#CLEARING-01", "SK": "BALANCE", "balance": Decimal("-500")})
        table.put_item(Item={"PK": "ACCOUNT#a", "SK": "TXN#1", "amount": Decimal("500")})

        assert restore_test.ledger_fingerprint(table) == {"items": 3, "balance_items": 2, "balance_sum": "0"}
