import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analytics import ledger_to_parquet
from app.lambda_ops_assistant import handler as ops
from app.lambda_slack_alerts import handler as slack


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inference", ROOT / "app/fraud_model/model_package/code/inference.py")
inference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inference)
MODEL = inference.model_fn(str(ROOT / "app/fraud_model/model_package"))


def score(**payload):
    request = inference.input_fn(json.dumps({"amount": 50, "hour_sgt": 12, **payload}).encode(), "application/json")
    return json.loads(inference.output_fn(inference.predict_fn(request, MODEL), "application/json"))["score"]


def test_fraud_model_ranks_risky_patterns_higher():
    everyday = score(amount=50, amount_to_balance=0.05)
    risky = score(amount=5000, amount_to_balance=0.9, hour_sgt=2, is_new_payee=1)
    extreme = score(amount=15000, amount_to_balance=1.0, hour_sgt=3, is_new_payee=1, transfers_last_hour=5)

    assert everyday < 0.05
    assert 0.6 <= risky < 0.9
    assert extreme >= 0.9


def test_fraud_model_rejects_other_content_types():
    with pytest.raises(ValueError):
        inference.input_fn(b"amount=5", "text/csv")


def test_ops_assistant_masks_pii_before_anything_leaves_the_lambda():
    text = (
        "user ari@example.com account 3f1c7d2e-0000-4000-8000-000000000001 phone +65 9123 4567 "
        "nric S1234567D card 4111111111111111 key AKIAABCDEFGHIJKLMNOP"
    )

    masked = ops.mask(text)

    for secret in ("ari@example.com", "3f1c7d2e", "9123", "S1234567D", "4111111111111111", "AKIAABCD"):
        assert secret not in masked
    assert "[EMAIL]" in masked and "[ID]" in masked and "[NRIC]" in masked


def test_ops_assistant_publishes_guarded_summary(monkeypatch):
    published = []
    guarded = []
    monkeypatch.setenv("OPS_TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:ops")
    monkeypatch.setattr(ops, "apply_guardrail", lambda text, source: guarded.append(source) or text)
    monkeypatch.setattr(ops, "summarise", lambda text: "What happened: balance p95 breached.")
    monkeypatch.setattr(ops.boto3, "client", lambda name: SimpleNamespace(publish=lambda **kw: published.append(kw)))

    ops.lambda_handler(
        {"Records": [{"Sns": {"Subject": "ALARM balance", "Message": json.dumps({"AlarmName": "p95"})}}]}, None
    )

    assert guarded == ["INPUT", "OUTPUT"]
    assert "balance p95 breached" in published[0]["Message"]
    assert "A human must approve" in published[0]["Message"]


def test_ops_assistant_falls_back_to_raw_alert_when_model_unavailable(monkeypatch):
    published = []
    monkeypatch.setenv("OPS_TOPIC_ARN", "arn:aws:sns:ap-southeast-1:123456789012:ops")
    monkeypatch.setattr(ops, "apply_guardrail", lambda text, source: text)

    def broken(text):
        raise RuntimeError("model not enabled")

    monkeypatch.setattr(ops, "summarise", broken)
    monkeypatch.setattr(ops.boto3, "client", lambda name: SimpleNamespace(publish=lambda **kw: published.append(kw)))

    ops.lambda_handler({"Records": [{"Sns": {"Subject": "DLQ", "Message": "events-dlq has 1 message"}}]}, None)

    assert "Automatic summary unavailable" in published[0]["Message"]


def test_slack_formats_alarm_and_guardduty_messages():
    alarm = slack.describe(
        json.dumps({"AlarmName": "balance-p95", "NewStateValue": "ALARM", "NewStateReason": "p95 412 ms"}), None
    )
    finding = slack.describe(
        json.dumps({"detail-type": "GuardDuty Finding", "detail": {"severity": 8, "title": "Root login"}}), None
    )

    assert "*balance-p95* is *ALARM*" in alarm
    assert "GuardDuty severity 8" in finding and "Root login" in finding
    assert slack.channel_kind("arn:aws:sns:ap-southeast-1:1:facaibank-dev-security-alerts") == "security"
    assert slack.describe("Budget at 80%", "AWS Budgets") == "*AWS Budgets*\nBudget at 80%"


def av(value):
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return value


def test_analytics_flattens_customer_ledger_entries_in_singapore_time():
    item = {key: av(value) for key, value in {
        "PK": "ACCOUNT#acct-1",
        "SK": "TXN#2026-10-21T16:30:00+00:00#t1",
        "record_type": "LEDGER_ENTRY",
        "transaction_id": "t1",
        "transaction_type": "TRANSFER",
        "entry_type": "DEBIT",
        "amount": 120.5,
        "fraud_score": 0.12,
        "created_at": "2026-10-21T16:30:00+00:00",
    }.items()}

    row = ledger_to_parquet.flatten(item)

    assert row["account_id"] == "acct-1"
    assert row["amount"] == 120.5 and row["fraud_score"] == 0.12
    assert (row["txn_date"], row["txn_hour_sgt"]) == ("2026-10-22", 0)
    assert ledger_to_parquet.flatten({"PK": {"S": "OUTBOX#t1"}, "record_type": {"S": "OUTBOX_EVENT"}}) is None
