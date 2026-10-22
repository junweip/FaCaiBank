# Proposal → implementation map

Where each commitment in the proposal deck (v3) lives in the code. "Optional" means it sits
behind an `enable_*` switch, is off in `dev.tfvars` and on in `full.tfvars`.

## Slide 3 — Use cases

| Use case | Implementation |
|---|---|
| Sign up and authenticate | Cognito user pool with mandatory TOTP MFA ([05-compute-asg.tf](../infra/05-compute-asg.tf)); sign-up, email confirmation, MFA enrolment and sign-in in [app.js](../app/frontend/assets/app.js); JWT verification in [auth.py](../app/backend_api/auth.py) |
| Upload KYC documents (simulated verification) | `POST /kyc/upload`: file-type check, SSE-KMS to S3, customer marked `VERIFIED_MOCK` ([main.py](../app/backend_api/main.py)) |
| Create linked accounts | `POST /accounts`, up to five per customer, owner recorded on the ledger balance item |
| Transfer between accounts | `POST /transfers`, a single DynamoDB transaction ([ledger.py](../app/backend_api/ledger.py)) |
| Deposit and withdraw funds (mock external bank) | `POST /accounts/{id}/deposits` and `/withdrawals` via [external_bank.py](../app/backend_api/external_bank.py), clearing accounts and atomic reversal |
| Receive transaction alerts | Outbox → SQS → [notifier](../app/lambda_notifier/handler.py) → SNS email, filtered per customer; `POST /me/alerts` |
| View balances, read history | `GET /accounts/{id}/balance` (strong read), `/transactions` (eventual read) |
| Request and download statements (asynchronous) | `POST /statements/request` → SQS → [statement_worker.py](../app/workers/statement_worker.py) → PDF; `GET /accounts/{id}/statements/{job}` returns a 15-minute link; monthly run on the 1st |

## Slide 4 — Service targets

| Target | How it is met / measured |
|---|---|
| 99.99% availability | Three-AZ subnets and fleets (`asg_min_size = 3` in full); DynamoDB across 3 AZs; RDS Multi-AZ; CloudFront edge. Measured by `failover_test.py` |
| RPO 0, RTO 10 minutes | DynamoDB transactions are durable across AZs before they're acknowledged; RDS synchronous standby; PITR on both stores. Timed by `failover_test.py` and `restore_test.py` |
| Balance p95 < 200 ms, transfer p95 < 1 s | Per-endpoint latency metrics ([metrics.py](../app/backend_api/metrics.py)), alarms in [06-monitoring-budgets.tf](../infra/06-monitoring-budgets.tf), client-side numbers from `load_test.py` |
| ACID ledger, eventual history | `TransactWriteItems` with conditions; history and statements use eventually consistent reads |
| Singapore only, 5-year retention | All data stores in ap-southeast-1; KYC lifecycle and audit Object Lock set to `audit_retention_days = 1825` |

## Slides 6, 8, 18 — Architecture

| Component | Implementation |
|---|---|
| Route 53, CloudFront, WAF, Shield | [07-edge-cloudfront.tf](../infra/07-edge-cloudfront.tf) (Route 53 when `domain_name` is set; Shield Standard is automatic) |
| Public ALB, frontend fleet, internal ALB, API and worker fleets | [05-compute-asg.tf](../infra/05-compute-asg.tf) |
| NAT per AZ, VPC endpoints | [01-network.tf](../infra/01-network.tf) (`single_nat_gateway`, `enable_interface_endpoints`) |
| RDS primary / standby / read replica | [03-data-storage.tf](../infra/03-data-storage.tf), [09-cache-replica.tf](../infra/09-cache-replica.tf) (optional) |
| ElastiCache (sessions, hot reads) | [09-cache-replica.tf](../infra/09-cache-replica.tf) and [cache.py](../app/backend_api/cache.py): profile/ownership cache and logout revocation list. Balances never cached. Optional |
| SageMaker fraud scoring before commit | [10-fraud-sagemaker.tf](../infra/10-fraud-sagemaker.tf), [fraud_model](../app/fraud_model), [fraud.py](../app/backend_api/fraud.py) (optional; rules fallback) |
| S3 + Glacier (KYC, statements, audit) | [03-data-storage.tf](../infra/03-data-storage.tf) |
| KMS, Secrets Manager | [02-security-kms.tf](../infra/02-security-kms.tf); RDS-managed rotating secret; Valkey and Slack secrets |
| EventBridge Scheduler monthly statements | [04-messaging-lambda.tf](../infra/04-messaging-lambda.tf) |
| Analytics: S3 lake, Glue, Athena, QuickSight | [11-analytics.tf](../infra/11-analytics.tf), [ledger_to_parquet.py](../app/analytics/ledger_to_parquet.py) (optional) |
| AI/ML safeguards: Bedrock assistant + Guardrails, Security Hub, Config, GuardDuty, Macie | [12-ops-assistant.tf](../infra/12-ops-assistant.tf) (optional), [08-security-services.tf](../infra/08-security-services.tf) |
| IAM Identity Center (staff SSO + MFA) | [13-identity-center.tf](../infra/13-identity-center.tf) (optional; needs Identity Center enabled) |

