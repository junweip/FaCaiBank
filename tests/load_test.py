"""Load test: throughput, p95 latency, error rate and scaling observations.

Simulates the slide-5 workload mix (10 reads per write) with disposable,
funded customers. Each virtual user reads its balance and history and moves
small amounts between its own two accounts, so balances never run out. While
the test runs, the Auto Scaling groups are sampled to show the API and worker
fleets scaling independently.

Each customer signs in again (password + TOTP) every --reauth seconds. After ten
transfers in an hour the fraud rules ask for step-up, which needs a sign-in less
than five minutes old; a real customer would be prompted, and without the
re-sign-in every busy account is refused a few minutes into the run.

--cooldown keeps a trickle of traffic going after the main load so scale-in is
captured. Target tracking waits about 15 minutes below target before removing
instances, and an idle ALB stops reporting request counts, so do not stop at zero.

    python tests/load_test.py --execute --rps 40 --duration 600 --users 8
    python tests/load_test.py --execute --rps 90 --duration 600 --users 16 --cooldown 1500

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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import EVIDENCE_DIR, provision_customer, write_evidence  # noqa: E402

TARGETS_MS = {"balance": 200, "transfer": 1000}
REAUTH_SECONDS = 240  # below the API's 300 s step-up window (STEP_UP_MAX_AGE_SECONDS)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))  # nearest-rank method
    return round(ordered[max(0, min(len(ordered), rank) - 1)], 1)


def rejection_code(status: int, payload: dict) -> str | None:
    """The API's error code for a 4xx (for example step_up_required), else None."""
    if not 400 <= status < 500:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"])
    return f"http_{status}"


