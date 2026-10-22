"""Load test: throughput, p95 latency, error rate and scaling observations.

Simulates the slide-5 workload mix (10 reads per write) with disposable,
funded customers. Each virtual user reads its balance and history and moves
small amounts between its own two accounts, so balances never run out and no
step-up is triggered. While the test runs, the Auto Scaling groups are sampled
to show the API and worker fleets scaling independently.

    python tests/load_test.py --execute --rps 40 --duration 600 --users 8

WAF limits each client IP to waf_rate_limit_per_5min requests. Before a
single-machine load test, raise that variable (for example to 100000), apply,
and set it back afterwards.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import EVIDENCE_DIR, provision_customer, write_evidence  # noqa: E402

TARGETS_MS = {"balance": 200, "transfer": 1000}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))  # nearest-rank method
    return round(ordered[max(0, min(len(ordered), rank) - 1)], 1)


def summarise(samples: dict[str, list[tuple[float, int]]], duration: float) -> dict:
    endpoints = {}
    total = errors = 0
    for name, results in samples.items():
        latencies = [millis for millis, _ in results]
        failed = sum(1 for _, status in results if status >= 500 or status == 0)
        rejected = sum(1 for _, status in results if 400 <= status < 500)
        total += len(results)
        errors += failed
        endpoints[name] = {
            "requests": len(results),
            "server_errors": failed,
            "client_errors": rejected,
            "p50_ms": percentile(latencies, 50),
            "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99),
            "mean_ms": round(statistics.fmean(latencies), 1) if latencies else None,
            "target_p95_ms": TARGETS_MS.get(name),
        }
        if name in TARGETS_MS and endpoints[name]["p95_ms"] is not None:
            endpoints[name]["meets_target"] = endpoints[name]["p95_ms"] <= TARGETS_MS[name]
    return {
        "total_requests": total,
        "throughput_rps": round(total / duration, 2) if duration else 0,
        "error_rate_percent": round(100 * errors / total, 3) if total else 0,
        "endpoints": endpoints,
    }


class ScalingSampler(threading.Thread):
    def __init__(self, region: str, asg_names: list[str], interval: float = 15):
        super().__init__(daemon=True)
        self.client = boto3.client("autoscaling", region_name=region)
        self.asg_names = asg_names
        self.interval = interval
        self.samples: list[dict] = []
        self.stopped = threading.Event()

    def sample(self) -> None:
        groups = self.client.describe_auto_scaling_groups(AutoScalingGroupNames=self.asg_names)["AutoScalingGroups"]
        self.samples.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "groups": {
                    group["AutoScalingGroupName"]: {
                        "desired": group["DesiredCapacity"],
                        "in_service": sum(1 for i in group["Instances"] if i["LifecycleState"] == "InService"),
                    }
                    for group in groups
                },
            }
        )

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.sample()
            except Exception as error:  # sampling must never stop the load test
                print(f"scaling sample failed: {error}", file=sys.stderr)
            self.stopped.wait(self.interval)

    def report(self) -> dict:
        report = {}
        for name in self.asg_names:
            desired = [sample["groups"].get(name, {}).get("desired", 0) for sample in self.samples]
            if desired:
                report[name] = {"start": desired[0], "peak": max(desired), "end": desired[-1]}
        return report


def markdown_report(evidence: dict) -> str:
    result = evidence["results"]
    lines = [
        f"# Load test {evidence['run_id']}",
        "",
        f"- Target rate: {evidence['config']['rps']} req/s for {evidence['config']['duration_seconds']} s "
        f"with {evidence['config']['users']} customers (10 reads : 1 write)",
        f"- Achieved throughput: **{result['throughput_rps']} req/s**, {result['total_requests']} requests",
        f"- Server error rate: **{result['error_rate_percent']}%**",
        "",
        "| Endpoint | Requests | p50 ms | p95 ms | p99 ms | Target p95 | Met |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, row in sorted(result["endpoints"].items()):
        met = {True: "yes", False: "**no**"}.get(row.get("meets_target"), "")
        lines.append(
            f"| {name} | {row['requests']} | {row['p50_ms']} | {row['p95_ms']} | {row['p99_ms']} | "
            f"{row['target_p95_ms'] or ''} | {met} |"
        )
    lines += ["", "| Auto Scaling group | Start | Peak | End |", "|---|---:|---:|---:|"]
    for name, row in evidence["scaling"].items():
        lines.append(f"| {name} | {row['start']} | {row['peak']} | {row['end']} |")
    lines += ["", "Latencies are measured at the client, through CloudFront, WAF and both ALBs."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--api-url", default=os.getenv("FACAI_API_URL"))
    parser.add_argument("--user-pool-id", default=os.getenv("FACAI_USER_POOL_ID"))
    parser.add_argument("--client-id", default=os.getenv("FACAI_CLIENT_ID"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--env", default=os.getenv("FACAI_ENV", "dev"))
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=64)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("the load test calls a live stack; pass --execute")
    if not all([args.api_url, args.user_pool_id, args.client_id]):
        parser.error("--api-url, --user-pool-id and --client-id are required")

    print(f"Provisioning {args.users} funded test customers…")
    customers = [
        provision_customer(args.api_url, args.region, args.user_pool_id, args.client_id, f"load{index}", accounts=2, deposit="1000.00")
        for index in range(args.users)
    ]
    asg_names = [f"facaibank-{args.env}-{name}" for name in ("frontend", "backend", "outbox-workers", "statement-workers")]
    sampler = ScalingSampler(args.region, asg_names)
    sampler.sample()
    sampler.start()

    samples: dict[str, list[tuple[float, int]]] = defaultdict(list)
    lock = threading.Lock()

    def one_request() -> None:
        _customer, api, (first, second) = random.choice(customers)
        roll = random.random()
        if roll < 1 / 11:
            name = "transfer"
            sender, receiver = random.choice([(first, second), (second, first)])
            call = lambda: api.request(  # noqa: E731
                "POST",
                "/transfers",
                {"sender_account_id": sender, "receiver_account_id": receiver, "amount": "1.00"},
                headers={"Idempotency-Key": f"load-{uuid.uuid4()}"},
            )
        elif roll < 0.75:
            name = "balance"
            call = lambda: api.request("GET", f"/accounts/{random.choice((first, second))}/balance")  # noqa: E731
        else:
            name = "transactions"
            call = lambda: api.request("GET", f"/accounts/{first}/transactions?limit=20")  # noqa: E731
        started = time.perf_counter()
        try:
            status = call()[0]
        except Exception:
            status = 0
        with lock:
            samples[name].append(((time.perf_counter() - started) * 1000, status))

    started_at = datetime.now(timezone.utc)
    begin = time.monotonic()
    interval = 1 / args.rps
    next_at = begin
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while time.monotonic() - begin < args.duration:
            pool.submit(one_request)
            next_at += interval
            time.sleep(max(0, next_at - time.monotonic()))
    elapsed = time.monotonic() - begin
    sampler.stopped.set()
    sampler.join(timeout=30)
    sampler.sample()

    for customer, _api, _accounts in customers:
        customer.delete()

    evidence = {
        "project": "FaCaiBank",
        "test": "load",
        "run_id": uuid.uuid4().hex[:12],
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "api_url": args.api_url,
            "rps": args.rps,
            "duration_seconds": args.duration,
            "users": args.users,
            "mix": "~68% balance, ~23% history, ~9% transfers (10:1 read:write)",
        },
        "results": summarise(samples, elapsed),
        "scaling": sampler.report(),
        "scaling_samples": sampler.samples,
    }
    path = write_evidence("load-test", evidence)
    report = markdown_report(evidence)
    path.with_suffix(".md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Evidence saved to {path} and {path.with_suffix('.md')}")
    return 0 if evidence["results"]["error_rate_percent"] < 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
