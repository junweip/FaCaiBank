from decimal import Decimal

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
        key = partition_key.removeprefix("IDEMPOTENCY#")
        item = self.idempotency_records.get(key)
        return {"Item": item} if item else {}

    def query(self, **kwargs):
        return {"Items": self.queries.pop(0)}


def test_idempotency_check_proves_single_debit(monkeypatch):
    idempotency_key = "validation-test-replay"
    transfer_id = "transfer-original"
    table = FakeTable(
        balances=[Decimal("100.00"), Decimal("99.00")],
        idempotency_records={
            "validation-test-run-replay": {
                "transfer_id": transfer_id,
            }
        },
        queries=[
            [
                {
                    "transfer_id": transfer_id,
                    "entry_type": "DEBIT",
                    "amount": Decimal("1.00"),
                }
            ]
        ],
    )
    responses = iter(
        [
            (201, {"transfer_id": transfer_id}),
            (409, {"detail": "Idempotency-Key has already been used"}),
        ]
    )
    monkeypatch.setattr(validate_all, "api_request", lambda *args, **kwargs: next(responses))

    result = validate_all.verify_idempotent_replay(
        "http://bank.example",
        table,
        "sender",
        "receiver",
        "test-run",
    )

    assert result["status"] == "PASS"
    assert result["deducted_once"] is True
    assert result["replay_status"] == 409


def test_concurrent_check_reconciles_transfer_legs_and_balances(monkeypatch):
    transfer_ids = [f"transfer-{index}" for index in range(validate_all.TRANSFER_COUNT)]
    sender_entries = [
        {
            "transfer_id": transfer_id,
            "entry_type": "DEBIT",
            "amount": validate_all.TRANSFER_AMOUNT,
        }
        for transfer_id in transfer_ids
    ]
    receiver_entries = [
        {
            "transfer_id": transfer_id,
            "entry_type": "CREDIT",
            "amount": validate_all.TRANSFER_AMOUNT,
        }
        for transfer_id in transfer_ids
    ]
    table = FakeTable(
        balances=[
            Decimal("100.00"),
            Decimal("50.00"),
            Decimal("95.00"),
            Decimal("55.00"),
        ],
        queries=[sender_entries, receiver_entries],
    )
    transfer_index = iter(transfer_ids)
    monkeypatch.setattr(
        validate_all,
        "send_transfer",
        lambda *args, **kwargs: {"transfer_id": next(transfer_index)},
    )

    result = validate_all.verify_concurrent_transfers(
        "http://bank.example",
        table,
        "sender",
        "receiver",
        "test-run",
    )

    assert result["status"] == "PASS"
    assert result["concurrent_requests"] == 5
    assert result["debit_total"] == "5.00"
    assert result["credit_total"] == "5.00"
    assert result["combined_balance_before"] == result["combined_balance_after"]
