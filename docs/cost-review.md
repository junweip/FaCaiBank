# Cost review

All figures are approximate on-demand prices for ap-southeast-1 (Singapore), in USD,
excluding tax and free-tier allowances unless stated. Check them in the
[AWS Pricing Calculator](https://calculator.aws/) before quoting them in the report;
prices change and some services bill by usage.

## The short version

| Profile | Per day | Per 30 days | Use it for |
|---|---:|---:|---|
| Before this branch (first release) | ~5 | ~155 | — |
| `dev.tfvars` | ~6.8 | ~205 | Development sessions; destroy when idle |
| `dev.tfvars` + office-hours schedule | ~5.5 | ~165 | A stack that stays up for a whole week |
| `full.tfvars` | ~33 | ~990 | Demo day and failover drills only |

The team has a **USD 200 credit**, and a dev stack left running for a month uses all of it.
So: **apply for work sessions and demos, then `terraform destroy`.** At these estimates, 20
dev-days plus 2 full-profile days is about USD 202, which is slightly over the credit, so
plan for about 18 dev-days or use the office-hours schedule. Check the real figure in Cost
Explorer. The budget alarms (50/80/100% of `monthly_budget_limit`, plus a forecast alarm)
warn before the credit runs out.

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
| RDS Multi-AZ db.t4g.small (+ read replica) | 3.9 | Synchronous standby. The estimate includes a replica in AZ 3, but RDS refused to create one with a Secrets Manager-managed password, so the 2026-11-08 run had none and the real figure is slightly lower |
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

The specification sets an estimated 13 million users at launch. The final report plans for
13 million **accounts** held by 5 million customers (2.6 each) and tabulates the literal
13-million-customer case beside it; the reading is pending confirmation with the
instructor. [traceability.md](traceability.md) has the full workload table.

With a 25% daily-active rate, three money movements per active user per day, a 3x peak
factor and ten reads per ledger item write, the planning basis gives 43 money movements/s
on average and 130/s at peak. The implemented transfer has six DynamoDB write actions (two
balance updates, two ledger entries, one outbox event and one idempotency record). At two
write units per transactional item that is about 520 write units/s on average and 1,560/s
at peak, or about 1.35 billion write units a month at the average rate. The literal case is
about 2.6 times higher (about 1,354 and 4,063 write units/s).

At the on-demand price for ap-southeast-1 of **USD 0.71 per million write request units**
(AWS Price List, published 2026-11-05), write capacity alone is:

| Case | Average rate, per month | Peak rate sustained all month |
|---|---:|---:|
| Planning basis (13M accounts) | about USD 960 | about USD 2,870 |
| Literal (13M customers) | about USD 2,500 | about USD 7,500 |

The peak column is an upper bound, not a forecast. These figures exclude reads and other
services, and prices change, so confirm them in the AWS Pricing Calculator. AWS halved
on-demand throughput prices in November 2024; earlier versions of this file used the older
price and overstated the figures by about 1.9 times. Provisioned capacity with auto scaling
and reserved capacity are the candidates for later optimisation. The prototype was load
tested at 90 requests/s, not at these rates, so it does not prove launch-scale capacity.
