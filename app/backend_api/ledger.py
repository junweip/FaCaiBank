"""DynamoDB ledger: balances, double-entry legs, outbox events and idempotency.

Every money movement is a single ``TransactWriteItems`` call, so balances,
ledger legs, the outbox event and the idempotency record either all commit or
none do. External deposits and withdrawals post their other leg to a sharded
clearing account, which keeps the ledger zero-sum.
"""

import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
from fastapi import HTTPException

from . import config


logger = logging.getLogger(__name__)
serializer = TypeSerializer()
deserializer = TypeDeserializer()
CENT = Decimal("0.01")
OUTBOX_TTL_SECONDS = 7 * 24 * 3600
MAX_CONFLICT_ATTEMPTS = 3


@lru_cache(maxsize=1)
def dynamodb_client():
    return boto3.client("dynamodb", region_name=config.settings.aws_region)


def table_name() -> str:
    name = config.settings.ledger_table_name
    if not name:
        raise HTTPException(status_code=503, detail="LEDGER_TABLE_NAME is not configured")
    return name


def to_av(item: dict) -> dict:
    return {key: serializer.serialize(value) for key, value in item.items()}


def from_av(item: dict) -> dict:
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def money(value: Decimal) -> str:
    return format(Decimal(value).quantize(CENT), "f")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def balance_key(account_id: str) -> dict:
    return {"PK": f"ACCOUNT#{account_id}", "SK": "BALANCE"}


def idempotency_key(subject: str, key: str, operation: str) -> dict:
    return {"PK": f"IDEMPOTENCY#{subject}#{key}", "SK": operation}


