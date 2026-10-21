# FaCaiBank Architecture & Implementation Rules (AWS ap-southeast-1)

## Core Architecture Rules (Strictly Enforced)
1. **Region & Tagging**: All resources must be in `ap-southeast-1` (Singapore). Apply default tags (`Project = "FaCaiBank"`, `Environment = var.env`, `Owner = "Group3"`) to every resource.
2. **Database Split**:
   - **RDS PostgreSQL**: Stores ONLY `customers` and `accounts` metadata (user_id, account_id, kyc_status, created_at). NO balances or transaction ledgers in RDS.
   - **DynamoDB (`facaibank-ledger`)**: Billing mode `PAY_PER_REQUEST`, encrypted with KMS CMK, `stream_enabled = true` (`NEW_IMAGE`). Stores:
     - Account Balances (`PK = ACCOUNT#<id>`, `SK = BALANCE`)
     - Ledger Entries (`PK = ACCOUNT#<id>`, `SK = TXN#<timestamp>#<txn_id>`)
     - Outbox Events (`PK = OUTBOX#<txn_id>`, `SK = EVENT`)
     - Deduplication Records (`PK = DEDUPE#<event_id>`, `SK = NOTIFICATION`)
3. **Atomic Transfers (`TransactWriteItems`)**:
   - Every transfer MUST execute a single DynamoDB `TransactWriteItems` call containing:
     1. Decrement sender balance (with condition `balance >= amount` and `idempotency_key` check)
     2. Increment receiver balance
     3. Insert sender debit ledger item
     4. Insert receiver credit ledger item
     5. Insert outbox event item
4. **Asynchronous Pipelines**:
   - **Notifications**: DynamoDB Streams -> `outbox_worker.py` -> SQS `events-queue` (with `events-dlq`, `maxReceiveCount = 3`) -> AWS Lambda `lambda_notifier` (checks/writes `DEDUPE#<event_id>` in DynamoDB) -> SNS Topic `customer-notifications`.
   - **Statements**: API / EventBridge Scheduler -> SQS `statement-jobs-queue` (with `statement-dlq`) -> `statement_worker.py` -> generates PDF statement -> uploads to S3 `kyc-statements-bucket` (SSE-KMS) -> returns presigned URL.
5. **Security & Networking**:
   - VPC `10.0.0.0/16` across 3 AZs: Public subnets (`10.0.1.0/24`..`3.0/24`), Private App subnets (`10.0.11.0/24`..`13.0/24`), Private Data subnets (`10.0.21.0/24`..`23.0/24`).
   - Security Group chaining: `Public ALB SG` -> `Frontend SG` -> `Internal ALB SG` -> `Backend App SG` -> `RDS SG`.
   - S3 buckets must enforce `aws:kms` (SSE-KMS) with a Customer-Managed Key (CMK) and block all public access. Audit bucket must enable S3 Object Lock (`COMPLIANCE` / `GOVERNANCE` mode) and a 5-year (1825 days) Glacier Deep Archive retention rule.
6. **Cost Control**: Parameterize `single_nat_gateway = true`, `rds_multi_az = false`, and `enable_interface_endpoints = false` in `dev.tfvars` to protect our $200 credit budget.