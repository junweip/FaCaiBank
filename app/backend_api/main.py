import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import PurePosixPath
from threading import Lock

import boto3
import psycopg
from botocore.exceptions import ClientError
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from boto3.dynamodb.types import TypeSerializer


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@dataclass(frozen=True)
class Settings:
    aws_region: str = os.getenv("AWS_REGION", "ap-southeast-1")
    database_host: str = os.getenv("DATABASE_HOST", "")
    database_name: str = os.getenv("DATABASE_NAME", "facaibank")
    database_port: int = int(os.getenv("DATABASE_PORT", "5432"))
    database_secret_arn: str = os.getenv("DATABASE_SECRET_ARN", "")
    ledger_table_name: str = os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger")
    kyc_statements_bucket: str = os.getenv("KYC_STATEMENTS_BUCKET", "")
    statement_queue_url: str = os.getenv("STATEMENT_QUEUE_URL", "")
    cors_origins: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5500",
    )
    max_upload_bytes: int = 10 * 1024 * 1024


settings = Settings()
serializer = TypeSerializer()
_schema_lock = Lock()
_schema_initialized = False


class AccountCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("A valid email address is required")
        return normalized

    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Full name must contain at least two characters")
        return normalized


class TransferRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sender_account_id: str = Field(min_length=1, max_length=64)
    receiver_account_id: str = Field(min_length=1, max_length=64)
    amount: Decimal

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("Amount must be a positive number")
        try:
            cents = value.quantize(Decimal("0.01"))
        except InvalidOperation as error:
            raise ValueError("Amount must have at most two decimal places") from error
        if cents != value:
            raise ValueError("Amount must have at most two decimal places")
        return cents


class StatementRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, end_date: date | None, info):
        start_date = info.data.get("start_date")
        if end_date is not None and start_date is not None and end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        return end_date


