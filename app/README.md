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
