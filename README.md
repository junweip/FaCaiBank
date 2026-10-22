# FaCaiBank

A fully digital retail bank prototype on AWS (ap-southeast-1), built for IS458 Cloud
Management and Engineering, Group 3. Customers sign up with MFA, verify their identity,
open linked accounts, move money between FaCaiBank accounts and partner banks, receive
transaction alerts and download PDF statements.

## What is in this repository

| Path | What it is |
|---|---|
| [app/backend_api](app/backend_api) | FastAPI banking API: Cognito JWT auth with step-up, DynamoDB ledger transactions, mock partner bank, fraud scoring, statements, alerts |
| [app/frontend](app/frontend) | Single-page web app (sign-up, MFA, dashboard). `assets/` is served from S3 through CloudFront |
| [app/workers](app/workers) | Outbox worker (DynamoDB Streams → SQS, shard leases and checkpoints) and statement worker (PDF statements, monthly run) |
| [app/lambda_notifier](app/lambda_notifier) | Per-customer transaction alerts (SQS → SNS) with deduplication |
| [app/lambda_ops_assistant](app/lambda_ops_assistant) | Bedrock ops assistant: summarises alarms for on-call, PII masked by a guardrail |
| [app/lambda_slack_alerts](app/lambda_slack_alerts) | Forwards page, ticket and security alerts to Slack |
| [app/fraud_model](app/fraud_model) | Transfer-risk model served by a SageMaker serverless endpoint |
| [app/analytics](app/analytics) | Glue job that turns the daily ledger export into Parquet for Athena |
| [infra](infra) | Terraform for the whole stack; [infra/bootstrap](infra/bootstrap) creates shared state and the CI deploy role |
| [tests](tests) | Unit tests (`pytest`) and live scripts that produce evidence against a deployed stack |
| [docs](docs) | Runbooks, cost review, validation guide and the proposal-to-implementation map |

## Quick start

```bash
# Unit tests (no AWS needed: DynamoDB, S3, SQS and SNS are emulated by moto)
python -m pip install -r app/backend_api/requirements-dev.txt
python -m pytest -q

# First deployment (one person, once): shared state + deploy role
cd infra/bootstrap && terraform init && terraform apply
# Then deploy the stack (see infra/README.md for profiles and options)
cd .. && cp backend.hcl.example backend.hcl   # fill in from the bootstrap output
terraform init -backend-config=backend.hcl
terraform apply -var-file=dev.tfvars
terraform output website_url
```

After that, deployments run from GitHub Actions: push a tag such as `v1.0.0`, or run the
**Deploy** workflow with a ref. Rolling back means deploying the previous tag
([runbook](docs/runbooks/deploy-and-rollback.md)).

## Read next

- [ARCHITECTURE.md](ARCHITECTURE.md): the rules the code follows and where it deviates from the proposal diagram
- [docs/traceability.md](docs/traceability.md): every proposal slide mapped to the code that implements it
- [docs/validation.md](docs/validation.md): how to produce the slide-15 evidence
- [docs/cost-review.md](docs/cost-review.md): what the stack costs per day and how to stay inside the credit
- [docs/runbooks](docs/runbooks): what to do when an alarm fires
