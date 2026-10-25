import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Annotated, Literal

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from . import auth, cache, config, db, external_bank, fraud, ledger, metrics
from .account_limits import ACCOUNT_LIMITS
from .auth import AuthenticatedUser, current_user


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

PROFILE_TTL_SECONDS = 60
OWNER_TTL_SECONDS = 3600
MAX_AMOUNT = Decimal("200000.00")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.:\-]{8,128}")
KYC_SIGNATURES = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}


def validate_uuid(value: str, label: str = "Account") -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise HTTPException(status_code=404, detail=f"{label} was not found") from error


def validate_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("Amount must be a positive number")
    try:
        cents = value.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Amount must have at most two decimal places") from error
    if cents != value:
        raise ValueError("Amount must have at most two decimal places")
    if cents > MAX_AMOUNT:
        raise ValueError(f"Amount must not exceed {MAX_AMOUNT}")
    return cents


def validate_account_field(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError("Account ID must be a valid account number") from error


AccountId = Annotated[str, AfterValidator(validate_account_field)]
Amount = Annotated[Decimal, AfterValidator(validate_amount)]


class ProfileCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)

    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Full name must contain at least two characters")
        return normalized


class AccountCreate(BaseModel):
    account_type: Literal["EVERYDAY", "SAVINGS"] = "EVERYDAY"
    nickname: str | None = Field(default=None, max_length=60)

    @field_validator("nickname")
    @classmethod
    def normalize_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None


class TransferRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sender_account_id: AccountId
    receiver_account_id: AccountId
    amount: Amount


class ExternalTransferRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    amount: Amount
    bank_code: str = Field(min_length=2, max_length=8)
    external_account: str = Field(min_length=9, max_length=12)

    @field_validator("bank_code")
    @classmethod
    def normalize_bank(cls, value: str) -> str:
        return value.upper()


class StatementRequest(BaseModel):
    account_id: AccountId
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, end_date: date | None, info):
        start_date = info.data.get("start_date")
        if end_date is not None and start_date is not None and end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        return end_date


@asynccontextmanager
async def lifespan(_app: FastAPI):
    metrics.recorder.start()
    yield
    metrics.recorder.flush()


app = FastAPI(
    title="FaCaiBank API",
    description="Accounts, KYC, transfers, deposits, withdrawals and statements.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in config.settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    expose_headers=["Idempotent-Replayed", "X-Request-Id"],
)


@app.middleware("http")
async def observe_request(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        route = request.scope.get("route")
        endpoint = getattr(route, "name", None) or "unmatched"
        metrics.recorder.record(endpoint, (time.perf_counter() - started) * 1000, status_code)


def required_setting(value: str, name: str) -> str:
    if not value:
        raise HTTPException(status_code=503, detail=f"{name} is not configured")
    return value


@lru_cache(maxsize=1)
def s3_client():
    return boto3.client(
        "s3",
        region_name=config.settings.aws_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


@lru_cache(maxsize=1)
def sqs_client():
    return boto3.client("sqs", region_name=config.settings.aws_region)


@lru_cache(maxsize=1)
def sns_client():
    return boto3.client("sns", region_name=config.settings.aws_region)


@lru_cache(maxsize=1)
def cognito_client():
    return boto3.client(
        "cognito-idp",
        region_name=config.settings.aws_region,
        config=Config(signature_version=UNSIGNED),
    )


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def load_profile(subject: str, use_cache: bool = True) -> dict | None:
    store = cache.get_cache()
    if use_cache:
        cached = store.get(cache.profile_key(subject))
        if cached:
            return cached
    db.ensure_schema()
    with db.primary.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT user_id, full_name, email, kyc_status, kyc_verified_at, alert_subscription_arn, created_at "
            "FROM customers WHERE cognito_sub = %s",
            (subject,),
        )
        customer = cursor.fetchone()
        if customer is None:
            return None
        cursor.execute(
            "SELECT account_id, account_type, nickname, currency, created_at "
            "FROM accounts WHERE user_id = %s ORDER BY created_at",
            (customer["user_id"],),
        )
        accounts = cursor.fetchall()
    profile = {
        "customer_id": str(customer["user_id"]),
        "full_name": customer["full_name"],
        "email": customer["email"],
        "kyc_status": customer["kyc_status"],
        "kyc_verified_at": _iso(customer["kyc_verified_at"]),
        "alert_subscription_arn": customer["alert_subscription_arn"],
        "created_at": _iso(customer["created_at"]),
        "accounts": [
            {
                "account_id": str(account["account_id"]),
                "account_type": account["account_type"],
                "nickname": account["nickname"],
                "currency": account["currency"],
                "created_at": _iso(account["created_at"]),
            }
            for account in accounts
        ],
    }
    store.set(cache.profile_key(subject), profile, PROFILE_TTL_SECONDS)
    return profile


def public_profile(profile: dict) -> dict:
    return {key: value for key, value in profile.items() if key != "alert_subscription_arn"}


def require_profile(user: AuthenticatedUser) -> dict:
    profile = load_profile(user.subject)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_required", "message": "Complete your profile to continue."},
        )
    return profile


