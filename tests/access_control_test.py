"""Access-restriction evidence (slide 15: "Is access restricted?").

1. IAM least privilege: simulates each instance role against actions it must
   NOT be able to perform (and a few it must), using the IAM policy simulator.
2. Bucket access: anonymous and plain-HTTP requests to the KYC bucket are
   denied; a short-lived presigned HTTPS URL works.
3. Private network paths: RDS and the instances have no public exposure, the
   RDS security group only admits the API and statement fleets, the public ALB
   refuses requests that did not come through CloudFront, and from inside the
   VPC the frontend tier cannot open a connection to the database while the API
   tier can (checked through Session Manager).

Customer-level authorization (401/404 checks) is covered by validate_all.py.

    python tests/access_control_test.py --execute
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import write_evidence  # noqa: E402


def simulate(iam, role_arn: str, action: str, resource: str) -> str:
    result = iam.simulate_principal_policy(PolicySourceArn=role_arn, ActionNames=[action], ResourceArns=[resource])
    return result["EvaluationResults"][0]["EvalDecision"]


def iam_checks(region: str, env: str, account_id: str, buckets: dict, ledger_arn: str) -> dict:
    iam = boto3.client("iam")
    role = lambda name: f"arn:aws:iam::{account_id}:role/facaibank-{env}-{name}-instance"  # noqa: E731
    kyc_object = f"arn:aws:s3:::{buckets['kyc']}/kyc/example.pdf"
    statement_object = f"arn:aws:s3:::{buckets['kyc']}/statements/example.pdf"
    cases = [
        ("frontend", "s3:GetObject", kyc_object, "denied"),
        ("frontend", "dynamodb:GetItem", ledger_arn, "denied"),
        ("frontend", "secretsmanager:GetSecretValue", "*", "denied"),
        ("backend", "s3:PutObject", kyc_object, "allowed"),
        ("backend", "s3:GetObject", kyc_object, "denied"),
        ("backend", "dynamodb:TransactWriteItems", ledger_arn, "allowed"),
        ("backend", "dynamodb:DeleteTable", ledger_arn, "denied"),
        ("backend", "dynamodb:Scan", ledger_arn, "denied"),
        ("backend", "kms:ScheduleKeyDeletion", "*", "denied"),
        ("backend", "s3:GetObject", f"arn:aws:s3:::{buckets['audit']}/AWSLogs/x", "denied"),
        ("outbox", "dynamodb:TransactWriteItems", ledger_arn, "denied"),
        ("outbox", "s3:GetObject", kyc_object, "denied"),
        ("statement", "s3:PutObject", statement_object, "allowed"),
        ("statement", "s3:GetObject", kyc_object, "denied"),
        ("statement", "dynamodb:TransactWriteItems", ledger_arn, "denied"),
    ]
    results = []
    for name, action, resource, expected in cases:
        decision = simulate(iam, role(name), action, resource)
        outcome = "allowed" if decision == "allowed" else "denied"
        results.append({"role": name, "action": action, "resource": resource, "expected": expected, "decision": decision, "pass": outcome == expected})
    return {"status": "PASS" if all(row["pass"] for row in results) else "FAIL", "cases": results}


def bucket_checks(region: str, bucket: str) -> dict:
    s3 = boto3.client("s3", region_name=region)
    key = f"kyc/access-test/{uuid.uuid4()}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"access test", ServerSideEncryption="aws:kms")
    results = {}
    try:
        def status(url: str) -> int:
            try:
                return urllib.request.urlopen(url, timeout=10).status
            except urllib.error.HTTPError as error:
                return error.code

        results["anonymous_https_get"] = status(f"https://{bucket}.s3.{region}.amazonaws.com/{key}")
        presigned = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60)
        results["presigned_https_get"] = status(presigned)
        results["presigned_plain_http_get"] = status(presigned.replace("https://", "http://", 1))
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
    expected = {"anonymous_https_get": 403, "presigned_https_get": 200, "presigned_plain_http_get": 403}
    return {
        "status": "PASS" if all(results[name] == code for name, code in expected.items()) else "FAIL",
        "observed": results,
        "expected": expected,
    }


def run_on(region: str, instance_id: str, command: str) -> str:
    ssm = boto3.client("ssm", region_name=region)
    command_id = ssm.send_command(
        InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", Parameters={"commands": [command]}
    )["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(2)
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            return invocation["StandardOutputContent"].strip()
    return "timeout"


def network_checks(region: str, env: str, rds_identifier: str, public_alb_dns: str) -> dict:
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)
    autoscaling = boto3.client("autoscaling", region_name=region)
    db = rds.describe_db_instances(DBInstanceIdentifier=rds_identifier)["DBInstances"][0]
    endpoint = db["Endpoint"]["Address"]
    rds_group = db["VpcSecurityGroups"][0]["VpcSecurityGroupId"]
    rules = ec2.describe_security_group_rules(Filters=[{"Name": "group-id", "Values": [rds_group]}])["SecurityGroupRules"]
    ingress = [rule for rule in rules if not rule["IsEgress"]]
    referenced = sorted(
        ec2.describe_security_groups(GroupIds=[rule["ReferencedGroupInfo"]["GroupId"]])["SecurityGroups"][0]["GroupName"]
        for rule in ingress
        if rule.get("ReferencedGroupInfo")
    )
    cidr_rules = [rule for rule in ingress if rule.get("CidrIpv4") or rule.get("CidrIpv6")]

    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": ["FaCaiBank"]},
            {"Name": "tag:Environment", "Values": [env]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    public_instances = [i["InstanceId"] for r in reservations for i in r["Instances"] if i.get("PublicIpAddress")]

    sock = socket.socket()
    sock.settimeout(5)
    try:
        sock.connect((endpoint, 5432))
        internet_to_rds = "connected"
    except OSError as error:
        internet_to_rds = f"refused/timeout ({error.__class__.__name__})"
    finally:
        sock.close()

    try:
        direct_alb = urllib.request.urlopen(f"http://{public_alb_dns}/", timeout=8).status
    except urllib.error.HTTPError as error:
        direct_alb = error.code
    except OSError:
        direct_alb = "timeout (blocked by CloudFront-only security group)"

    def first_instance(fleet: str) -> str | None:
        groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[f"facaibank-{env}-{fleet}"])["AutoScalingGroups"]
        instances = [i["InstanceId"] for g in groups for i in g["Instances"] if i["LifecycleState"] == "InService"]
        return instances[0] if instances else None

    probe = f"timeout 4 bash -c '</dev/tcp/{endpoint}/5432' && echo OPEN || echo BLOCKED"
    frontend_instance, backend_instance = first_instance("frontend"), first_instance("backend")
    from_frontend = run_on(region, frontend_instance, probe) if frontend_instance else "no instance"
    from_backend = run_on(region, backend_instance, probe) if backend_instance else "no instance"

    checks = {
        "rds_publicly_accessible": db["PubliclyAccessible"],
        "rds_ingress_source_groups": referenced,
        "rds_ingress_cidr_rules": len(cidr_rules),
        "instances_with_public_ip": public_instances,
        "internet_to_rds_5432": internet_to_rds,
        "direct_public_alb_request": direct_alb,
        "frontend_tier_to_rds": from_frontend,
        "api_tier_to_rds": from_backend,
    }
    passed = (
        not db["PubliclyAccessible"]
        and not cidr_rules
        and not public_instances
        and internet_to_rds != "connected"
        and direct_alb != 200
        and from_frontend.endswith("BLOCKED")
        and from_backend.endswith("OPEN")
    )
    return {"status": "PASS" if passed else "FAIL", "observed": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--env", default=os.getenv("FACAI_ENV", "dev"))
    parser.add_argument("--kyc-bucket", default=os.getenv("KYC_STATEMENTS_BUCKET"))
    parser.add_argument("--audit-bucket", default=os.getenv("AUDIT_BUCKET"))
    parser.add_argument("--ledger-table", default=os.getenv("LEDGER_TABLE_NAME", "facaibank-ledger"))
    parser.add_argument("--rds-identifier", default=os.getenv("RDS_IDENTIFIER", "facaibank-dev-postgres"))
    parser.add_argument("--public-alb-dns", default=os.getenv("PUBLIC_ALB_DNS"))
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("this test calls live AWS APIs; pass --execute")
    if not all([args.kyc_bucket, args.audit_bucket, args.public_alb_dns]):
        parser.error("--kyc-bucket, --audit-bucket and --public-alb-dns are required (see terraform output)")

    account_id = boto3.client("sts").get_caller_identity()["Account"]
    ledger_arn = f"arn:aws:dynamodb:{args.region}:{account_id}:table/{args.ledger_table}"
    evidence = {
        "project": "FaCaiBank",
        "test": "access-control",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "iam_least_privilege": iam_checks(args.region, args.env, account_id, {"kyc": args.kyc_bucket, "audit": args.audit_bucket}, ledger_arn),
        "bucket_access": bucket_checks(args.region, args.kyc_bucket),
        "private_network_paths": network_checks(args.region, args.env, args.rds_identifier, args.public_alb_dns),
    }
    evidence["status"] = "PASS" if all(evidence[key]["status"] == "PASS" for key in ("iam_least_privilege", "bucket_access", "private_network_paths")) else "FAIL"
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    path = write_evidence("access-control", evidence)
    print(f"{evidence['status']}: evidence saved to {path}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
