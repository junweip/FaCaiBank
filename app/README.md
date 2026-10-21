# FaCaiBank demo applications

## Backend API

The FastAPI service is in `backend_api`. Install dependencies and launch it from the repository root:

```powershell
python -m pip install -r app/backend_api/requirements.txt
$env:DATABASE_HOST = "your-rds-endpoint"
$env:DATABASE_SECRET_ARN = "your-rds-credentials-secret-arn"
$env:KYC_STATEMENTS_BUCKET = "your-kyc-statements-bucket"
$env:STATEMENT_QUEUE_URL = "your-statement-jobs-queue-url"
$env:LEDGER_TABLE_NAME = "facaibank-ledger"
uvicorn app.backend_api.main:app --reload
```

Set AWS credentials through the normal AWS SDK credential chain. The service defaults to `ap-southeast-1`. It fetches the PostgreSQL username and password from `DATABASE_SECRET_ARN`, creates its `customers` and `accounts` metadata tables on first database use, and stores balances and transactions only in DynamoDB.

The frontend uses `http://localhost:8000` by default. Open `frontend/index.html` with a static server; to target another API origin, set `window.FACAI_API_BASE` before `app.js` loads. Add that origin to `CORS_ORIGINS` on the backend.

## Frontend

The static single-page demo is in `frontend`. Serve it from the repository root with:

```powershell
python -m http.server 5173 --directory app/frontend
```

## Background workers

Install `workers/requirements.txt` and run each long-lived worker as its own process:

```powershell
python -m pip install -r app/workers/requirements.txt
$env:LEDGER_TABLE_NAME = "facaibank-ledger"
$env:EVENTS_QUEUE_URL = "your-events-queue-url"
python -m app.workers.outbox_worker
```

The statement worker uses `STATEMENT_QUEUE_URL` and `KYC_STATEMENTS_BUCKET`:

```powershell
$env:STATEMENT_QUEUE_URL = "your-statement-jobs-queue-url"
$env:KYC_STATEMENTS_BUCKET = "your-kyc-statements-bucket"
python -m app.workers.statement_worker
```

Both workers default to `ap-southeast-1` and use the AWS SDK credential chain. Grant the outbox worker `dynamodb:DescribeTable`, DynamoDB Streams `DescribeStream`, `GetShardIterator`, and `GetRecords`, plus `sqs:SendMessage` on `events-queue`. Grant the statement worker `dynamodb:Query` and `PutItem` on the ledger table, SQS receive/delete/get-attributes permissions on `statement-jobs-queue`, and S3 `PutObject`/`GetObject` permissions on the KYC/statements bucket. Their roles also need the KMS permissions required for the encrypted table, queue, and bucket.

The statement worker uploads an HTML statement with SSE-KMS and stores its `download_url` in the ledger table under `PK=STATEMENT#<job_id>`, `SK=JOB`. The presigned URL expires after seven days by default; set `PRESIGNED_URL_EXPIRATION` to change the duration.

## Compute and alerts

`infra/05-compute-asg.tf` builds and uploads frontend, backend, and worker bundles to the encrypted KYC/statements bucket. Three private-subnet Auto Scaling Groups bootstrap those bundles as systemd services. The public ALB serves the frontend and proxies `/api/` through the internal ALB to the backend. Cognito provisions an email-based user pool, app client, and hosted UI domain; the public listener remains HTTP because no ACM certificate/domain was supplied, so ALB Cognito authentication is not enabled.

`infra/06-monitoring-budgets.tf` creates the latency, 5xx-rate, DynamoDB stream-age, and both DLQ alarms, as well as $200 monthly budget notifications at 50%, 80%, and 100%. CloudWatch and Budgets publish to the `alerts_topic_arn` output. Subscribe an email or other endpoint to that SNS topic after deployment to receive notifications.

## Live presentation validation

`tests/validate_all.py` performs real transfers and sends test messages to the live SQS queues. Use a non-production environment and choose two existing accounts with sufficient sender funds. The script requires `--execute`, uses the normal AWS credential chain, waits for the Lambda/DLQ/CloudWatch alarm evidence, and saves a JSON report under `tests/evidence/`.

```powershell
$env:FACAI_API_URL = "http://your-public-alb-dns"
$env:FACAI_SENDER_ACCOUNT_ID = "sender-account-id"
$env:FACAI_RECEIVER_ACCOUNT_ID = "receiver-account-id"
$env:LEDGER_TABLE_NAME = "facaibank-ledger"
$env:EVENTS_QUEUE_URL = "your-events-queue-url"
$env:EVENTS_DLQ_URL = "your-events-dlq-url"
$env:LAMBDA_LOG_GROUP = "/aws/lambda/facaibank-dev-lambda-notifier"
$env:DLQ_ALARM_NAME = "facaibank-dev-events-dlq-visible"
python tests/validate_all.py --execute
```

The caller needs DynamoDB `GetItem`/`Query`, SQS `SendMessage`/`ReceiveMessage`/`ChangeMessageVisibility`/`GetQueueAttributes`, CloudWatch `DescribeAlarms`, CloudWatch Logs `FilterLogEvents`, and KMS `Decrypt`/`GenerateDataKey` permissions in addition to permission to make transfers through the API. Test C requires the events DLQ to be empty and its alarm not already in `ALARM`; it intentionally causes a malformed message to reach the events DLQ and the alarm to enter `ALARM`. Clear that test message and allow the alarm to return to `OK` after capturing evidence.
