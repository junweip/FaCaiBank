"""Backup restoration test with timed recovery (slides 14-15).

DynamoDB: records a point in time, captures the ledger's item count and the
sum of all balances (zero-sum: customer balances plus clearing accounts),
restores the table to that second with PITR, and compares. RDS (optional,
--include-rds): restores the accounts database to a new instance from
automated backups and times it. Restored copies are deleted afterwards unless
--keep is passed.

    python tests/restore_test.py --execute [--include-rds]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import write_evidence  # noqa: E402

RTO_SECONDS = 600


def ledger_fingerprint(table) -> dict:
    items = balance_items = 0
    balance_sum = Decimal("0")
    request: dict = {"ConsistentRead": True}
    while True:
        page = table.scan(**request)
        for item in page.get("Items", []):
            items += 1
            if item.get("SK") == "BALANCE":
                balance_items += 1
                balance_sum += Decimal(str(item.get("balance", 0)))
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return {"items": items, "balance_items": balance_items, "balance_sum": str(balance_sum)}


def restore_dynamodb(region: str, table_name: str, keep: bool) -> dict:
    dynamodb = boto3.client("dynamodb", region_name=region)
    resource = boto3.resource("dynamodb", region_name=region)
    description = dynamodb.describe_continuous_backups(TableName=table_name)["ContinuousBackupsDescription"]
    pitr = description["PointInTimeRecoveryDescription"]
    if pitr["PointInTimeRecoveryStatus"] != "ENABLED":
        raise RuntimeError("Point-in-time recovery is not enabled on the ledger table")

    # Fingerprint the table, then restore to a moment just after the scan finished,
    # while no test traffic is running.
    before = ledger_fingerprint(resource.Table(table_name))
    restore_point = datetime.now(timezone.utc)
    time.sleep(5)
    target = f"{table_name}-restore-{restore_point.strftime('%Y%m%d%H%M%S')}"
    started = time.monotonic()
    dynamodb.restore_table_to_point_in_time(
        SourceTableName=table_name,
        TargetTableName=target,
        RestoreDateTime=restore_point,
        BillingModeOverride="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=target, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
    while dynamodb.describe_table(TableName=target)["Table"]["TableStatus"] != "ACTIVE":
        time.sleep(10)
    restore_seconds = round(time.monotonic() - started, 1)
    after = ledger_fingerprint(resource.Table(target))
    matches = before == after
    if not keep:
        dynamodb.delete_table(TableName=target)
    return {
        "status": "PASS" if matches else "FAIL",
        "restore_point": restore_point.isoformat(),
        "earliest_restorable": pitr.get("EarliestRestorableDateTime"),
        "source_fingerprint": before,
        "restored_fingerprint": after,
        "data_matches": matches,
        "restored_table": target,
        "restore_seconds": restore_seconds,
        "zero_sum_ledger": Decimal(after["balance_sum"]) == 0,
        "note": "Restore time grows with table size; RPO is the restore point (1 s granularity).",
    }


def restore_rds(region: str, identifier: str, keep: bool) -> dict:
    rds = boto3.client("rds", region_name=region)
    source = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    latest = source["LatestRestorableTime"]
    target = f"{identifier}-restore-{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
    started = time.monotonic()
    rds.restore_db_instance_to_point_in_time(
        SourceDBInstanceIdentifier=identifier,
        TargetDBInstanceIdentifier=target,
        UseLatestRestorableTime=True,
        DBInstanceClass=source["DBInstanceClass"],
        DBSubnetGroupName=source["DBSubnetGroup"]["DBSubnetGroupName"],
        VpcSecurityGroupIds=[group["VpcSecurityGroupId"] for group in source["VpcSecurityGroups"]],
        PubliclyAccessible=False,
        MultiAZ=False,
        DeletionProtection=False,
        CopyTagsToSnapshot=True,
    )
    rds.get_waiter("db_instance_available").wait(
        DBInstanceIdentifier=target, WaiterConfig={"Delay": 30, "MaxAttempts": 120}
    )
    restore_seconds = round(time.monotonic() - started, 1)
    if not keep:
        rds.delete_db_instance(DBInstanceIdentifier=target, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
    return {
        "status": "PASS",
        "source": identifier,
        "restored_instance": target,
        "restored_to": latest.isoformat() if hasattr(latest, "isoformat") else latest,
        "backup_lag_seconds": round((datetime.now(timezone.utc) - latest).total_seconds()) if hasattr(latest, "tzinfo") else None,
        "restore_seconds": restore_seconds,
        "note": "An RDS PITR restore is a disaster-recovery path; AZ failure is handled by Multi-AZ failover instead.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--ledger-table", default=os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger"))
    parser.add_argument("--rds-identifier", default=os.getenv("RDS_IDENTIFIER", "facaibank-dev-postgres"))
    parser.add_argument("--include-rds", action="store_true", help="Also restore RDS (takes 10-20 minutes and costs extra)")
    parser.add_argument("--keep", action="store_true", help="Keep restored copies for inspection")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("this test creates restored copies of production-shaped data; pass --execute")

    evidence = {"project": "FaCaiBank", "test": "restore", "started_at": datetime.now(timezone.utc).isoformat(), "rto_target_seconds": RTO_SECONDS}
    evidence["dynamodb"] = restore_dynamodb(args.region, args.ledger_table, args.keep)
    if args.include_rds:
        evidence["rds"] = restore_rds(args.region, args.rds_identifier, args.keep)
    evidence["status"] = "PASS" if all(evidence[key]["status"] == "PASS" for key in ("dynamodb", "rds") if key in evidence) else "FAIL"
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    path = write_evidence("restore", evidence)
    print(f"{evidence['status']}: DynamoDB restored in {evidence['dynamodb']['restore_seconds']} s; evidence at {path}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
