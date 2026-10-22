"""Statement worker: SQS job -> PDF statement in S3 (SSE-KMS).

On-demand jobs come from the API. On the 1st of each month EventBridge
Scheduler sends one MONTHLY_RUN message; the worker lists every account from
the RDS read replica and queues one statement job per account for the
previous calendar month.
"""

import json
import logging
import os
import signal
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

REGION = os.getenv("AWS_REGION", "ap-southeast-1")
TABLE_NAME = os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger")
QUEUE_URL = os.getenv("STATEMENT_QUEUE_URL", "")
BUCKET_NAME = os.getenv("KYC_STATEMENTS_BUCKET", "")
DATABASE_HOST = os.getenv("DATABASE_HOST", "")
DATABASE_READ_HOST = os.getenv("DATABASE_READ_HOST", "")
DATABASE_NAME = os.getenv("DATABASE_NAME", "facaibank")
DATABASE_SECRET_ARN = os.getenv("DATABASE_SECRET_ARN", "")
MAX_RECEIVES = int(os.getenv("STATEMENT_MAX_RECEIVES", "3"))
CURRENCY = os.getenv("CURRENCY", "SGD")
SINGAPORE = timezone(timedelta(hours=8))
MONTHLY_NAMESPACE = uuid.UUID("5d0f5a39-5f55-4c55-9a8e-6f0d2b3c4a11")
running = True


def _stop(_signum, _frame):
    global running
    running = False


def dynamodb_resource():
    return boto3.resource("dynamodb", region_name=REGION)


def sqs_client():
    return boto3.client("sqs", region_name=REGION)


def s3_client():
    return boto3.client("s3", region_name=REGION)


# ---------------------------------------------------------------- database (read replica)

_credentials: dict | None = None
_credentials_loaded = 0.0
_credentials_lock = threading.Lock()


def _database_credentials() -> dict:
    global _credentials, _credentials_loaded
    with _credentials_lock:
        if _credentials is None or time.monotonic() - _credentials_loaded > 300:
            secret = boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=DATABASE_SECRET_ARN)
            _credentials = json.loads(secret["SecretString"])
            _credentials_loaded = time.monotonic()
        return _credentials


def database_connection():
    """Connect to the read replica when one exists, otherwise the primary."""
    host = DATABASE_READ_HOST or DATABASE_HOST
    if not host or not DATABASE_SECRET_ARN:
        return None
    import psycopg
    from psycopg.rows import dict_row

    endpoint, _, port = host.partition(":")
    credentials = _database_credentials()
    return psycopg.connect(
        host=endpoint,
        port=int(port or 5432),
        dbname=DATABASE_NAME,
        user=credentials["username"],
        password=credentials["password"],
        sslmode="require",
        connect_timeout=5,
        row_factory=dict_row,
        application_name="facaibank-statement-worker",
    )


def account_holder(account_id: str) -> dict | None:
    connection = database_connection()
    if connection is None:
        return None
    with connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.full_name, a.account_type, a.nickname FROM accounts a "
            "JOIN customers c ON c.user_id = a.user_id WHERE a.account_id = %s",
            (account_id,),
        )
        return cursor.fetchone()


def statement_accounts() -> list[dict]:
    connection = database_connection()
    if connection is None:
        raise RuntimeError("DATABASE_HOST and DATABASE_SECRET_ARN are required for the monthly run")
    with connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.account_id::text AS account_id, c.cognito_sub AS owner_sub FROM accounts a "
            "JOIN customers c ON c.user_id = a.user_id WHERE c.cognito_sub IS NOT NULL ORDER BY a.created_at"
        )
        return list(cursor.fetchall())


# ---------------------------------------------------------------- statement content


def load_transactions(table, account_id: str) -> list[dict[str, Any]]:
    items = []
    request = {
        "KeyConditionExpression": Key("PK").eq(f"ACCOUNT#{account_id}") & Key("SK").begins_with("TXN#"),
        "ConsistentRead": False,
    }
    while True:
        response = table.query(**request)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        request["ExclusiveStartKey"] = last_key


def in_date_range(item: dict[str, Any], start_date: date | None, end_date: date | None) -> bool:
    created_at = item.get("created_at")
    if not created_at:
        return True
    try:
        transaction_date = (
            datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).astimezone(SINGAPORE).date()
        )
    except ValueError:
        logger.warning("Ignoring unparseable transaction date %r", created_at)
        return True
    return not (
        (start_date is not None and transaction_date < start_date)
        or (end_date is not None and transaction_date > end_date)
    )


DESCRIPTIONS = {
    ("TRANSFER", "DEBIT"): "Transfer sent",
    ("TRANSFER", "CREDIT"): "Transfer received",
    ("DEPOSIT", "CREDIT"): "Deposit from {bank}",
    ("WITHDRAWAL", "DEBIT"): "Withdrawal to {bank}",
    ("REVERSAL", "CREDIT"): "Withdrawal returned by {bank}",
}


