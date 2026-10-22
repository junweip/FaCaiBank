"""Background-job recovery: worker stop, crash and restart (slide 14, stage 3).

1. Stops every outbox worker through Session Manager, makes transfers while no
   worker runs, then starts them again and measures how long until every
   transfer's notifications are SENT exactly once (resume from checkpoint).
2. Kills (SIGKILL) the outbox workers mid-traffic and checks nothing is lost.
3. Stops the statement workers, queues a statement, restarts them and checks
   the job completes (SQS kept the job while no worker ran).

    python tests/recovery_test.py --execute
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).parent))
from live_support import provision_customer, write_evidence  # noqa: E402


def fleet_instances(region: str, asg_name: str) -> list[str]:
    groups = boto3.client("autoscaling", region_name=region).describe_auto_scaling_groups(
        AutoScalingGroupNames=[asg_name]
    )["AutoScalingGroups"]
    return [instance["InstanceId"] for group in groups for instance in group["Instances"] if instance["LifecycleState"] == "InService"]


def run_on(region: str, instance_ids: list[str], command: str, timeout: int = 120) -> list[dict]:
    ssm = boto3.client("ssm", region_name=region)
    command_id = ssm.send_command(
        InstanceIds=instance_ids,
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        TimeoutSeconds=timeout,
    )["Command"]["CommandId"]
    results = []
    for instance_id in instance_ids:
        for _ in range(timeout):
            time.sleep(1)
            try:
                invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            except ssm.exceptions.InvocationDoesNotExist:
                continue
            if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
                results.append({"instance": instance_id, "status": invocation["Status"], "output": invocation["StandardOutputContent"][-300:]})
                break
    return results


def notification_states(table, transaction_ids: list[str]) -> dict[str, list[str]]:
    """Map each transaction to the dedupe statuses of its notifications."""
    states = {}
    for transaction_id in transaction_ids:
        outbox = table.get_item(Key={"PK": f"OUTBOX#{transaction_id}", "SK": "EVENT"}, ConsistentRead=True).get("Item")
        if not outbox:
            states[transaction_id] = ["NO_OUTBOX"]
            continue
        event_id = outbox["event_id"]
        records = table.query(
            KeyConditionExpression=Key("PK").eq(f"DEDUPE#{event_id}"),
            ConsistentRead=True,
        )["Items"]
        states[transaction_id] = sorted(record.get("status", "?") for record in records)
    return states


def wait_for_delivery(table, transaction_ids: list[str], timeout: int) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        states = notification_states(table, transaction_ids)
        if all(state == ["SENT", "SENT"] for state in states.values()):
            return round(time.monotonic() - started, 1)
        time.sleep(3)
    raise AssertionError(f"Notifications not delivered within {timeout}s: {notification_states(table, transaction_ids)}")


def transfer_batch(api, sender: str, receiver: str, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        result = api.expect(
            201,
            "POST",
            "/transfers",
            {"sender_account_id": sender, "receiver_account_id": receiver, "amount": "1.00"},
            headers={"Idempotency-Key": f"recovery-{uuid.uuid4()}"},
        )
        ids.append(result["transaction_id"])
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--api-url", default=os.getenv("FACAI_API_URL"))
    parser.add_argument("--user-pool-id", default=os.getenv("FACAI_USER_POOL_ID"))
    parser.add_argument("--client-id", default=os.getenv("FACAI_CLIENT_ID"))
    parser.add_argument("--ledger-table", default=os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--env", default=os.getenv("FACAI_ENV", "dev"))
    parser.add_argument("--transfers", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("this test stops and kills live workers; pass --execute")

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.ledger_table)
    outbox_fleet = fleet_instances(args.region, f"facaibank-{args.env}-outbox-workers")
    statement_fleet = fleet_instances(args.region, f"facaibank-{args.env}-statement-workers")
    if not outbox_fleet or not statement_fleet:
        raise SystemExit("Both worker fleets need at least one InService instance")

    sender, sender_api, (from_account, to_account) = provision_customer(
        args.api_url, args.region, args.user_pool_id, args.client_id, "recovery", accounts=2, deposit="200.00"
    )
    evidence = {"project": "FaCaiBank", "test": "worker-recovery", "started_at": datetime.now(timezone.utc).isoformat(), "checks": {}}
    try:
        # 1. Planned stop and restart.
        run_on(args.region, outbox_fleet, "systemctl stop facaibank-outbox-worker")
        stopped_ids = transfer_batch(sender_api, from_account, to_account, args.transfers)
        time.sleep(20)
        undelivered = notification_states(table, stopped_ids)
        run_on(args.region, outbox_fleet, "systemctl start facaibank-outbox-worker")
        restart_seconds = wait_for_delivery(table, stopped_ids, args.timeout)
        evidence["checks"]["planned_restart"] = {
            "status": "PASS",
            "transfers_while_stopped": len(stopped_ids),
            "states_while_stopped": undelivered,
            "seconds_to_deliver_after_restart": restart_seconds,
            "delivered_exactly_once": True,
        }

        # 2. Crash mid-traffic.
        first = transfer_batch(sender_api, from_account, to_account, args.transfers)
        run_on(args.region, outbox_fleet, "systemctl kill -s SIGKILL facaibank-outbox-worker")
        second = transfer_batch(sender_api, to_account, from_account, args.transfers)
        crash_seconds = wait_for_delivery(table, first + second, args.timeout)
        evidence["checks"]["crash_recovery"] = {
            "status": "PASS",
            "transfers_around_crash": len(first + second),
            "seconds_to_deliver_all": crash_seconds,
            "note": "systemd restarts the worker in 5 s; it resumes from the last checkpoint and the notifier drops any re-published event.",
        }

        # 3. Statement worker stopped while a job is queued.
        run_on(args.region, statement_fleet, "systemctl stop facaibank-statement-worker")
        job_id = sender_api.expect(202, "POST", "/statements/request", {"account_id": from_account})["job_id"]
        time.sleep(15)
        queued_status = sender_api.expect(200, "GET", f"/accounts/{from_account}/statements/{job_id}")["status"]
        run_on(args.region, statement_fleet, "systemctl start facaibank-statement-worker")
        started = time.monotonic()
        while time.monotonic() - started < args.timeout:
            statement = sender_api.expect(200, "GET", f"/accounts/{from_account}/statements/{job_id}")
            if statement["status"] == "COMPLETED":
                break
            time.sleep(5)
        else:
            raise AssertionError("Statement job did not complete after the worker restarted")
        evidence["checks"]["statement_worker_restart"] = {
            "status": "PASS",
            "status_while_stopped": queued_status,
            "seconds_to_complete_after_restart": round(time.monotonic() - started, 1),
            "download_url_issued": bool(statement.get("download_url")),
        }
        evidence["status"] = "PASS"
    except Exception as error:
        evidence["status"] = "FAIL"
        evidence["error"] = str(error)
        run_on(args.region, outbox_fleet, "systemctl start facaibank-outbox-worker")
        run_on(args.region, statement_fleet, "systemctl start facaibank-statement-worker")
    finally:
        sender.delete()
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    path = write_evidence("worker-recovery", evidence)
    print(f"{evidence['status']}: evidence saved to {path}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