app = FastAPI(
    title="FaCaiBank API",
    description="Demo account, KYC, transfer, and statement services.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)


def required_setting(value: str, name: str) -> str:
    if not value:
        raise HTTPException(status_code=503, detail=f"{name} is not configured")
    return value


@lru_cache(maxsize=1)
def secrets_manager():
    return boto3.client("secretsmanager", region_name=settings.aws_region)


@lru_cache(maxsize=1)
def dynamodb_client():
    return boto3.client("dynamodb", region_name=settings.aws_region)


@lru_cache(maxsize=1)
def dynamodb_table():
    return boto3.resource("dynamodb", region_name=settings.aws_region).Table(
        required_setting(settings.ledger_table_name, "LEDGER_TABLE_NAME")
    )


@lru_cache(maxsize=1)
def s3_client():
    return boto3.client("s3", region_name=settings.aws_region)


@lru_cache(maxsize=1)
def sqs_client():
    return boto3.client("sqs", region_name=settings.aws_region)


def database_credentials() -> dict[str, str]:
    secret_arn = required_setting(settings.database_secret_arn, "DATABASE_SECRET_ARN")
    response = secrets_manager().get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise HTTPException(status_code=503, detail="Database credentials secret is empty")
    try:
        secret = json.loads(secret_string)
    except json.JSONDecodeError as error:
        logger.exception("Database credentials secret is not valid JSON")
        raise HTTPException(status_code=503, detail="Database credentials secret is invalid") from error
    if not isinstance(secret, dict) or not secret.get("username") or not secret.get("password"):
        raise HTTPException(status_code=503, detail="Database credentials secret is missing fields")
    return {"username": secret["username"], "password": secret["password"]}


def parse_database_endpoint(endpoint: str, default_port: int) -> tuple[str, int]:
    host, separator, port_text = endpoint.rpartition(":")
    if separator and port_text.isdigit():
        return host, int(port_text)
    return endpoint, default_port


def connect_database():
    endpoint = required_setting(settings.database_host, "DATABASE_HOST")
    host, port = parse_database_endpoint(endpoint, settings.database_port)
    credentials = database_credentials()
    return psycopg.connect(
        host=host,
        port=port,
        dbname=settings.database_name,
        user=credentials["username"],
        password=credentials["password"],
        connect_timeout=5,
        sslmode=os.getenv("DATABASE_SSLMODE", "require"),
        row_factory=dict_row,
    )


def ensure_database_schema(connection) -> None:
    global _schema_initialized
    if _schema_initialized:
        return

    with _schema_lock:
        if _schema_initialized:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    user_id UUID PRIMARY KEY,
                    full_name VARCHAR(120) NOT NULL,
                    email VARCHAR(254) NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES customers(user_id),
                    kyc_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        connection.commit()
        _schema_initialized = True


def serialize_item(item: dict) -> dict:
    return {key: serializer.serialize(value) for key, value in item.items()}


def decimal_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def mock_fraud_score(amount: Decimal) -> Decimal:
    if amount >= Decimal("10000.00"):
        return Decimal("0.95")
    if amount >= Decimal("2500.00"):
        return Decimal("0.65")
    return Decimal("0.05")


def build_transfer_items(
    sender_account_id: str,
    receiver_account_id: str,
    amount: Decimal,
    idempotency_key: str,
    transfer_id: str,
    timestamp: str,
) -> list[dict]:
    table_name = required_setting(settings.ledger_table_name, "LEDGER_TABLE_NAME")
    amount = amount.quantize(Decimal("0.01"))
    transaction_key = f"TXN#{timestamp}#{transfer_id}"
    event_id = str(uuid.uuid4())
    event_payload = {
        "event_id": event_id,
        "event_type": "TRANSFER_COMPLETED",
        "transfer_id": transfer_id,
        "sender_account_id": sender_account_id,
        "receiver_account_id": receiver_account_id,
        "amount": decimal_string(amount),
        "currency": "USD",
        "created_at": timestamp,
    }
    amount_av = serializer.serialize(amount)
    timestamp_av = serializer.serialize(timestamp)
    idempotency_av = serializer.serialize(idempotency_key)

    sender_update = {
        "TableName": table_name,
        "Key": serialize_item({"PK": f"ACCOUNT#{sender_account_id}", "SK": "BALANCE"}),
        "UpdateExpression": (
            "SET #balance = #balance - :amount, "
            "last_idempotency_key = :idempotency_key, updated_at = :timestamp"
        ),
        "ConditionExpression": (
            "attribute_exists(PK) AND #balance >= :amount AND "
            "(attribute_not_exists(last_idempotency_key) "
            "OR last_idempotency_key <> :idempotency_key)"
        ),
        "ExpressionAttributeNames": {"#balance": "balance"},
        "ExpressionAttributeValues": {
            ":amount": amount_av,
            ":idempotency_key": idempotency_av,
            ":timestamp": timestamp_av,
        },
    }
    receiver_update = {
        "TableName": table_name,
        "Key": serialize_item({"PK": f"ACCOUNT#{receiver_account_id}", "SK": "BALANCE"}),
        "UpdateExpression": "SET #balance = #balance + :amount, updated_at = :timestamp",
        "ConditionExpression": "attribute_exists(PK)",
        "ExpressionAttributeNames": {"#balance": "balance"},
        "ExpressionAttributeValues": {
            ":amount": amount_av,
            ":timestamp": timestamp_av,
        },
    }

    debit = {
        "PK": f"ACCOUNT#{sender_account_id}",
        "SK": transaction_key,
        "record_type": "LEDGER_ENTRY",
        "transfer_id": transfer_id,
        "entry_type": "DEBIT",
        "amount": amount,
        "currency": "USD",
        "counterparty_account_id": receiver_account_id,
        "idempotency_key": idempotency_key,
        "created_at": timestamp,
    }
    credit = {
        "PK": f"ACCOUNT#{receiver_account_id}",
        "SK": transaction_key,
        "record_type": "LEDGER_ENTRY",
        "transfer_id": transfer_id,
        "entry_type": "CREDIT",
        "amount": amount,
        "currency": "USD",
        "counterparty_account_id": sender_account_id,
        "idempotency_key": idempotency_key,
        "created_at": timestamp,
    }
    outbox = {
        "PK": f"OUTBOX#{transfer_id}",
        "SK": "EVENT",
        "record_type": "OUTBOX_EVENT",
        **event_payload,
        "payload": event_payload,
    }
    idempotency_record = {
        "PK": f"IDEMPOTENCY#{idempotency_key}",
        "SK": "TRANSFER",
        "transfer_id": transfer_id,
        "created_at": timestamp,
    }

    def conditional_put(item: dict) -> dict:
        return {
            "TableName": table_name,
            "Item": serialize_item(item),
            "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
        }

    return [
        {"Update": sender_update},
        {"Update": receiver_update},
        {"Put": conditional_put(debit)},
        {"Put": conditional_put(credit)},
        {"Put": conditional_put(outbox)},
        {"Put": conditional_put(idempotency_record)},
    ]


def dynamodb_item_value(attribute: dict):
    if "N" in attribute:
        return Decimal(attribute["N"])
    if "S" in attribute:
        return attribute["S"]
    return None


def transfer_failure_response(
    error: ClientError,
    idempotency_key: str,
    sender_account_id: str,
    requested_amount: Decimal,
):
    error_code = error.response.get("Error", {}).get("Code")
    if error_code != "TransactionCanceledException":
        raise error

    table_name = required_setting(settings.ledger_table_name, "LEDGER_TABLE_NAME")
    client = dynamodb_client()
    existing_key = client.get_item(
        TableName=table_name,
        Key=serialize_item({"PK": f"IDEMPOTENCY#{idempotency_key}", "SK": "TRANSFER"}),
        ConsistentRead=True,
    ).get("Item")
    if existing_key:
        raise HTTPException(status_code=409, detail="Idempotency-Key has already been used") from error

    balance_item = client.get_item(
        TableName=table_name,
        Key=serialize_item({"PK": f"ACCOUNT#{sender_account_id}", "SK": "BALANCE"}),
        ConsistentRead=True,
    ).get("Item")
    if balance_item is None:
        raise HTTPException(status_code=404, detail="Sender account was not found") from error

    balance = dynamodb_item_value(balance_item.get("balance", {}))
    if balance is not None and balance < requested_amount:
        raise HTTPException(status_code=400, detail="Insufficient funds") from error

    reasons = error.response.get("CancellationReasons", [])
    if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
        raise HTTPException(status_code=400, detail="Insufficient funds") from error
    raise HTTPException(status_code=409, detail="Transfer could not be completed") from error


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/accounts", status_code=201)
def create_account(request: AccountCreate):
    user_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    table = dynamodb_table()
    ledger_item = {
        "PK": f"ACCOUNT#{account_id}",
        "SK": "BALANCE",
        "record_type": "ACCOUNT_BALANCE",
        "account_id": account_id,
        "balance": Decimal("1000.00"),
        "currency": "USD",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ledger_item_created = False

    try:
        with connect_database() as connection:
            ensure_database_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO customers (user_id, full_name, email) VALUES (%s, %s, %s)",
                    (user_id, request.full_name, request.email),
                )
                cursor.execute(
                    "INSERT INTO accounts (account_id, user_id, kyc_status) "
                    "VALUES (%s, %s, 'PENDING')",
                    (account_id, user_id),
                )
            table.put_item(
                Item=ledger_item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
            ledger_item_created = True
    except UniqueViolation as error:
        if ledger_item_created:
            try:
                table.delete_item(Key={"PK": ledger_item["PK"], "SK": ledger_item["SK"]})
            except ClientError:
                logger.exception("Could not remove initial balance after account creation failed")
        raise HTTPException(status_code=409, detail="A customer with this email already exists") from error
    except ClientError as error:
        if ledger_item_created:
            try:
                table.delete_item(Key={"PK": ledger_item["PK"], "SK": ledger_item["SK"]})
            except ClientError:
                logger.exception("Could not remove initial balance after account creation failed")
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(status_code=409, detail="Account identifier already exists") from error
        raise
    except Exception:
        if ledger_item_created:
            try:
                table.delete_item(Key={"PK": ledger_item["PK"], "SK": ledger_item["SK"]})
            except ClientError:
                logger.exception("Could not remove initial balance after account creation failed")
        raise

    return {
        "customer_id": user_id,
        "account_id": account_id,
        "balance": "1000.00",
        "currency": "USD",
        "kyc_status": "PENDING",
    }


@app.post("/kyc/upload")
async def upload_kyc_document(
    account_id: str,
    file: UploadFile = File(...),
):
    bucket = required_setting(settings.kyc_statements_bucket, "KYC_STATEMENTS_BUCKET")
    contents = await file.read(settings.max_upload_bytes + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="KYC document exceeds the 10 MiB limit")

    safe_name = PurePosixPath(file.filename.replace("\\", "/") if file.filename else "document").name
    object_key = f"kyc/{account_id}/{uuid.uuid4()}-{safe_name}"
    s3_client().put_object(
        Bucket=bucket,
        Key=object_key,
        Body=contents,
        ContentType=file.content_type or "application/octet-stream",
        ServerSideEncryption="aws:kms",
    )

    try:
        with connect_database() as connection:
            ensure_database_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts SET kyc_status = 'VERIFIED_MOCK' "
                    "WHERE account_id = %s RETURNING account_id",
                    (account_id,),
                )
                account = cursor.fetchone()
                if account is None:
                    raise HTTPException(status_code=404, detail="Account was not found")
    except Exception:
        try:
            s3_client().delete_object(Bucket=bucket, Key=object_key)
        except ClientError:
            logger.exception("Could not remove KYC object after database update failed")
        raise

    return {
        "account_id": account_id,
        "kyc_status": "VERIFIED_MOCK",
        "document_key": object_key,
    }


@app.post("/transfers", status_code=201)
def create_transfer(
    request: TransferRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    idempotency_key = idempotency_key.strip()
    if len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key must be 128 characters or fewer")
    if request.sender_account_id == request.receiver_account_id:
        raise HTTPException(status_code=400, detail="Sender and receiver accounts must differ")

    fraud_score = mock_fraud_score(request.amount)
    if fraud_score >= Decimal("0.90"):
        raise HTTPException(status_code=403, detail="Transfer rejected by mock fraud screening")

    transfer_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    client = dynamodb_client()
    try:
        client.transact_write_items(
            TransactItems=build_transfer_items(
                sender_account_id=request.sender_account_id,
                receiver_account_id=request.receiver_account_id,
                amount=request.amount,
                idempotency_key=idempotency_key,
                transfer_id=transfer_id,
                timestamp=timestamp,
            )
        )
    except ClientError as error:
        transfer_failure_response(
            error,
            idempotency_key,
            request.sender_account_id,
            request.amount,
        )

    return {
        "transfer_id": transfer_id,
        "status": "COMPLETED",
        "amount": decimal_string(request.amount),
        "currency": "USD",
        "fraud_score": float(fraud_score),
        "created_at": timestamp,
    }


@app.get("/accounts/{account_id}/balance")
def get_account_balance(account_id: str):
    result = dynamodb_table().get_item(
        Key={"PK": f"ACCOUNT#{account_id}", "SK": "BALANCE"},
        ConsistentRead=True,
    )
    item = result.get("Item")
    if item is None:
        raise HTTPException(status_code=404, detail="Account was not found")
    return {
        "account_id": account_id,
        "balance": decimal_string(Decimal(item["balance"])),
        "currency": item.get("currency", "USD"),
    }


@app.get("/accounts/{account_id}/transactions")
def get_account_transactions(account_id: str, limit: int = 50):
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    result = dynamodb_table().query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :txn_prefix)",
        ExpressionAttributeValues={
            ":pk": f"ACCOUNT#{account_id}",
            ":txn_prefix": "TXN#",
        },
        ConsistentRead=False,
        ScanIndexForward=False,
        Limit=limit,
    )
    transactions = result.get("Items", [])
    for transaction in transactions:
        if "amount" in transaction:
            transaction["amount"] = decimal_string(Decimal(transaction["amount"]))
    return {"account_id": account_id, "transactions": transactions}


@app.post("/statements/request", status_code=202)
def request_statement(request: StatementRequest):
    queue_url = required_setting(settings.statement_queue_url, "STATEMENT_QUEUE_URL")
    job_id = str(uuid.uuid4())
    message = {
        "job_id": job_id,
        "account_id": request.account_id,
        "start_date": request.start_date.isoformat() if request.start_date else None,
        "end_date": request.end_date.isoformat() if request.end_date else None,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    sqs_client().send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message, separators=(",", ":")),
    )
    return {"job_id": job_id, "status": "QUEUED"}
