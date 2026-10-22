"""Glue Python shell job: latest DynamoDB export -> curated Parquet ledger table.

Reads the newest completed point-in-time export under s3://<lake>/raw/, keeps
ledger entries, flattens them and rewrites s3://<lake>/curated/ledger_entries/
partitioned by Singapore transaction date. The Glue crawler then catalogs the
table for Athena and QuickSight. Runs entirely off the request path.
"""

import gzip
import io
import json
import sys
from datetime import datetime, timedelta, timezone

SINGAPORE = timezone(timedelta(hours=8))


def attribute(value):
    """Convert one DynamoDB-JSON attribute to a plain Python value."""
    if not isinstance(value, dict) or len(value) != 1:
        return value
    (kind, raw), = value.items()
    if kind == "S":
        return raw
    if kind == "N":
        return float(raw)
    if kind == "BOOL":
        return bool(raw)
    if kind == "NULL":
        return None
    if kind == "M":
        return {key: attribute(item) for key, item in raw.items()}
    if kind == "L":
        return [attribute(item) for item in raw]
    return raw


def flatten(item):
    """Return an analytics row for a customer ledger entry, or None to skip it."""
    record = {key: attribute(value) for key, value in item.items()}
    if record.get("record_type") != "LEDGER_ENTRY" or not str(record.get("PK", "")).startswith("ACCOUNT#"):
        return None
    account_id = record["PK"].split("#", 1)[1]
    created = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00")).astimezone(SINGAPORE)
    return {
        "transaction_id": record.get("transaction_id") or record.get("transfer_id"),
        "account_id": account_id,
        "is_clearing_account": account_id.startswith("CLEARING-"),
        "transaction_type": record.get("transaction_type", "TRANSFER"),
        "entry_type": record.get("entry_type"),
        "status": record.get("status", "COMPLETED"),
        "amount": float(record.get("amount", 0)),
        "currency": record.get("currency", "SGD"),
        "fraud_score": record.get("fraud_score"),
        "fraud_model": record.get("fraud_model"),
        "bank_code": record.get("bank_code"),
        "created_at": created.isoformat(),
        "txn_hour_sgt": created.hour,
        "txn_date": created.date().isoformat(),
    }


def latest_export(s3, bucket, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    summaries = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}AWSDynamoDB/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("manifest-summary.json"):
                summary = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                summaries.append(summary)
    completed = [summary for summary in summaries if summary.get("manifestFilesS3Key")]
    if not completed:
        raise RuntimeError("No completed DynamoDB export found")
    return max(completed, key=lambda summary: summary.get("exportTime", ""))


def read_rows(s3, bucket, summary):
    manifest = s3.get_object(Bucket=bucket, Key=summary["manifestFilesS3Key"])["Body"].read().decode()
    for line in manifest.splitlines():
        data_key = json.loads(line)["dataFileS3Key"]
        body = s3.get_object(Bucket=bucket, Key=data_key)["Body"].read()
        for raw in gzip.GzipFile(fileobj=io.BytesIO(body)).read().decode().splitlines():
            if raw.strip():
                row = flatten(json.loads(raw)["Item"])
                if row:
                    yield row


def main(argv):
    import boto3
    import pandas as pd
    from awsglue.utils import getResolvedOptions

    args = getResolvedOptions(argv, ["LAKE_BUCKET", "RAW_PREFIX", "CURATED_PREFIX", "KMS_KEY_ARN"])
    s3 = boto3.client("s3")
    summary = latest_export(s3, args["LAKE_BUCKET"], args["RAW_PREFIX"])
    frame = pd.DataFrame(list(read_rows(s3, args["LAKE_BUCKET"], summary)))

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args["LAKE_BUCKET"], Prefix=args["CURATED_PREFIX"]):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=args["LAKE_BUCKET"], Delete={"Objects": keys})

    for txn_date, partition in frame.groupby("txn_date") if not frame.empty else []:
        buffer = io.BytesIO()
        partition.drop(columns=["txn_date"]).to_parquet(buffer, index=False)
        s3.put_object(
            Bucket=args["LAKE_BUCKET"],
            Key=f"{args['CURATED_PREFIX']}txn_date={txn_date}/part-0000.parquet",
            Body=buffer.getvalue(),
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=args["KMS_KEY_ARN"],
        )
    print(f"Wrote {len(frame)} ledger rows from export {summary.get('exportArn')}")


if __name__ == "__main__":
    main(sys.argv)
