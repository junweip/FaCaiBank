"""Availability-zone recovery exercises (slide 4: RPO 0, RTO within 10 minutes).

rds   : forces an RDS Multi-AZ failover (reboot with failover) while probing
        every second: an uncached profile read on the primary (POST /me for an
        existing customer), the profile as customers read it (GET /me, served
        from ElastiCache when enabled) and a ledger endpoint (balance). Whether
        and how long it failed over comes from RDS events, because the
        instance's AvailabilityZone field can lag the failover by many minutes.
        Requires rds_multi_az = true.
az    : terminates every frontend, API and worker instance in one AZ (simulating
        the AZ going dark) and measures how long the site keeps answering while
        Auto Scaling replaces capacity in the other AZs. Requires asg_min_size
        >= 2 to avoid a full outage.

    python tests/failover_test.py --execute rds
    python tests/failover_test.py --execute az --zone ap-southeast-1a
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import provision_customer, write_evidence  # noqa: E402

RTO_SECONDS = 600


class Prober(threading.Thread):
    """Calls an endpoint every second and records outage windows."""

    def __init__(self, name: str, call):
        super().__init__(daemon=True)
        self.name = name
        self.call = call
        self.results: list[tuple[float, bool]] = []
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                ok = self.call() == 200
            except Exception:
                ok = False
            self.results.append((time.time(), ok))
            self.stopped.wait(1)

    def report(self) -> dict:
        outages, current = [], None
        for timestamp, ok in self.results:
            if not ok and current is None:
                current = timestamp
            elif ok and current is not None:
                outages.append(timestamp - current)
                current = None
        if current is not None:
            outages.append(self.results[-1][0] - current)
        failures = sum(1 for _, ok in self.results if not ok)
        return {
            "probes": len(self.results),
            "failed_probes": failures,
            "availability_percent": round(100 * (1 - failures / len(self.results)), 3) if self.results else None,
            "longest_outage_seconds": round(max(outages), 1) if outages else 0,
            "within_rto": (max(outages) if outages else 0) <= RTO_SECONDS,
        }


def failover_timeline(events: list[dict]) -> dict:
    """Failover start/completion from RDS events (oldest first)."""
    started = next((e["Date"] for e in events if "failover started" in e["Message"]), None)
    completed = next((e["Date"] for e in events if "failover completed" in e["Message"]), None)
    return {
        "failed_over": completed is not None,
        "failover_started_at": started.isoformat() if started else None,
        "failover_completed_at": completed.isoformat() if completed else None,
        "failover_seconds": round((completed - started).total_seconds(), 1) if started and completed else None,
        "rds_events": [{"at": e["Date"].isoformat(), "message": e["Message"]} for e in events],
    }


def rds_failover(args, api, account_id: str) -> dict:
    rds = boto3.client("rds", region_name=args.region)
    instance = rds.describe_db_instances(DBInstanceIdentifier=args.rds_identifier)["DBInstances"][0]
    if not instance["MultiAZ"]:
        raise SystemExit("RDS is not Multi-AZ; apply with rds_multi_az = true first")
    before_az = instance["AvailabilityZone"]
    profile = {"full_name": "Live Test Failover"}
    probes = [
        Prober("accounts-db (uncached POST /me)", lambda: api.request("POST", "/me", profile)[0]),
        Prober("profile (GET /me, cache-backed)", lambda: api.request("GET", "/me")[0]),
        Prober("ledger (balance)", lambda: api.request("GET", f"/accounts/{account_id}/balance")[0]),
    ]
    for probe in probes:
        probe.start()
    time.sleep(15)
    started = time.monotonic()
    requested_at = datetime.now(timezone.utc)
    rds.reboot_db_instance(DBInstanceIdentifier=args.rds_identifier, ForceFailover=True)
    time.sleep(30)
    rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=args.rds_identifier)
    failover_seconds = round(time.monotonic() - started, 1)
    time.sleep(60)
    for probe in probes:
        probe.stopped.set()
        probe.join()
    after_az = rds.describe_db_instances(DBInstanceIdentifier=args.rds_identifier)["DBInstances"][0]["AvailabilityZone"]
    events = rds.describe_events(
        SourceIdentifier=args.rds_identifier, SourceType="db-instance", StartTime=requested_at
    )["Events"]
    return {
        "primary_az_before": before_az,
        "primary_az_reported_after": after_az,
        **failover_timeline(sorted(events, key=lambda event: event["Date"])),
        "seconds_until_available": failover_seconds,
        "probes": {probe.name: probe.report() for probe in probes},
        "expectation": "Ledger reads (DynamoDB, 3 AZs) stay available; account metadata recovers within the RTO.",
    }


def az_outage(args, api, account_id: str) -> dict:
    ec2 = boto3.client("ec2", region_name=args.region)
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": ["FaCaiBank"]},
            {"Name": "tag:Environment", "Values": [args.env]},
            {"Name": "availability-zone", "Values": [args.zone]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    victims = [instance["InstanceId"] for reservation in reservations for instance in reservation["Instances"]]
    if not victims:
        raise SystemExit(f"No running FaCaiBank instances in {args.zone}")
    probe = Prober("balance via CloudFront", lambda: api.request("GET", f"/accounts/{account_id}/balance")[0])
    probe.start()
    time.sleep(15)
    ec2.terminate_instances(InstanceIds=victims)
    time.sleep(args.observe)
    probe.stopped.set()
    probe.join()
    return {"zone": args.zone, "terminated_instances": victims, "observed_seconds": args.observe, "probe": probe.report()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=["rds", "az"])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--api-url", default=os.getenv("FACAI_API_URL"))
    parser.add_argument("--user-pool-id", default=os.getenv("FACAI_USER_POOL_ID"))
    parser.add_argument("--client-id", default=os.getenv("FACAI_CLIENT_ID"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--env", default=os.getenv("FACAI_ENV", "dev"))
    parser.add_argument("--rds-identifier", default=os.getenv("RDS_IDENTIFIER", "facaibank-dev-postgres"))
    parser.add_argument("--zone", default="ap-southeast-1a")
    parser.add_argument("--observe", type=int, default=420, help="Seconds to observe after an AZ outage")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("this exercise disrupts a live environment; pass --execute")

    customer, api, (account_id,) = provision_customer(
        args.api_url, args.region, args.user_pool_id, args.client_id, "failover", deposit="10.00"
    )
    evidence = {"project": "FaCaiBank", "test": f"failover-{args.scenario}", "started_at": datetime.now(timezone.utc).isoformat(), "rto_target_seconds": RTO_SECONDS}
    try:
        evidence["result"] = rds_failover(args, api, account_id) if args.scenario == "rds" else az_outage(args, api, account_id)
    finally:
        customer.delete()
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    path = write_evidence(f"failover-{args.scenario}", evidence)
    print(f"Evidence saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
