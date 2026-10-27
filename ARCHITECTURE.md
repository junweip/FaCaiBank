# FaCaiBank Architecture & Implementation Rules (AWS ap-southeast-1)

## Core Architecture Rules (Strictly Enforced)

1. **Region & Tagging**: All resources are in `ap-southeast-1` (Singapore). The only exceptions are the CloudFront certificate and the CLOUDFRONT-scoped WAF web ACL, which AWS requires in `us-east-1`; they hold edge configuration, never customer data. Default tags on every resource: `Project`, `Environment`, `Owner = "Group3"`, `ManagedBy`, `AppVersion`.

2. **Database Split**:
   - **RDS PostgreSQL**: customer and account metadata only (`customers`: Cognito subject, name, email, KYC status; `accounts`: type, nickname, currency). No balances or ledger entries. Schema changes are versioned SQL files in `app/backend_api/migrations`, applied once under a PostgreSQL advisory lock.
   - **DynamoDB (`facaibank-ledger`)**: `PAY_PER_REQUEST`, KMS CMK, Streams (`NEW_IMAGE`), point-in-time recovery, TTL on `expires_at`. Items:
     - Balances: `PK = ACCOUNT#<id>`, `SK = BALANCE` (carries `owner_sub`, the owner's Cognito subject)
     - Ledger entries: `PK = ACCOUNT#<id>`, `SK = TXN#<timestamp>#<transaction_id>`
     - Clearing accounts for partner-bank flows: `PK = ACCOUNT#CLEARING-<shard>` (sharded to avoid a hot key)
     - Outbox events: `PK = OUTBOX#<transaction_id>`, `SK = EVENT` (expire after 7 days)
     - Idempotency records: `PK = IDEMPOTENCY#<sub>#<key>`, `SK = TRANSFER|DEPOSIT|WITHDRAWAL` (store the original response)
     - Dedupe records: `PK = DEDUPE#<event_id>`, `SK = NOTIFICATION#<role>`
     - Statement jobs: `PK = ACCOUNT#<id>`, `SK = STATEMENT#<job_id>`
     - Revoked sessions: `PK = REVOKED_SESSION#<jti>`, `SK = SESSION` (written at sign-out, expires with the access token; read on every request when the API has no shared cache, so every worker honours a sign-out)
   - **`facaibank-<env>-outbox-leases`**: shard leases, checkpoints and worker heartbeats for the outbox fleet.

3. **Atomic Money Movement (`TransactWriteItems`)**:
   - A transfer is one transaction: sender balance − amount (condition: exists, `owner_sub` = caller, `balance >= amount`), receiver balance + amount, debit leg, credit leg, outbox event, idempotency record.
   - Deposits and withdrawals post the other leg to a clearing account, so the sum of all balances is always zero.
   - A withdrawal debits into clearing first, then calls the partner bank; if the bank declines, a second transaction reverses it atomically.
   - Repeating a request with the same `Idempotency-Key` returns the original response (`Idempotent-Replayed: true`); the same key with a different body returns 409.

4. **Asynchronous Pipelines**:
   - **Notifications**: DynamoDB Streams → `outbox_worker.py` (leases + checkpoints, at-least-once) → SQS `events-queue` (`events-dlq`, `maxReceiveCount = 3`) → Lambda `lambda_notifier` (dedupe per event and recipient) → SNS `customer-notifications`, one alert per recipient tagged with `customer_sub`; each customer's email subscription filters on it.
   - **Statements**: API or EventBridge Scheduler (monthly `MONTHLY_RUN`) → SQS `statement-jobs-queue` (`statement-dlq`) → `statement_worker.py` → PDF in S3 `kyc-statements` (SSE-KMS); the API issues a 15-minute presigned download.

5. **Security & Networking**:
   - Customers → CloudFront (HTTPS, WAF, security headers) → public ALB → frontend fleet → internal ALB → API fleet. The public ALB accepts only CloudFront's origin-facing prefix list and a secret `X-Origin-Verify` header.
   - Every banking request carries a Cognito access token (MFA required). Transfers and withdrawals of SGD 1,000 or more, or those the fraud model flags, also need a sign-in in the last 5 minutes (step-up).
   - VPC `10.0.0.0/16` across 3 AZs: public subnets (`10.0.1-3.0/24`, ALB and NAT only), private app subnets (`10.0.11-13.0/24`, all EC2 fleets), private data subnets (`10.0.21-23.0/24`, RDS and ElastiCache).
   - Security group chain: CloudFront prefix list → public ALB → frontend → internal ALB → API → RDS/ElastiCache. Statement workers may also reach RDS; outbox workers have no inbound rules.
   - All buckets: SSE-KMS (CMK), public access blocked, TLS-only policies. The audit bucket uses Object Lock (`GOVERNANCE` in dev, `COMPLIANCE` for production) with 5-year retention.
   - No SSH: instances are reached through Session Manager. Services run as a non-root user.

6. **Cost Control**: `dev.tfvars` keeps one NAT gateway, single-AZ RDS, no interface endpoints and one instance per fleet. `full.tfvars` turns on everything in the diagram for demos. See [docs/cost-review.md](docs/cost-review.md).

## Deviations from the proposal diagram (slides 6 and 18)

| Diagram | Implementation | Why |
|---|---|---|
| Frontend servers in public subnets | Frontend servers in private app subnets | Instances have no public IPs; only the ALB and NAT gateways sit in public subnets. Smaller attack surface, same request path. Update the slide or mention this in the report. |
| WAF in front of CloudFront, plus the ALB | WAF attached to CloudFront only; the ALB is locked to CloudFront | One inspection point; requests that bypass CloudFront are refused |
| Real-time fraud scoring (SageMaker) | SageMaker serverless endpoint with a rules fallback | A slow or failing endpoint must not block transfers or let them through unscored |
| Shield | Shield Standard (automatic, free) | Shield Advanced costs USD 3,000/month |
| Push notifications | Email (SNS) per customer | Push needs mobile app credentials (APNs/FCM); SMS needs SNS production access |
| Adaptive auth, passkeys | Not implemented | Cognito Plus tier and WebAuthn were outside the agreed scope |