def statement_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for item in sorted(items, key=lambda entry: str(entry.get("created_at", ""))):
        amount = Decimal(str(item.get("amount", "0"))).quantize(Decimal("0.01"))
        entry_type = str(item.get("entry_type", ""))
        kind = str(item.get("transaction_type", "TRANSFER"))
        description = DESCRIPTIONS.get((kind, entry_type), kind.title()).format(bank=item.get("bank_code", "bank"))
        if item.get("status") == "REVERSED":
            description += " (reversed)"
        try:
            when = datetime.fromisoformat(str(item.get("created_at")).replace("Z", "+00:00")).astimezone(SINGAPORE)
            when_text = when.strftime("%d %b %Y %H:%M")
        except ValueError:
            when_text = str(item.get("created_at", ""))
        rows.append(
            {
                "date": when_text,
                "description": description,
                "reference": str(item.get("transaction_id") or item.get("transfer_id") or "")[:8],
                "amount": f"-{amount:,.2f}" if entry_type == "DEBIT" else f"+{amount:,.2f}",
                "signed": str(-amount if entry_type == "DEBIT" else amount),
            }
        )
    return rows


def _latin1(value: Any) -> str:
    return str(value).encode("latin-1", "replace").decode("latin-1")


def render_statement_pdf(job: dict, holder: dict | None, rows: list[dict[str, str]], compress: bool = True) -> bytes:
    from fpdf import FPDF

    money_in = sum((Decimal(row["signed"]) for row in rows if Decimal(row["signed"]) > 0), Decimal("0"))
    money_out = sum((-Decimal(row["signed"]) for row in rows if Decimal(row["signed"]) < 0), Decimal("0"))
    pdf = FPDF(format="A4")
    pdf.set_compression(compress)
    pdf.set_title("FaCaiBank statement")
    pdf.set_author("FaCaiBank")
    pdf.add_page()
    pdf.set_text_color(36, 84, 59)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "FACAI BANK", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(32, 55, 42)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 11, "Account statement", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(36, 84, 59)
    pdf.set_line_width(0.8)
    pdf.line(10, pdf.get_y() + 2, 200, pdf.get_y() + 2)
    pdf.ln(7)

    period = f"{job.get('start_date') or 'All available'} to {job.get('end_date') or 'present'}"
    details = [
        ("Account holder", (holder or {}).get("full_name") or "-"),
        ("Account", job["account_id"]),
        ("Account type", ((holder or {}).get("nickname") or (holder or {}).get("account_type") or "Everyday").title()),
        ("Period", period),
        ("Generated", datetime.now(SINGAPORE).strftime("%d %b %Y %H:%M SGT")),
    ]
    pdf.set_font("Helvetica", "", 9)
    for label, value in details:
        pdf.set_text_color(99, 114, 103)
        pdf.cell(40, 6, _latin1(label))
        pdf.set_text_color(32, 55, 42)
        pdf.cell(0, 6, _latin1(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    widths = (42, 80, 28, 40)
    pdf.set_fill_color(237, 243, 232)
    pdf.set_text_color(53, 88, 65)
    pdf.set_font("Helvetica", "B", 9)
    for width, heading, align in zip(widths, ("Date", "Description", "Reference", f"Amount ({CURRENCY})"), "LLLR"):
        pdf.cell(width, 8, heading, fill=True, align=align)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(32, 55, 42)
    if not rows:
        pdf.cell(sum(widths), 10, "No transactions for this period.", align="C", new_x="LMARGIN", new_y="NEXT")
    for row in rows:
        pdf.cell(widths[0], 7, _latin1(row["date"]))
        pdf.cell(widths[1], 7, _latin1(row["description"])[:48])
        pdf.cell(widths[2], 7, _latin1(row["reference"]))
        pdf.cell(widths[3], 7, row["amount"], align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 9)
    for label, value in (("Money in", money_in), ("Money out", money_out), ("Net change", money_in - money_out)):
        pdf.cell(sum(widths[:3]), 6, label, align="R")
        pdf.cell(widths[3], 6, f"{value:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(130, 144, 135)
    pdf.multi_cell(
        0,
        4,
        "Generated by FaCaiBank from the DynamoDB ledger. History is read with eventual consistency, so "
        "transactions from the last few seconds may appear on your next statement. Prototype - not a legal record.",
    )
    return bytes(pdf.output())


def parse_optional_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Statement {field} must be an ISO date") from error


def statement_key(account_id: str, job_id: str) -> dict:
    return {"PK": f"ACCOUNT#{account_id}", "SK": f"STATEMENT#{job_id}"}


def process_job(job: dict[str, Any], table=None, s3=None, holder_lookup=account_holder) -> dict[str, Any]:
    account_id = job.get("account_id")
    job_id = job.get("job_id")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("Statement job must include account_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("Statement job must include job_id")
    start_date = parse_optional_date(job.get("start_date"), "start_date")
    end_date = parse_optional_date(job.get("end_date"), "end_date")
    if start_date and end_date and end_date < start_date:
        raise ValueError("Statement end_date must be on or after start_date")
    if not BUCKET_NAME:
        raise RuntimeError("KYC_STATEMENTS_BUCKET must be configured")

    table = table or dynamodb_resource().Table(TABLE_NAME)
    s3 = s3 or s3_client()
    transactions = [item for item in load_transactions(table, account_id) if in_date_range(item, start_date, end_date)]
    try:
        holder = holder_lookup(account_id)
    except Exception:
        logger.warning("Could not read account holder for %s from the read replica", account_id, exc_info=True)
        holder = None
    rows = statement_rows(transactions)
    content = render_statement_pdf(job, holder, rows)
    object_key = f"statements/{account_id}/{job_id}.pdf"
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=object_key,
        Body=content,
        ContentType="application/pdf",
        ServerSideEncryption="aws:kms",
    )
    record = {
        **statement_key(account_id, job_id),
        "record_type": "STATEMENT_JOB",
        "job_id": job_id,
        "account_id": account_id,
        "owner_sub": job.get("owner_sub"),
        "type": job.get("type", "ON_DEMAND"),
        "start_date": job.get("start_date"),
        "end_date": job.get("end_date"),
        "requested_at": job.get("requested_at"),
        "status": "COMPLETED",
        "object_key": object_key,
        "transaction_count": len(rows),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    table.put_item(Item={key: value for key, value in record.items() if value is not None})
    return {"job_id": job_id, "object_key": object_key, "transaction_count": len(rows)}


def mark_failed(table, job: dict, reason: str) -> None:
    if not job.get("account_id") or not job.get("job_id"):
        return
    try:
        table.update_item(
            Key=statement_key(job["account_id"], job["job_id"]),
            UpdateExpression="SET #status = :failed, failure_reason = :reason",
            ConditionExpression="attribute_exists(PK) AND #status <> :completed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": "FAILED", ":reason": reason[:500], ":completed": "COMPLETED"},
        )
    except ClientError:
        logger.warning("Could not mark statement job %s as failed", job.get("job_id"), exc_info=True)


def previous_month(today: date) -> tuple[date, date]:
    first_of_this_month = today.replace(day=1)
    end = first_of_this_month - timedelta(days=1)
    return end.replace(day=1), end


def fan_out_monthly(table, sqs, accounts_lookup=statement_accounts, today: date | None = None) -> int:
    today = today or datetime.now(SINGAPORE).date()
    start, end = previous_month(today)
    period = start.strftime("%Y-%m")
    requested_at = datetime.now(timezone.utc).isoformat()
    queued = 0
    batch: list[dict] = []

    def flush():
        nonlocal batch
        if batch:
            response = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)
            if response.get("Failed"):
                raise RuntimeError(f"Could not queue {len(response['Failed'])} monthly statement jobs")
            batch = []

    for account in accounts_lookup():
        job = {
            "job_id": str(uuid.uuid5(MONTHLY_NAMESPACE, f"{account['account_id']}:{period}")),
            "account_id": account["account_id"],
            "owner_sub": account["owner_sub"],
            "type": "MONTHLY",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "requested_at": requested_at,
        }
        try:
            table.put_item(
                Item={**statement_key(job["account_id"], job["job_id"]), "record_type": "STATEMENT_JOB", "status": "QUEUED", **job},
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise
        batch.append({"Id": str(len(batch)), "MessageBody": json.dumps(job, separators=(",", ":"))})
        queued += 1
        if len(batch) == 10:
            flush()
    flush()
    logger.info("Queued %s monthly statements for %s", queued, period)
    return queued


def process_message(message: dict[str, Any], table=None, s3=None, sqs=None) -> None:
    job = json.loads(message["Body"])
    if not isinstance(job, dict):
        raise ValueError("Statement queue message must be a JSON object")
    if job.get("type") == "MONTHLY_RUN":
        fan_out_monthly(table or dynamodb_resource().Table(TABLE_NAME), sqs or sqs_client())
        return
    result = process_job(job, table=table, s3=s3)
    logger.info("Statement job completed job_id=%s object_key=%s", result["job_id"], result["object_key"])


def run() -> None:
    if not QUEUE_URL:
        raise RuntimeError("STATEMENT_QUEUE_URL must be configured")
    if not BUCKET_NAME:
        raise RuntimeError("KYC_STATEMENTS_BUCKET must be configured")

    sqs = sqs_client()
    table = dynamodb_resource().Table(TABLE_NAME)
    s3 = s3_client()
    while running:
        response = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=300,
            AttributeNames=["ApproximateReceiveCount"],
        )
        for message in response.get("Messages", []):
            try:
                process_message(message, table=table, s3=s3, sqs=sqs)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])
            except Exception as error:
                logger.exception("Statement job failed message_id=%s", message.get("MessageId", "unknown"))
                receives = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
                if receives >= MAX_RECEIVES:
                    try:
                        mark_failed(table, json.loads(message["Body"]), str(error))
                    except (ValueError, TypeError):
                        pass


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    run()
