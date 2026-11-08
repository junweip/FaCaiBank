# Validation and demonstration (slide 15)

Two layers of evidence:

1. **Unit tests** (`python -m pytest -q`, about 80 tests, no AWS needed). DynamoDB, S3, SQS and SNS are emulated with moto, so the real transaction conditions run: ownership, insufficient funds, idempotent replay, withdrawal reversal and the zero-sum ledger invariant. CI runs them on every push.
2. **Live scripts** in `tests/`, run against a deployed non-production stack. Each writes timestamped JSON (and Markdown for the load test) into `tests/evidence/`.

The load test's error rate counts every non-2xx response, including 4xx responses and
transport failures. The checked-in 2026-11-01 load-test evidence predates that correction:
it records 1,086 4xx responses among 2,191 transfer requests (49.6%) while reporting 0%
server errors. Treat that run as inconclusive and rerun before quoting it. It was rerun on 2026-11-08 on the full profile with customers re-signing in
(`tests/evidence/load-test-20261108T165132Z.md`): 54,000 requests at 90 req/s, 0 non-2xx responses. The 40-request/s
test is a prototype demonstration, not proof of capacity for the specification's 13-million-
user launch target.

## Setup for the live scripts

```bash
python -m pip install -r app/backend_api/requirements-dev.txt
cd infra
export FACAI_API_URL=$(terraform output -raw api_base_url)
export FACAI_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id)
export FACAI_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id)
export EVENTS_QUEUE_URL=$(terraform output -raw events_queue_url)
export EVENTS_DLQ_URL=$(terraform output -raw events_dlq_url)
export LAMBDA_LOG_GROUP=$(terraform output -raw notifier_log_group)
export KYC_STATEMENTS_BUCKET=$(terraform output -raw kyc_statements_bucket_name)
export AUDIT_BUCKET=$(terraform output -raw audit_bucket_name)
export PUBLIC_ALB_DNS=$(terraform output -raw public_alb_dns_name)
cd ..
```

The scripts create disposable Cognito customers (MFA enrolled automatically) and delete
them afterwards. Your AWS credentials need Cognito admin, DynamoDB read, SQS, CloudWatch,
SSM `SendCommand`, `iam:SimulatePrincipalPolicy`, and RDS/DynamoDB restore permissions.

## Question → evidence

| Slide-15 question | Command | What the evidence shows |
|---|---|---|
| Are transfers correct? | `python tests/validate_all.py --execute` | **A:** 5 concurrent transfers produce exactly 5 debit and 5 credit legs, and the combined balance is unchanged. **B:** a repeated request returns the original result with `Idempotent-Replayed: true` and debits once; the same key with a different amount is rejected (409). Rollback is shown by the unit tests (insufficient funds, declined withdrawal reversal). |
| Do background jobs recover? | `validate_all.py` (test C) and `python tests/recovery_test.py --execute` | Duplicate events are skipped; a poison message is retried 3 times, lands in the DLQ and fires the alarm. The outbox fleet is stopped and then killed mid-traffic, and every notification is still delivered exactly once; the evidence includes time to recover. A statement queued while its worker is down completes after restart. |
| Can the prototype scale under a demo load? | `python tests/load_test.py --execute --rps 90 --duration 600 --users 16 --cooldown 1500` | Throughput, non-2xx response rate (4xx broken down by reason; customers re-sign in every 4 minutes so step-up is satisfied) and p50/p95/p99 per endpoint against the 200 ms and 1 s targets; each Auto Scaling group's start, peak desired and in-service, load-end and end capacity, how long scale-in took after the load fell, and Auto Scaling's activity history including failed launches. Raise `waf_rate_limit_per_5min` first (single-IP test). This does not establish launch-scale capacity. |
| Can operations detect and recover? | `validate_all.py` (test C), `python tests/restore_test.py --execute [--include-rds]`, `python tests/failover_test.py --execute rds` and `--execute az` (full profile) | The DLQ alarm firing; a timed DynamoDB PITR restore matched item for item, with the zero-sum check (and optionally RDS); an RDS Multi-AZ failover with the outage window of an uncached database read, the cache-backed profile and a ledger endpoint measured separately; every instance in one AZ terminated, with the site's availability while Auto Scaling replaces them. |
| Is access restricted? | `validate_all.py` (test D) and `python tests/access_control_test.py --execute` | **D:** no token, another customer's token, a forged signature and a revoked (logged-out) token are all refused. **Access test:** IAM simulation shows each instance role denied outside its job; the KYC bucket refuses anonymous and plain-HTTP access; RDS and the instances have no public exposure; the ALB refuses requests that bypass CloudFront; from inside the VPC the frontend tier cannot reach the database while the API tier can. |

## Screenshots worth capturing for the report

- The CloudWatch dashboard during the load test (latency against targets, instances per fleet rising).
- Auto Scaling activity history showing the scale-out and scale-in.
- The DLQ alarm in ALARM, then OK after cleanup.
- A Security Hub summary and a GuardDuty sample finding (GuardDuty > Settings > *Generate sample findings*).
- CloudTrail log file validation (`aws cloudtrail validate-logs`) and the audit bucket's Object Lock settings.
- The sign-in flow with MFA, a step-up prompt on a SGD 1,500 transfer, and a downloaded PDF statement.