def summarise(samples: dict[str, list[tuple[float, int, str | None]]], duration: float) -> dict:
    endpoints = {}
    total = errors = 0
    for name, results in samples.items():
        latencies = [millis for millis, _, _ in results]
        failed = sum(1 for _, status, _ in results if status >= 500 or status == 0)
        reasons = Counter(code for _, _, code in results if code)
        rejected = sum(reasons.values())
        total += len(results)
        errors += failed
        endpoints[name] = {
            "requests": len(results),
            "server_errors": failed,
            "client_errors": rejected,
            "client_error_codes": dict(reasons),
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
        self.load_end_index: int | None = None
        self.stopped = threading.Event()

    def mark_load_end(self) -> None:
        self.sample()
        self.load_end_index = len(self.samples) - 1

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
            if not desired:
                continue
            in_service = [sample["groups"].get(name, {}).get("in_service", 0) for sample in self.samples]
            row = {"start": desired[0], "peak": max(desired), "peak_in_service": max(in_service), "end": desired[-1]}
            if self.load_end_index is not None:
                at_end = desired[self.load_end_index]
                row["load_end"] = at_end
                later = [
                    sample for sample in self.samples[self.load_end_index :]
                    if sample["groups"].get(name, {}).get("desired", at_end) < at_end
                ]
                if later:
                    ended = datetime.fromisoformat(self.samples[self.load_end_index]["at"])
                    row["scale_in_after_seconds"] = round((datetime.fromisoformat(later[0]["at"]) - ended).total_seconds())
            report[name] = row
        return report

    def activities(self, since: datetime) -> list[dict]:
        """Auto Scaling's own launch/terminate history for the run, oldest first."""
        events = []
        for name in self.asg_names:
            pages = self.client.get_paginator("describe_scaling_activities").paginate(AutoScalingGroupName=name)
            for page in pages:
                for activity in page["Activities"]:
                    if activity["StartTime"] < since:
                        continue
                    events.append(
                        {
                            "group": name,
                            "at": activity["StartTime"].isoformat(),
                            "status": activity["StatusCode"],
                            "description": activity["Description"],
                            "cause": activity.get("Cause", "")[:400],
                        }
                    )
        return sorted(events, key=lambda event: event["at"])


class SessionRefresher(threading.Thread):
    """Signs every load customer in again on a schedule so step-up stays satisfied."""

    def __init__(self, customers: list, interval: float = REAUTH_SECONDS):
        super().__init__(daemon=True)
        self.customers = customers
        self.interval = interval
        self.sign_ins = 0
        self.failures: list[str] = []
        self.stopped = threading.Event()

    def refresh_all(self) -> None:
        for customer, api, _accounts in self.customers:
            try:
                api.token = customer.sign_in()
                self.sign_ins += 1
            except Exception as error:  # a failed sign-in shows up as 401s, not a crash
                self.failures.append(f"{type(error).__name__}: {error}"[:200])

    def run(self) -> None:
        while not self.stopped.wait(self.interval):
            self.refresh_all()

    def report(self) -> dict:
        return {
            "interval_seconds": self.interval,
            "sign_ins": self.sign_ins,
            "failed_sign_ins": len(self.failures),
            "failure_samples": self.failures[:5],
        }


def markdown_report(evidence: dict) -> str:
    result = evidence["results"]
    lines = [
        f"# Load test {evidence['run_id']}",
        "",
        f"- Target rate: {evidence['config']['rps']} req/s for {evidence['config']['duration_seconds']} s "
        f"with {evidence['config']['users']} customers (10 reads : 1 write)",
        f"- Achieved throughput: **{result['throughput_rps']} req/s**, {result['total_requests']} requests",
        f"- Server error rate: **{result['error_rate_percent']}%**",
    ]
    rejections = {
        f"{name}: {code}": count
        for name, row in sorted(result["endpoints"].items())
        for code, count in sorted(row.get("client_error_codes", {}).items())
    }
    lines.append(
        "- Client rejections: " + (", ".join(f"{key} × {count}" for key, count in rejections.items()) or "none")
    )
    if "sessions" in evidence:
        sessions = evidence["sessions"]
        lines.append(
            f"- Customers re-signed in every {sessions['interval_seconds']} s: "
            f"{sessions['sign_ins']} sign-ins, {sessions['failed_sign_ins']} failed"
        )
    lines += [
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
    cooldown = evidence["config"].get("cooldown_seconds")
    if cooldown:
        lines += [
            "",
            f"After the main load, {evidence['config']['cooldown_rps']} req/s for {cooldown} s to observe scale-in.",
            "",
            "| Auto Scaling group | Start | Peak desired | Peak in service | Load end | End | Scale-in after load end |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, row in evidence["scaling"].items():
            after = row.get("scale_in_after_seconds")
            lines.append(
                f"| {name} | {row['start']} | {row['peak']} | {row.get('peak_in_service', '')} | "
                f"{row.get('load_end', '')} | {row['end']} | "
                f"{f'{after} s' if after is not None else ''} |"
            )
    else:
        lines += ["", "| Auto Scaling group | Start | Peak | End |", "|---|---:|---:|---:|"]
        for name, row in evidence["scaling"].items():
            lines.append(f"| {name} | {row['start']} | {row['peak']} | {row['end']} |")
    activities = evidence.get("scaling_activities", [])
    if activities:
        lines += ["", "Scaling activity (from Auto Scaling, including failed launches):", ""]
        for event in activities:
            status = "" if event["status"] == "Successful" else f"**{event['status'].upper()}** "
            description = " ".join(event["description"].split())[:240]
            lines.append(f"- {event['at'][11:19]} UTC {event['group']}: {status}{description}")
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
    parser.add_argument("--reauth", type=float, default=REAUTH_SECONDS, help="Seconds between fresh sign-ins")
    parser.add_argument("--cooldown", type=int, default=0, help="Seconds of trickle traffic after the load")
    parser.add_argument("--cooldown-rps", type=float, default=1)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("the load test calls a live stack; pass --execute")
    if not all([args.api_url, args.user_pool_id, args.client_id]):
        parser.error("--api-url, --user-pool-id and --client-id are required")

    print(f"Provisioning {args.users} funded test customers…")
    customers: list = []
    try:
        for index in range(args.users):
            customers.append(
                provision_customer(
                    args.api_url, args.region, args.user_pool_id, args.client_id, f"load{index}", accounts=2, deposit="1000.00"
                )
            )
        return run_load(args, customers)
    finally:
        for customer, _api, _accounts in customers:
            customer.delete()


def make_request(customers: list, samples: dict, lock: threading.Lock) -> None:
    """One request from a random customer in the 10:1 read:write mix."""
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
        status, payload, _headers = call()
    except Exception:
        status, payload = 0, {}
    with lock:
        samples[name].append(((time.perf_counter() - started) * 1000, status, rejection_code(status, payload)))


def drive(customers: list, rps: float, duration: float, concurrency: int) -> tuple[dict, float]:
    """Send requests at a fixed rate for duration seconds; returns (samples, elapsed)."""
    samples: dict[str, list[tuple[float, int, str | None]]] = defaultdict(list)
    lock = threading.Lock()
    begin = time.monotonic()
    interval = 1 / rps
    next_at = begin
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while time.monotonic() - begin < duration:
            pool.submit(make_request, customers, samples, lock)
            next_at += interval
            time.sleep(max(0, next_at - time.monotonic()))
    return samples, time.monotonic() - begin


def run_load(args, customers: list) -> int:
    asg_names = [f"facaibank-{args.env}-{name}" for name in ("frontend", "backend", "outbox-workers", "statement-workers")]
    sampler = ScalingSampler(args.region, asg_names)
    sessions = SessionRefresher(customers, args.reauth)
    print("Signing every customer in afresh…")
    sessions.refresh_all()
    sessions.start()
    sampler.sample()
    sampler.start()

    started_at = datetime.now(timezone.utc)
    print(f"Load: {args.rps} req/s for {args.duration} s")
    samples, elapsed = drive(customers, args.rps, args.duration, args.concurrency)
    cooldown = None
    if args.cooldown:
        sampler.mark_load_end()
        print(f"Cooldown: {args.cooldown_rps} req/s for {args.cooldown} s, watching for scale-in")
        cooldown_samples, cooldown_elapsed = drive(customers, args.cooldown_rps, args.cooldown, args.concurrency)
        cooldown = summarise(cooldown_samples, cooldown_elapsed)
    sessions.stopped.set()
    sampler.stopped.set()
    sampler.join(timeout=30)
    sampler.sample()

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
            "reauth_seconds": args.reauth,
            "cooldown_seconds": args.cooldown,
            "cooldown_rps": args.cooldown_rps,
        },
        "results": summarise(samples, elapsed),
        "cooldown_results": cooldown,
        "sessions": sessions.report(),
        "scaling": sampler.report(),
        "scaling_samples": sampler.samples,
    }
    try:
        evidence["scaling_activities"] = sampler.activities(started_at)
    except Exception as error:  # evidence is still useful without the activity history
        print(f"could not read scaling activities: {error}", file=sys.stderr)
    path = write_evidence("load-test", evidence)
    report = markdown_report(evidence)
    path.with_suffix(".md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Evidence saved to {path} and {path.with_suffix('.md')}")
    return 0 if evidence["results"]["error_rate_percent"] < 1 else 1

if __name__ == "__main__":
    raise SystemExit(main())