def require_verified_identity(profile: dict) -> None:
    if profile["kyc_status"] != "VERIFIED_MOCK":
        raise HTTPException(
            status_code=403,
            detail={"code": "kyc_required", "message": "Verify your identity before moving money."},
        )


def invalidate_profile(subject: str) -> None:
    cache.get_cache().delete(cache.profile_key(subject))


def require_owned_account(account_id: str, user: AuthenticatedUser, consistent: bool = False) -> dict | None:
    """Confirm the caller owns the account. Returns the balance item when it was read."""
    account_id = validate_uuid(account_id)
    store = cache.get_cache()
    if not consistent and store.get(cache.owner_key(account_id)) == user.subject:
        return None
    item = ledger.get_balance_item(account_id, consistent=consistent)
    if not item or item.get("record_type") != "ACCOUNT_BALANCE" or item.get("owner_sub") != user.subject:
        raise HTTPException(status_code=404, detail="Account was not found")
    store.set(cache.owner_key(account_id), user.subject, OWNER_TTL_SECONDS)
    return item


def parse_idempotency_key(value: str | None) -> str:
    if not value or not value.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    value = value.strip()
    if not IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must be 8-128 letters, digits, '.', '_', ':' or '-'",
        )
    return value


def ledger_response(result: ledger.LedgerResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


def verified_email(user: AuthenticatedUser) -> str:
    try:
        response = cognito_client().get_user(AccessToken=user.access_token)
    except ClientError as error:
        raise HTTPException(status_code=401, detail="Sign in again to continue") from error
    attributes = {item["Name"]: item["Value"] for item in response.get("UserAttributes", [])}
    if not attributes.get("email") or attributes.get("email_verified") != "true":
        raise HTTPException(status_code=403, detail="Verify your email address first")
    return attributes["email"].strip().lower()


def serialize_entry(item: dict) -> dict:
    return {
        "transaction_id": item.get("transaction_id") or item.get("transfer_id"),
        "transfer_id": item.get("transfer_id"),
        "transaction_type": item.get("transaction_type", "TRANSFER"),
        "entry_type": item.get("entry_type"),
        "amount": ledger.money(item.get("amount", Decimal("0"))),
        "currency": item.get("currency", config.settings.currency),
        "counterparty_account_id": item.get("counterparty_account_id"),
        "status": item.get("status", "COMPLETED"),
        "external_reference": item.get("external_reference"),
        "created_at": item.get("created_at"),
    }


@app.get("/health", name="health")
def health():
    settings = config.settings
    return {"status": "ok", "version": settings.app_version, "environment": settings.environment}


@app.get("/me", name="profile")
def get_profile(user: AuthenticatedUser = Depends(current_user)):
    return public_profile(require_profile(user))


@app.post("/me", status_code=201, name="create_profile")
def create_profile(request: ProfileCreate, user: AuthenticatedUser = Depends(current_user)):
    existing = load_profile(user.subject, use_cache=False)
    if existing:
        return JSONResponse(status_code=200, content=public_profile(existing))
    email = verified_email(user)
    try:
        with db.primary.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO customers (user_id, cognito_sub, full_name, email) VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), user.subject, request.full_name, email),
            )
    except UniqueViolation as error:
        raise HTTPException(status_code=409, detail="A customer with this email already exists") from error
    invalidate_profile(user.subject)
    return public_profile(load_profile(user.subject, use_cache=False))