## Slide 7 — Independent scaling

| Fleet | Scales on | Where |
|---|---|---|
| Frontend | ALB requests per target, CPU | `aws_autoscaling_policy.requests_per_target["frontend"]`, `cpu["frontend"]` |
| Backend API | ALB requests per target, CPU | `requests_per_target["backend"]`, `cpu["backend"]` |
| Outbox worker | Stream iterator age (published by the worker); shards split across workers through leases | `outbox_iterator_age`, [outbox_worker.py](../app/workers/outbox_worker.py) |
| Statement worker | SQS backlog per worker | `statement_backlog` (metric math) |

## Slide 9 — Transfer processing

API authorizes (JWT, ownership, KYC, step-up) and checks idempotency → fraud score → one
DynamoDB transaction (legs, balances, outbox event, idempotency record) → outbox worker →
SQS (retries, DLQ) → notifier Lambda (dedupe by event ID) → SNS.

## Slide 11 — Security

| Control | Implementation |
|---|---|
| WAF injection rules, rate limits | CloudFront WAF: Common, SQLi, Known Bad Inputs, IP reputation, per-IP rate limit, optional Bot Control |
| Cognito MFA, transfer step-up, JWT on every request | [auth.py](../app/backend_api/auth.py): RS256 against JWKS, issuer, client, `token_use`; RFC 9470 step-up |
| IAM least privilege | One role per fleet with resource-scoped policies; verified by `access_control_test.py` |
| Private subnets, SG-to-SG, VPC endpoints | Security group chain in [05-compute-asg.tf](../infra/05-compute-asg.tf) and [02-security-kms.tf](../infra/02-security-kms.tf) |
| Secrets Manager, encrypted transport | RDS TLS (`sslmode=require`), Valkey TLS + AUTH, TLS-only bucket policies, HTTPS-only CloudFront with HSTS |
| CloudTrail, GuardDuty, Macie, Security Hub | [08-security-services.tf](../infra/08-security-services.tf) |
| S3 Object Lock (WORM) audit archives | Audit bucket, retention mode configurable |

## Slide 12 — Monitoring and alerts

See [runbooks/monitoring-and-alerts.md](runbooks/monitoring-and-alerts.md) for the full
alarm table. Routing: page (on-call), ticket, security, budget email; delivered by email
and optionally Slack.

## Slides 13–16 — Scope, plan, validation, management

| Commitment | Implementation |
|---|---|
| IaC, private access, KMS, audit | Terraform throughout; remote state ([bootstrap](../infra/bootstrap)) |
| Monitoring, backups, restore test | Alarms and dashboard; PITR on DynamoDB, RDS automated backups; `restore_test.py` |
| Schema and deployment pipeline | Versioned SQL migrations; [CI](../.github/workflows/ci.yml) and [Deploy](../.github/workflows/deploy.yml) workflows |
| Recovery after worker restart; duplicate and failed messages | Shard leases and checkpoints; `recovery_test.py`, `validate_all.py` test C |
| Load test, scaling, alarms | `load_test.py` |
| Cost review, runbooks | [cost-review.md](cost-review.md), [runbooks](runbooks) |
| Versioned deployment, rollback procedure | Content-addressed bundles and instance refresh; [deploy-and-rollback.md](runbooks/deploy-and-rollback.md) |
| Resource tags and owners | Default tags on every resource, including `Owner` and `AppVersion` |
| Full fleet redundancy across three AZs | `asg_min_size = 3` in `full.tfvars` |

## Not implemented (out of scope or needs external accounts)

- Real identity proofing, FAST/PayNow settlement, SMS and push notifications (slide 13 exclusions).
- Passkeys and Cognito adaptive authentication (Cognito Plus tier).
- Live load at the 5-million-customer scale (slide 13: "Proof of capacity at the full target" is out of scope).