def request_fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps({"operation": operation, **payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clearing_account_id(seed: str) -> str:
    shard = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return f"CLEARING-{shard % max(1, config.settings.clearing_shards):02d}"


# ---------------------------------------------------------------- reads


def get_balance_item(account_id: str, consistent: bool = False) -> dict | None:
    response = dynamodb_client().get_item(
        TableName=table_name(),
        Key=to_av(balance_key(account_id)),
        ConsistentRead=consistent,
    )
    item = response.get("Item")
    return from_av(item) if item else None


def list_transactions(account_id: str, limit: int) -> list[dict]:
    response = dynamodb_client().query(
        TableName=table_name(),
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues=to_av({":pk": f"ACCOUNT#{account_id}", ":prefix": "TXN#"}),
        ConsistentRead=False,
        ScanIndexForward=False,
        Limit=limit,
    )
    return [from_av(item) for item in response.get("Items", [])]


def create_account_balance(
    account_id: str,
    owner_subject: str,
    customer_id: str,
    account_type: str,
    currency: str,
) -> dict:
    timestamp = now_iso()
    item = {
        **balance_key(account_id),
        "record_type": "ACCOUNT_BALANCE",
        "account_id": account_id,
        "customer_id": customer_id,
        "owner_sub": owner_subject,
        "account_type": account_type,
        "balance": Decimal("0.00"),
        "currency": currency,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    dynamodb_client().put_item(
        TableName=table_name(),
        Item=to_av(item),
        ConditionExpression="attribute_not_exists(PK)",
    )
    return item


def delete_account_balance(account_id: str) -> None:
    dynamodb_client().delete_item(
        TableName=table_name(),
        Key=to_av(balance_key(account_id)),
        ConditionExpression="#balance = :zero",
        ExpressionAttributeNames={"#balance": "balance"},
        ExpressionAttributeValues=to_av({":zero": Decimal("0")}),
    )


# ---------------------------------------------------------------- idempotency


@dataclass
class LedgerResult:
    status_code: int
    body: dict
    replayed: bool = False


def _replay(record: dict, fingerprint: str) -> LedgerResult:
    if record.get("request_fingerprint") != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different request",
        )
    if record.get("status") == "PROCESSING":
        raise HTTPException(
            status_code=409,
            detail="A request with this Idempotency-Key is still being processed",
        )
    return LedgerResult(
        status_code=int(record.get("response_status", 200)),
        body=json.loads(record.get("response_body", "{}")),
        replayed=True,
    )


def find_previous_result(subject: str, key: str, operation: str, fingerprint: str) -> LedgerResult | None:
    response = dynamodb_client().get_item(
        TableName=table_name(),
        Key=to_av(idempotency_key(subject, key, operation)),
        ConsistentRead=True,
    )
    item = response.get("Item")
    return _replay(from_av(item), fingerprint) if item else None


def _idempotency_item(
    subject: str,
    key: str,
    operation: str,
    fingerprint: str,
    transaction_id: str,
    status: str,
    timestamp: str,
    response_status: int | None = None,
    response_body: dict | None = None,
) -> dict:
    item = {
        **idempotency_key(subject, key, operation),
        "record_type": "IDEMPOTENCY",
        "request_fingerprint": fingerprint,
        "transaction_id": transaction_id,
        "status": status,
        "created_at": timestamp,
        "expires_at": int(time.time()) + config.settings.idempotency_ttl_days * 86400,
    }
    if response_status is not None:
        item["response_status"] = response_status
        item["response_body"] = json.dumps(response_body, separators=(",", ":"))
    return item


# ---------------------------------------------------------------- transaction items


def _balance_update(
    account_id: str,
    sign: str,
    amount: Decimal,
    timestamp: str,
    *,
    owner_subject: str | None = None,
    require_funds: bool = False,
    clearing: bool = False,
) -> dict:
    values: dict = {":amount": amount, ":ts": timestamp}
    if clearing:
        values.update({":zero": Decimal("0"), ":clearing": "CLEARING_ACCOUNT"})
        update = {
            "UpdateExpression": (
                f"SET #balance = if_not_exists(#balance, :zero) {sign} :amount, "
                "record_type = if_not_exists(record_type, :clearing), updated_at = :ts"
            ),
        }
    else:
        conditions = ["attribute_exists(PK)", "record_type = :balance_type"]
        values[":balance_type"] = "ACCOUNT_BALANCE"
        if owner_subject:
            conditions.append("owner_sub = :owner")
            values[":owner"] = owner_subject
        if require_funds:
            conditions.append("#balance >= :amount")
        update = {
            "UpdateExpression": f"SET #balance = #balance {sign} :amount, updated_at = :ts",
            "ConditionExpression": " AND ".join(conditions),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
    update.update(
        {
            "TableName": table_name(),
            "Key": to_av(balance_key(account_id)),
            "ExpressionAttributeNames": {"#balance": "balance"},
            "ExpressionAttributeValues": to_av(values),
        }
    )
    return {"Update": update}


def _put_new(item: dict) -> dict:
    return {
        "Put": {
            "TableName": table_name(),
            "Item": to_av(item),
            "ConditionExpression": "attribute_not_exists(PK)",
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
    }


def _update_status(key: dict, expected: str, assignments: dict) -> dict:
    names = {"#status": "status"}
    values = {":expected": expected}
    clauses = []
    for index, (attribute, value) in enumerate(assignments.items()):
        names[f"#a{index}"] = attribute
        values[f":v{index}"] = value
        clauses.append(f"#a{index} = :v{index}")
    return {
        "Update": {
            "TableName": table_name(),
            "Key": to_av(key),
            "UpdateExpression": "SET " + ", ".join(clauses),
            "ConditionExpression": "#status = :expected",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": to_av(values),
        }
    }


def ledger_entry(
    account_id: str,
    *,
    transaction_id: str,
    transaction_type: str,
    entry_type: str,
    amount: Decimal,
    currency: str,
    counterparty: str,
    idempotency_key_value: str,
    timestamp: str,
    status: str = "COMPLETED",
    extra: dict | None = None,
) -> dict:
    return {
        "PK": f"ACCOUNT#{account_id}",
        "SK": f"TXN#{timestamp}#{transaction_id}",
        "record_type": "LEDGER_ENTRY",
        "transaction_id": transaction_id,
        "transfer_id": transaction_id,
        "transaction_type": transaction_type,
        "entry_type": entry_type,
        "amount": amount,
        "currency": currency,
        "counterparty_account_id": counterparty,
        "idempotency_key": idempotency_key_value,
        "status": status,
        "created_at": timestamp,
        **(extra or {}),
    }


def outbox_event(
    transaction_id: str,
    event_type: str,
    timestamp: str,
    recipients: list[dict],
    details: dict,
) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "transaction_id": transaction_id,
        "transfer_id": transaction_id,
        "created_at": timestamp,
        "recipients": recipients,
        **details,
    }
    return {
        "PK": f"OUTBOX#{transaction_id}",
        "SK": "EVENT",
        "record_type": "OUTBOX_EVENT",
        "event_id": payload["event_id"],
        "event_type": event_type,
        "payload": payload,
        "expires_at": int(time.time()) + OUTBOX_TTL_SECONDS,
    }


# ---------------------------------------------------------------- execution


def cancellation_reasons(error: ClientError) -> list[dict]:
    return error.response.get("CancellationReasons") or []


def is_transaction_conflict(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code")
    if code == "TransactionConflictException":
        return True
    return code == "TransactionCanceledException" and any(
        reason.get("Code") == "TransactionConflict" for reason in cancellation_reasons(error)
    )


def transact(items: list[dict]) -> None:
    for attempt in range(MAX_CONFLICT_ATTEMPTS):
        try:
            dynamodb_client().transact_write_items(TransactItems=items)
            return
        except ClientError as error:
            if not is_transaction_conflict(error) or attempt + 1 == MAX_CONFLICT_ATTEMPTS:
                raise
            time.sleep(random.uniform(0.02, 0.06) * (2**attempt))


def interpret_cancellation(
    error: ClientError,
    roles: list[str],
    *,
    owner_subject: str,
    amount: Decimal,
    fingerprint: str,
) -> LedgerResult:
    """Translate a cancelled transaction into a replay or a customer-facing error."""
    if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        raise error
    failed = [
        (role, from_av(reason["Item"]) if reason.get("Item") else None)
        for role, reason in zip(roles, cancellation_reasons(error))
        if reason.get("Code") == "ConditionalCheckFailed"
    ]
    for role, old in failed:
        if role == "idempotency" and old:
            return _replay(old, fingerprint)
    for role, old in failed:
        if role in {"source", "target"}:
            if old is None or old.get("owner_sub") != owner_subject:
                raise HTTPException(status_code=404, detail="Account was not found") from error
            if role == "source" and Decimal(str(old.get("balance", "0"))) < amount:
                raise HTTPException(status_code=400, detail="Insufficient funds") from error
        if role == "destination":
            raise HTTPException(status_code=404, detail="Recipient account was not found") from error
    if is_transaction_conflict(error):
        raise HTTPException(status_code=409, detail="The account is busy. Please try again.") from error
    logger.error("Unexpected ledger cancellation: %s", cancellation_reasons(error))
    raise HTTPException(status_code=409, detail="The ledger rejected this request") from error


def execute_transfer(
    *,
    subject: str,
    key: str,
    fingerprint: str,
    sender_account_id: str,
    receiver_account_id: str,
    receiver_subject: str,
    amount: Decimal,
    currency: str,
    fraud_score: float,
    fraud_model: str,
) -> LedgerResult:
    transaction_id = str(uuid.uuid4())
    timestamp = now_iso()
    body = {
        "transaction_id": transaction_id,
        "transfer_id": transaction_id,
        "status": "COMPLETED",
        "amount": money(amount),
        "currency": currency,
        "sender_account_id": sender_account_id,
        "receiver_account_id": receiver_account_id,
        "fraud_score": round(fraud_score, 4),
        "fraud_model": fraud_model,
        "created_at": timestamp,
    }
    common = {
        "transaction_id": transaction_id,
        "transaction_type": "TRANSFER",
        "amount": amount,
        "currency": currency,
        "idempotency_key_value": key,
        "timestamp": timestamp,
    }
    items = [
        _balance_update(
            sender_account_id, "-", amount, timestamp, owner_subject=subject, require_funds=True
        ),
        _balance_update(receiver_account_id, "+", amount, timestamp),
        _put_new(
            ledger_entry(
                sender_account_id,
                entry_type="DEBIT",
                counterparty=receiver_account_id,
                extra={"fraud_score": Decimal(str(round(fraud_score, 4))), "fraud_model": fraud_model},
                **common,
            )
        ),
        _put_new(
            ledger_entry(receiver_account_id, entry_type="CREDIT", counterparty=sender_account_id, **common)
        ),
        _put_new(
            outbox_event(
                transaction_id,
                "TRANSFER_COMPLETED",
                timestamp,
                recipients=[
                    {"role": "SENDER", "customer_sub": subject, "account_id": sender_account_id},
                    {"role": "RECEIVER", "customer_sub": receiver_subject, "account_id": receiver_account_id},
                ],
                details={
                    "amount": money(amount),
                    "currency": currency,
                    "sender_account_id": sender_account_id,
                    "receiver_account_id": receiver_account_id,
                },
            )
        ),
        _put_new(
            _idempotency_item(
                subject, key, "TRANSFER", fingerprint, transaction_id, "COMPLETED", timestamp, 201, body
            )
        ),
    ]
    try:
        transact(items)
    except ClientError as error:
        return interpret_cancellation(
            error,
            ["source", "destination", "entry", "entry", "outbox", "idempotency"],
            owner_subject=subject,
            amount=amount,
            fingerprint=fingerprint,
        )
    return LedgerResult(201, body)


def execute_deposit(
    *,
    subject: str,
    key: str,
    fingerprint: str,
    account_id: str,
    amount: Decimal,
    currency: str,
    bank_code: str,
    external_reference: str,
) -> LedgerResult:
    transaction_id = str(uuid.uuid4())
    timestamp = now_iso()
    clearing = clearing_account_id(f"{subject}:{key}")
    body = {
        "transaction_id": transaction_id,
        "status": "COMPLETED",
        "amount": money(amount),
        "currency": currency,
        "account_id": account_id,
        "bank_code": bank_code,
        "external_reference": external_reference,
        "created_at": timestamp,
    }
    common = {
        "transaction_id": transaction_id,
        "transaction_type": "DEPOSIT",
        "amount": amount,
        "currency": currency,
        "idempotency_key_value": key,
        "timestamp": timestamp,
    }
    bank_details = {"bank_code": bank_code, "external_reference": external_reference}
    items = [
        _balance_update(account_id, "+", amount, timestamp, owner_subject=subject),
        _balance_update(clearing, "-", amount, timestamp, clearing=True),
        _put_new(
            ledger_entry(
                account_id, entry_type="CREDIT", counterparty=f"EXTERNAL:{bank_code}", extra=bank_details, **common
            )
        ),
        _put_new(ledger_entry(clearing, entry_type="DEBIT", counterparty=account_id, extra=bank_details, **common)),
        _put_new(
            outbox_event(
                transaction_id,
                "DEPOSIT_COMPLETED",
                timestamp,
                recipients=[{"role": "ACCOUNT_HOLDER", "customer_sub": subject, "account_id": account_id}],
                details={"amount": money(amount), "currency": currency, "account_id": account_id, **bank_details},
            )
        ),
        _put_new(
            _idempotency_item(
                subject, key, "DEPOSIT", fingerprint, transaction_id, "COMPLETED", timestamp, 201, body
            )
        ),
    ]
    try:
        transact(items)
    except ClientError as error:
        return interpret_cancellation(
            error,
            ["target", "clearing", "entry", "entry", "outbox", "idempotency"],
            owner_subject=subject,
            amount=amount,
            fingerprint=fingerprint,
        )
    return LedgerResult(201, body)


@dataclass
class PendingWithdrawal:
    subject: str
    key: str
    fingerprint: str
    account_id: str
    amount: Decimal
    currency: str
    bank_code: str
    transaction_id: str
    timestamp: str
    clearing_account: str
    entry_sort_key: str = field(init=False)

    def __post_init__(self):
        self.entry_sort_key = f"TXN#{self.timestamp}#{self.transaction_id}"


def begin_withdrawal(
    *,
    subject: str,
    key: str,
    fingerprint: str,
    account_id: str,
    amount: Decimal,
    currency: str,
    bank_code: str,
) -> PendingWithdrawal | LedgerResult:
    """Phase 1: move funds into clearing before the partner bank is called."""
    pending = PendingWithdrawal(
        subject=subject,
        key=key,
        fingerprint=fingerprint,
        account_id=account_id,
        amount=amount,
        currency=currency,
        bank_code=bank_code,
        transaction_id=str(uuid.uuid4()),
        timestamp=now_iso(),
        clearing_account=clearing_account_id(f"{subject}:{key}"),
    )
    common = {
        "transaction_id": pending.transaction_id,
        "transaction_type": "WITHDRAWAL",
        "amount": amount,
        "currency": currency,
        "idempotency_key_value": key,
        "timestamp": pending.timestamp,
        "extra": {"bank_code": bank_code},
    }
    items = [
        _balance_update(
            account_id, "-", amount, pending.timestamp, owner_subject=subject, require_funds=True
        ),
        _balance_update(pending.clearing_account, "+", amount, pending.timestamp, clearing=True),
        _put_new(
            ledger_entry(
                account_id,
                entry_type="DEBIT",
                counterparty=f"EXTERNAL:{bank_code}",
                status="PENDING_EXTERNAL",
                **common,
            )
        ),
        _put_new(ledger_entry(pending.clearing_account, entry_type="CREDIT", counterparty=account_id, **common)),
        _put_new(
            _idempotency_item(
                subject, key, "WITHDRAWAL", fingerprint, pending.transaction_id, "PROCESSING", pending.timestamp
            )
        ),
    ]
    try:
        transact(items)
    except ClientError as error:
        return interpret_cancellation(
            error,
            ["source", "clearing", "entry", "entry", "idempotency"],
            owner_subject=subject,
            amount=amount,
            fingerprint=fingerprint,
        )
    return pending


def complete_withdrawal(pending: PendingWithdrawal, external_reference: str) -> LedgerResult:
    """Phase 2a: the partner bank accepted the payout."""
    timestamp = now_iso()
    body = {
        "transaction_id": pending.transaction_id,
        "status": "COMPLETED",
        "amount": money(pending.amount),
        "currency": pending.currency,
        "account_id": pending.account_id,
        "bank_code": pending.bank_code,
        "external_reference": external_reference,
        "created_at": pending.timestamp,
    }
    items = [
        _update_status(
            {"PK": f"ACCOUNT#{pending.account_id}", "SK": pending.entry_sort_key},
            "PENDING_EXTERNAL",
            {"status": "COMPLETED", "external_reference": external_reference, "settled_at": timestamp},
        ),
        _put_new(
            outbox_event(
                pending.transaction_id,
                "WITHDRAWAL_COMPLETED",
                timestamp,
                recipients=[
                    {"role": "ACCOUNT_HOLDER", "customer_sub": pending.subject, "account_id": pending.account_id}
                ],
                details={
                    "amount": money(pending.amount),
                    "currency": pending.currency,
                    "account_id": pending.account_id,
                    "bank_code": pending.bank_code,
                    "external_reference": external_reference,
                },
            )
        ),
        _update_status(
            idempotency_key(pending.subject, pending.key, "WITHDRAWAL"),
            "PROCESSING",
            {
                "status": "COMPLETED",
                "response_status": 201,
                "response_body": json.dumps(body, separators=(",", ":")),
            },
        ),
    ]
    transact(items)
    return LedgerResult(201, body)


def reverse_withdrawal(pending: PendingWithdrawal, reason: str) -> LedgerResult:
    """Phase 2b: the partner bank declined; return the funds atomically."""
    reversal_id = str(uuid.uuid4())
    timestamp = now_iso()
    body = {
        "detail": f"Withdrawal declined by the partner bank: {reason}. Your funds were returned.",
        "transaction_id": pending.transaction_id,
        "reversal_transaction_id": reversal_id,
        "status": "REVERSED",
    }
    common = {
        "transaction_id": reversal_id,
        "transaction_type": "REVERSAL",
        "amount": pending.amount,
        "currency": pending.currency,
        "idempotency_key_value": pending.key,
        "timestamp": timestamp,
        "extra": {"bank_code": pending.bank_code, "reverses_transaction_id": pending.transaction_id},
    }
    items = [
        _balance_update(pending.account_id, "+", pending.amount, timestamp),
        _balance_update(pending.clearing_account, "-", pending.amount, timestamp, clearing=True),
        _put_new(
            ledger_entry(
                pending.account_id,
                entry_type="CREDIT",
                counterparty=f"EXTERNAL:{pending.bank_code}",
                **common,
            )
        ),
        _put_new(
            ledger_entry(pending.clearing_account, entry_type="DEBIT", counterparty=pending.account_id, **common)
        ),
        _update_status(
            {"PK": f"ACCOUNT#{pending.account_id}", "SK": pending.entry_sort_key},
            "PENDING_EXTERNAL",
            {"status": "REVERSED", "decline_reason": reason},
        ),
        _put_new(
            outbox_event(
                reversal_id,
                "WITHDRAWAL_REVERSED",
                timestamp,
                recipients=[
                    {"role": "ACCOUNT_HOLDER", "customer_sub": pending.subject, "account_id": pending.account_id}
                ],
                details={
                    "amount": money(pending.amount),
                    "currency": pending.currency,
                    "account_id": pending.account_id,
                    "bank_code": pending.bank_code,
                    "reason": reason,
                },
            )
        ),
        _update_status(
            idempotency_key(pending.subject, pending.key, "WITHDRAWAL"),
            "PROCESSING",
            {
                "status": "COMPLETED",
                "response_status": 422,
                "response_body": json.dumps(body, separators=(",", ":")),
            },
        ),
    ]
    transact(items)
    return LedgerResult(422, body)


# ---------------------------------------------------------------- statements


def statement_key(account_id: str, job_id: str) -> dict:
    return {"PK": f"ACCOUNT#{account_id}", "SK": f"STATEMENT#{job_id}"}


def put_statement_job(job: dict) -> None:
    dynamodb_client().put_item(
        TableName=table_name(),
        Item=to_av(
            {
                **statement_key(job["account_id"], job["job_id"]),
                "record_type": "STATEMENT_JOB",
                "status": "QUEUED",
                **{key: value for key, value in job.items() if value is not None},
            }
        ),
        ConditionExpression="attribute_not_exists(PK)",
    )


def get_statement_job(account_id: str, job_id: str) -> dict | None:
    response = dynamodb_client().get_item(
        TableName=table_name(),
        Key=to_av(statement_key(account_id, job_id)),
        ConsistentRead=True,
    )
    item = response.get("Item")
    return from_av(item) if item else None


def list_statement_jobs(account_id: str, limit: int = 20) -> list[dict]:
    response = dynamodb_client().query(
        TableName=table_name(),
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues=to_av({":pk": f"ACCOUNT#{account_id}", ":prefix": "STATEMENT#"}),
        Limit=limit,
    )
    jobs = [from_av(item) for item in response.get("Items", [])]
    return sorted(jobs, key=lambda job: job.get("requested_at", ""), reverse=True)