@app.get("/accounts", name="list_accounts")
def list_accounts(user: AuthenticatedUser = Depends(current_user)):
    return {"accounts": require_profile(user)["accounts"]}


@app.post("/accounts", status_code=201, name="open_account")
def open_account(request: AccountCreate, user: AuthenticatedUser = Depends(current_user)):
    profile = require_profile(user)
    limit = ACCOUNT_LIMITS[request.account_type]
    label = request.account_type.lower()
    if sum(1 for account in profile["accounts"] if account["account_type"] == request.account_type) >= limit:
        raise HTTPException(status_code=409, detail=f"You can hold up to {limit} {label} accounts")
    account_id = str(uuid.uuid4())
    currency = config.settings.currency
    balance_created = False
    try:
        with db.primary.connection() as conn, conn.cursor() as cursor:
            # Lock the customer row so two simultaneous requests cannot both pass the limit check.
            cursor.execute("SELECT user_id FROM customers WHERE user_id = %s FOR UPDATE", (profile["customer_id"],))
            cursor.execute(
                "SELECT COUNT(*) AS count FROM accounts WHERE user_id = %s AND account_type = %s",
                (profile["customer_id"], request.account_type),
            )
            if cursor.fetchone()["count"] >= limit:
                raise HTTPException(status_code=409, detail=f"You can hold up to {limit} {label} accounts")
            cursor.execute(
                "INSERT INTO accounts (account_id, user_id, kyc_status, account_type, nickname, currency) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (account_id, profile["customer_id"], profile["kyc_status"], request.account_type, request.nickname, currency),
            )
            ledger.create_account_balance(account_id, user.subject, profile["customer_id"], request.account_type, currency)
            balance_created = True
    except Exception:
        if balance_created:
            try:
                ledger.delete_account_balance(account_id)
            except ClientError:
                logger.exception("Could not remove balance item for account %s after a database failure", account_id)
        raise
    invalidate_profile(user.subject)
    cache.get_cache().set(cache.owner_key(account_id), user.subject, OWNER_TTL_SECONDS)
    return {
        "account_id": account_id,
        "account_type": request.account_type,
        "nickname": request.nickname,
        "currency": currency,
        "balance": "0.00",
    }


@app.post("/kyc/upload", name="kyc_upload")
def upload_kyc_document(file: UploadFile = File(...), user: AuthenticatedUser = Depends(current_user)):
    settings = config.settings
    bucket = required_setting(settings.kyc_statements_bucket, "KYC_STATEMENTS_BUCKET")
    profile = require_profile(user)
    contents = file.file.read(settings.max_upload_bytes + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="KYC document exceeds the 10 MiB limit")
    content_type = (file.content_type or "").lower()
    signatures = KYC_SIGNATURES.get(content_type)
    if signatures is None:
        raise HTTPException(status_code=415, detail="Upload a PDF, PNG or JPG document")
    if not contents.startswith(signatures):
        raise HTTPException(status_code=415, detail="The file content does not match its type")

    safe_name = PurePosixPath((file.filename or "document").replace("\\", "/")).name[:100]
    object_key = f"kyc/{profile['customer_id']}/{uuid.uuid4()}-{safe_name}"
    s3_client().put_object(
        Bucket=bucket,
        Key=object_key,
        Body=contents,
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
    )
    try:
        with db.primary.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE customers SET kyc_status = 'VERIFIED_MOCK', kyc_document_key = %s, "
                "kyc_verified_at = NOW() WHERE cognito_sub = %s RETURNING user_id",
                (object_key, user.subject),
            )
            customer = cursor.fetchone()
            if customer is None:
                raise HTTPException(status_code=404, detail="Customer profile was not found")
            cursor.execute(
                "UPDATE accounts SET kyc_status = 'VERIFIED_MOCK' WHERE user_id = %s",
                (customer["user_id"],),
            )
    except Exception:
        try:
            s3_client().delete_object(Bucket=bucket, Key=object_key)
        except ClientError:
            logger.exception("Could not remove KYC object after the database update failed")
        raise
    invalidate_profile(user.subject)
    return {"kyc_status": "VERIFIED_MOCK", "verification": "simulated", "document_key": object_key}


