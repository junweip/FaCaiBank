# Cost review

All figures are approximate on-demand prices for ap-southeast-1 (Singapore), in USD,
excluding tax and free-tier allowances unless stated. Check them in the
[AWS Pricing Calculator](https://calculator.aws/) before quoting them in the report;
prices change and some services bill by usage.

## The short version

| Profile | Per day | Per 30 days | Use it for |
|---|---:|---:|---|
| Before this branch (first release) | ~5 | ~155 | — |
| `dev.tfvars` | ~6.5 | ~200 | Development sessions; destroy when idle |
| `dev.tfvars` + office-hours schedule | ~5.5 | ~165 | A stack that stays up for a whole week |
| `full.tfvars` | ~30 | ~900 | Demo day and failover drills only |

The team has a **USD 200 credit**, and a dev stack left running for a month uses all of it.
So: **apply for work sessions and demos, then `terraform destroy`.** About 20 dev-days plus
2 full-profile days fits within the credit. The budget alarms (50/80/100% of
`monthly_budget_limit`, plus a forecast alarm) warn before that happens.

## Where the money goes (dev profile, per month)

| Item | Monthly | Notes |
|---|---:|---|
| NAT gateway (1) + Elastic IP | 47 | USD 0.059/hour plus USD 0.059 per GB processed. The largest fixed cost. |
| Application Load Balancers (2) + public IPv4 | 50 | USD 0.0252/hour each; the public ALB holds 3 public IPv4 addresses at USD 0.005/hour each |
| EC2: 4 × t3.micro + 8 GB gp3 each | 42 | One instance per fleet (frontend, API, outbox, statements) |
| RDS PostgreSQL db.t4g.micro, single-AZ, 20 GB | 21 | Backups free up to database size |
| AWS WAF (1 web ACL, 5 rules) | 10 | Plus USD 0.60 per million requests |
| CloudWatch: ~30 alarms, ~35 custom metrics, logs | 16 | Custom metrics are USD 0.30 each. Endpoint metrics are capped at six endpoints and host metrics drop the hostname dimension, so instance churn doesn't add metrics. |
| GuardDuty, Security Hub, Config, Macie, VPC flow logs | 15 | GuardDuty and Security Hub are free for the first 30 days. Config records daily. |
| CloudWatch Logs data protection (PII masking) | < 1 | About USD 0.12 per GB of application and Lambda logs scanned (check the Singapore price). A dev stack writes a few GB a month. Scales with log volume, so keep debug logging off in production. |
| KMS key, Secrets Manager, DynamoDB, S3, SQS, SNS, Lambda, Scheduler, CloudFront | 4 | Usage-based and tiny at prototype volume. CloudFront's always-free tier covers 1 TB and 10 million requests. |
| **Total** | **~205** | |

## What the full profile adds (per day)

| Item | Per day | Notes |
|---|---:|---|
| Interface VPC endpoints (11 services × 3 AZs) | 10.3 | USD 0.013/hour per endpoint per AZ. Turn off `enable_interface_endpoints` to save a third of the full-profile cost. |
| EC2: 12 × t3.small | 7.9 | Full redundancy: one instance per AZ in every fleet |
| NAT gateways: 2 more | 2.9 | One per AZ removes the cross-AZ single point of failure |
| RDS Multi-AZ db.t4g.small + read replica | 3.9 | Synchronous standby plus replica in AZ 3 |
| ElastiCache Valkey: 2 × cache.t4g.micro | 1.0 | Primary + replica |
| SageMaker serverless, Bedrock, Glue, Athena | < 0.5 | Pay per use; a summary of one alert with Claude Opus 5.5 costs cents |
| QuickSight | 0.8 | USD 24/month per author after the free trial; only if subscribed |

## Cost controls in place

- **Tagging:** every resource carries `Project`, `Environment`, `Owner`, `ManagedBy` and `AppVersion`. Activate these as cost-allocation tags in the Billing console to see spend per environment and release.
- **Budgets:** monthly budget with alerts to the ticket topic and the budget owners' email at 50%, 80% and 100% actual, and 100% forecast.
- **Profiles:** expensive components are behind `enable_*` switches; `dev.tfvars` is the cheapest configuration that still exercises every code path.
- **Office-hours schedule** (`enable_office_hours_schedule`): fleets scale to zero and RDS stops between 23:00 and 08:00 SGT and at weekends.
- **Lifecycle rules:** raw analytics exports expire after 7 days, Athena results after 30, old bundle versions after 90. Audit logs move to Glacier Deep Archive after 90 days.
- **On-demand everywhere it fits:** DynamoDB, Lambda, SageMaker serverless and Athena bill only for use, so idle cost is close to zero.

## At the specification's planned scale

The project specification sets an estimated 13 million users at launch. Proposal v3's
5-million-customer workload is marked provisional, so its original 217 item writes/s and
2,170 reads/s should not be presented as the 13-million-user sizing case.

For a comparable estimate, retain the proposal's other assumptions: 25% daily-active
users, three money movements per active user per day, a 3x peak factor, and ten reads per
item write. This yields about 3.25 million daily active users, 9.75 million daily money
movements, 113 movements/s average and 339/s at peak. The implemented transfer has six
DynamoDB write actions (two balance updates, two ledger entries, one outbox event, and one
idempotency record); at two write capacity units per transactional item, that is about
1,354 write units/s average and 4,063/s at peak. The corresponding read estimate is about
6,770 requests/s average and 20,310/s at peak.

Using the same rough per-item pricing basis as the earlier 5-million estimate, the write
capacity alone is approximately USD 4,700/month at average demand, or USD 15,600/month if
the modeled 3x peak were sustained all month. These are planning estimates, not a quote:
they exclude reads and other services, use on-demand rates that may change, and require
validation in the AWS Pricing Calculator. Provisioned capacity with auto scaling and
reserved capacity would be candidates for later optimization. The prototype was not
load-tested at these rates; its live load test targets 40 total requests/s and does not
prove launch-scale capacity.
