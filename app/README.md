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