@app.get("/accounts/{account_id}/balance", name="balance")
def get_account_balance(account_id: str, user: AuthenticatedUser = Depends(current_user)):
    item = require_owned_account(account_id, user, consistent=True)
    return {
        "account_id": item["account_id"],
        "balance": ledger.money(item["balance"]),
        "currency": item.get("currency", config.settings.currency),
        "as_of": item.get("updated_at"),
    }


@app.get("/accounts/{account_id}/transactions", name="transactions")
def get_account_transactions(account_id: str, limit: int = 50, user: AuthenticatedUser = Depends(current_user)):
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    require_owned_account(account_id, user)
    entries = ledger.list_transactions(validate_uuid(account_id), limit)
    return {"account_id": account_id, "transactions": [serialize_entry(entry) for entry in entries]}


@app.post("/transfers", status_code=201, name="transfer")
def create_transfer(
    request: TransferRequest,
    user: AuthenticatedUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = parse_idempotency_key(idempotency_key)
    if request.sender_account_id == request.receiver_account_id:
        raise HTTPException(status_code=400, detail="Sender and receiver accounts must differ")
    require_verified_identity(require_profile(user))

    fingerprint = ledger.request_fingerprint(
        "TRANSFER",
        {
            "sender": request.sender_account_id,
            "receiver": request.receiver_account_id,
            "amount": ledger.money(request.amount),
        },
    )
    previous = ledger.find_previous_result(user.subject, key, "TRANSFER", fingerprint)
    if previous:
        return ledger_response(previous)

    sender = ledger.get_balance_item(request.sender_account_id)
    if not sender or sender.get("owner_sub") != user.subject:
        raise HTTPException(status_code=404, detail="Account was not found")
    receiver = ledger.get_balance_item(request.receiver_account_id)
    if not receiver or receiver.get("record_type") != "ACCOUNT_BALANCE":
        raise HTTPException(status_code=404, detail="Recipient account was not found")

    features = fraud.extract_features(
        request.amount,
        Decimal(str(sender.get("balance", "0"))),
        request.receiver_account_id,
        ledger.list_transactions(request.sender_account_id, 50),
    )
    score = fraud.score_transfer(features)
    logger.info(
        "Fraud screening transfer key=%s score=%.4f model=%s decision=%s",
        key,
        score.score,
        score.model,
        score.decision,
    )
    if score.decision == "BLOCK":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "fraud_blocked",
                "message": "This transfer was blocked by fraud screening. Contact us if this was you.",
            },
        )
    if request.amount >= config.settings.step_up_amount:
        auth.require_recent_authentication(user, "high_value_transfer")
    elif score.decision == "STEP_UP":
        auth.require_recent_authentication(user, "unusual_transfer")

    result = ledger.execute_transfer(
        subject=user.subject,
        key=key,
        fingerprint=fingerprint,
        sender_account_id=request.sender_account_id,
        receiver_account_id=request.receiver_account_id,
        receiver_subject=str(receiver.get("owner_sub", "")),
        amount=request.amount,
        currency=str(sender.get("currency", config.settings.currency)),
        fraud_score=score.score,
        fraud_model=score.model,
    )
    return ledger_response(result)


