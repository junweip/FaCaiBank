# Full-profile test day, 2026-11-08 (UTC)

Failover, AZ-outage and load/scale-in runs on the full profile. Times are UTC.

## Environment

- Applied `infra/full.tfvars` with Terraform 1.13.4, overriding:
  - `waf_rate_limit_per_5min=100000` (single-machine load test; the default is 2000)
  - `enable_rds_read_replica=false`. The replica cannot be created: RDS for PostgreSQL
    refuses read replicas when the primary uses a Secrets Manager-managed master password
    (`manage_master_user_password = true`). No application path reads from it.
- Result: 3 × t3.small per fleet (one per AZ) on min 3 / max 6, RDS db.t4g.small Multi-AZ,
  ElastiCache Valkey, interface endpoints, 3 NAT gateways, SageMaker fraud endpoint,
  GuardDuty, Security Hub, Macie.
- Client: one laptop in Singapore, Python 3.13, `SSL_CERT_FILE` set to certifi's bundle.

```bash
cd infra
terraform apply -var-file=full.tfvars -var waf_rate_limit_per_5min=100000 -var enable_rds_read_replica=false
export FACAI_API_URL=$(terraform output -raw api_base_url)
export FACAI_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id)
export FACAI_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id)
cd ..
python tests/failover_test.py rds --execute
python tests/failover_test.py az --execute --zone ap-southeast-1a
python tests/load_test.py --execute --rps 90 --duration 600 --users 16 --cooldown 1500
python tests/validate_all.py --execute
python tests/capture_dashboard.py --label <test> --start <window start> --end <window end>
```

## Results

| Test | Window | Result | Evidence |
|---|---|---|---|
| RDS Multi-AZ failover | 16:01:45–16:04:37 | Failover 16:02:19 → 16:02:54 (**34.9 s**). Uncached DB read (POST /me): **0 failed** of 133 probes. Cached profile (GET /me): 0 of 154. Ledger balance: 1 of 152 (one 1.1 s gap). | `failover-rds-20261108T160436Z.json`, `figures/failover-rds-20261108T155500Z/` |
| AZ outage (ap-southeast-1a) | 16:04:58–16:12:19 | All 4 instances in the AZ terminated (one per fleet). Balance via CloudFront: **98.9%** of 379 probes, longest gap **2.4 s**; every fleet back to 3 in service. | `failover-az-20261108T161218Z.json`, `figures/failover-az-20261108T160000Z/` |
| Load + scale-in | 16:15:22–16:51:35 | 54,000 requests at **89.98 req/s**, **0% server errors, 0 client rejections**. API and frontend scaled 3 → 6 (16:21–16:23) and back to 3 about 17 min after the load fell (16:43–16:47). | `load-test-20261108T165132Z.{json,md}`, `figures/load-test-20261108T161000Z/` (incl. `backend-scaling.json`, `frontend-scaling.json`) |

| Correctness + DLQ alarm (`validate_all.py`) | 16:57:24–17:09:23 | A concurrent ACID, B idempotent replay, C SQS dedup + DLQ, D authorization: **all PASS**. The poison message reached the DLQ at ~17:06 and `facaibank-dev-events-dlq-visible` went **OK → ALARM at 17:09:21**. | `validation-20261108T170923Z.json`, `figures/validation-dlq-alarm-20261108T165500Z/alarm-facaibank-dev-events-dlq-visible.png` |

### Latency: client side vs server side

Client-measured p95 (laptop → CloudFront → WAF → public ALB → frontend → internal ALB → API):
balance **319 ms** (target 200 ms, **missed**), transfer **406 ms** (target 1 s, met).
The API's own p95 on the dashboard (`figures/load-test-20261108T161000Z/01-api-latency-p95-...png`)
stays at roughly 10–20 ms for balance and 100–200 ms for transfers for the whole run, so the
gap is the path from one client machine through the edge, not the service. Report both.

## What changed because of these runs

1. **Bug found and fixed: stale database connections after failover.** The first RDS failover
   (15:49:59–15:50:54) was followed three minutes later by a 500 on `/kyc/upload`:
   `psycopg.OperationalError: server closed the connection unexpectedly`. Idle pooled
   connections still pointed at the old primary and each failed one request when reused.
   Fix: `check=ConnectionPool.check_connection` in `app/backend_api/db.py` (validates on
   checkout), deployed at 16:01. The RDS result above was measured after the fix.
2. **`failover_test.py` probes the database for real.** On the full profile, `GET /me` is served
   from ElastiCache, so the old "accounts-db (/me)" probe never reached Postgres. The test now
   also probes `POST /me` for an existing customer, an uncached read on the primary. Whether
   the instance failed over now comes from RDS events: `describe-db-instances` still showed the
   old AZ minutes after the failover had completed. The first run's evidence file was
   discarded because of that check (it reported `failed_over: false` after a real failover).
3. **`load_test.py`:** customers sign in again every 240 s (step-up needs a sign-in under 300 s
   old; on 2026-11-01, 1,086 of 2,191 transfers were refused for this); 4xx responses are
   recorded by reason code; `--cooldown` keeps a 1 req/s trickle while sampling Auto Scaling
   to capture scale-in, and the report includes Auto Scaling's activity history.
4. **`capture_dashboard.py`:** renders each CloudWatch dashboard widget as a PNG for a test window
   and saves the alarm state changes and RDS events for that window.