@app.get("/external-banks", name="external_banks")
def list_external_banks(user: AuthenticatedUser = Depends(current_user)):
    return {
        "banks": [{"code": code, "name": name} for code, name in external_bank.PARTNER_BANKS.items()],
        "partner_limit": ledger.money(external_bank.PARTNER_LIMIT),
        "simulated": True,
    }


def _external_setup(account_id: str, request: ExternalTransferRequest, user: AuthenticatedUser, operation: str, key: str):
    require_verified_identity(require_profile(user))
    account_id = validate_uuid(account_id)
    require_owned_account(account_id, user)
    invalid = external_bank.validate_destination(request.bank_code, request.external_account)
    if invalid:
        raise HTTPException(status_code=400, detail=invalid)
    fingerprint = ledger.request_fingerprint(
        operation,
        {
            "account": account_id,
            "amount": ledger.money(request.amount),
            "bank": request.bank_code,
            "external_account": request.external_account,
        },
    )
    return account_id, fingerprint, ledger.find_previous_result(user.subject, key, operation, fingerprint)


@app.post("/accounts/{account_id}/deposits", status_code=201, name="deposit")
def create_deposit(
    account_id: str,
    request: ExternalTransferRequest,
    user: AuthenticatedUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = parse_idempotency_key(idempotency_key)
    account_id, fingerprint, previous = _external_setup(account_id, request, user, "DEPOSIT", key)
    if previous:
        return ledger_response(previous)
    bank = external_bank.pull_funds(request.bank_code, request.external_account, request.amount, f"{user.subject}:{key}")
    if not bank.approved:
        raise HTTPException(status_code=422, detail=f"Deposit declined by the partner bank: {bank.reason}")
    result = ledger.execute_deposit(
        subject=user.subject,
        key=key,
        fingerprint=fingerprint,
        account_id=account_id,
        amount=request.amount,
        currency=config.settings.currency,
        bank_code=request.bank_code,
        external_reference=bank.reference,
    )
    return ledger_response(result)


@app.post("/accounts/{account_id}/withdrawals", status_code=201, name="withdrawal")
def create_withdrawal(
    account_id: str,
    request: ExternalTransferRequest,
    user: AuthenticatedUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = parse_idempotency_key(idempotency_key)
    account_id, fingerprint, previous = _external_setup(account_id, request, user, "WITHDRAWAL", key)
    if previous:
        return ledger_response(previous)
    if request.amount >= config.settings.step_up_amount:
        auth.require_recent_authentication(user, "high_value_withdrawal")

    pending = ledger.begin_withdrawal(
        subject=user.subject,
        key=key,
        fingerprint=fingerprint,
        account_id=account_id,
        amount=request.amount,
        currency=config.settings.currency,
        bank_code=request.bank_code,
    )
    if isinstance(pending, ledger.LedgerResult):
        return ledger_response(pending)
    bank = external_bank.push_funds(request.bank_code, request.external_account, request.amount, f"{user.subject}:{key}")
    if bank.approved:
        result = ledger.complete_withdrawal(pending, bank.reference)
    else:
        result = ledger.reverse_withdrawal(pending, bank.reason or "Declined")
    return ledger_response(result)


def statement_view(job: dict, include_url: bool = False) -> dict:
    view = {
        "job_id": job["job_id"],
        "account_id": job["account_id"],
        "status": job.get("status", "QUEUED"),
        "type": job.get("type", "ON_DEMAND"),
        "start_date": job.get("start_date"),
        "end_date": job.get("end_date"),
        "requested_at": job.get("requested_at"),
        "completed_at": job.get("completed_at"),
    }
    if include_url and view["status"] == "COMPLETED" and job.get("object_key"):
        filename = f"facaibank-statement-{(job.get('completed_at') or '')[:10] or job['job_id'][:8]}.pdf"
        view["download_url"] = s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": required_setting(config.settings.kyc_statements_bucket, "KYC_STATEMENTS_BUCKET"),
                "Key": job["object_key"],
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=config.settings.statement_url_ttl_seconds,
        )
        view["download_url_expires_in"] = config.settings.statement_url_ttl_seconds
    return view


@app.post("/statements/request", status_code=202, name="request_statement")
def request_statement(request: StatementRequest, user: AuthenticatedUser = Depends(current_user)):
    queue_url = required_setting(config.settings.statement_queue_url, "STATEMENT_QUEUE_URL")
    require_owned_account(request.account_id, user)
    job = {
        "job_id": str(uuid.uuid4()),
        "account_id": request.account_id,
        "owner_sub": user.subject,
        "type": "ON_DEMAND",
        "start_date": request.start_date.isoformat() if request.start_date else None,
        "end_date": request.end_date.isoformat() if request.end_date else None,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    ledger.put_statement_job(job)
    sqs_client().send_message(QueueUrl=queue_url, MessageBody=json.dumps(job, separators=(",", ":")))
    return {"job_id": job["job_id"], "account_id": request.account_id, "status": "QUEUED"}


@app.get("/accounts/{account_id}/statements", name="list_statements")
def list_statements(account_id: str, user: AuthenticatedUser = Depends(current_user)):
    require_owned_account(account_id, user)
    jobs = ledger.list_statement_jobs(validate_uuid(account_id))
    return {"statements": [statement_view(job) for job in jobs]}


@app.get("/accounts/{account_id}/statements/{job_id}", name="statement")
def get_statement(account_id: str, job_id: str, user: AuthenticatedUser = Depends(current_user)):
    require_owned_account(account_id, user)
    job = ledger.get_statement_job(validate_uuid(account_id), validate_uuid(job_id, "Statement"))
    if not job or job.get("owner_sub") != user.subject:
        raise HTTPException(status_code=404, detail="Statement was not found")
    return statement_view(job, include_url=True)


def subscription_status(subscription_arn: str | None) -> str:
    if not subscription_arn or not subscription_arn.startswith("arn:"):
        return "NONE"
    try:
        attributes = sns_client().get_subscription_attributes(SubscriptionArn=subscription_arn)["Attributes"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NotFound", "NotFoundException"}:
            return "NONE"
        raise
    return "PENDING_CONFIRMATION" if attributes.get("PendingConfirmation") == "true" else "ACTIVE"


@app.get("/me/alerts", name="alert_status")
def get_alert_status(user: AuthenticatedUser = Depends(current_user)):
    profile = require_profile(user)
    return {"status": subscription_status(profile.get("alert_subscription_arn")), "email": profile["email"]}


@app.post("/me/alerts", status_code=202, name="subscribe_alerts")
def subscribe_alerts(user: AuthenticatedUser = Depends(current_user)):
    topic_arn = required_setting(config.settings.notifications_topic_arn, "NOTIFICATIONS_TOPIC_ARN")
    profile = require_profile(user)
    status = subscription_status(profile.get("alert_subscription_arn"))
    if status != "NONE":
        return {"status": status, "email": profile["email"]}
    response = sns_client().subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=profile["email"],
        Attributes={
            "FilterPolicy": json.dumps({"customer_sub": [user.subject]}),
            "FilterPolicyScope": "MessageAttributes",
        },
        ReturnSubscriptionArn=True,
    )
    with db.primary.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "UPDATE customers SET alert_subscription_arn = %s WHERE cognito_sub = %s",
            (response["SubscriptionArn"], user.subject),
        )
    invalidate_profile(user.subject)
    return {"status": "PENDING_CONFIRMATION", "email": profile["email"]}


@app.post("/auth/logout", status_code=204, name="logout")
def logout(user: AuthenticatedUser = Depends(current_user)):
    auth.revoke_session(user)
    return Response(status_code=204)
