# FaCaiBank infrastructure (Terraform)

## Files

| File | Contents |
|---|---|
| `01-network.tf` | VPC, 3 × (public, app, data) subnets, NAT, S3/DynamoDB gateway endpoints, optional interface endpoints, VPC flow logs |
| `02-security-kms.tf` | Customer-managed KMS key and its policy, API and RDS security groups |
| `03-data-storage.tf` | RDS PostgreSQL, DynamoDB ledger (PITR, TTL, Streams), outbox lease table, KYC/statements, audit (Object Lock) and artifacts buckets |
| `04-messaging-lambda.tf` | SQS queues and DLQs, customer-notification SNS topic, notifier Lambda, monthly statement schedule |
| `05-compute-asg.tf` | Versioned bundles, ALBs, Cognito, instance roles, launch templates, four Auto Scaling groups and their scaling policies |
| `06-monitoring-budgets.tf` | Page/ticket/security topics, alarms, RDS event subscription, budget, Slack forwarder, dashboard |
| `07-edge-cloudfront.tf` | CloudFront, WAF (us-east-1), static assets bucket (OAC), security headers, optional domain and certificates |
| `08-security-services.tf` | CloudTrail and security-change alarms, GuardDuty, Security Hub, Macie, AWS Config |
| `09-cache-replica.tf` | Optional ElastiCache (Valkey) and RDS read replica |
| `10-fraud-sagemaker.tf` | Optional SageMaker serverless fraud endpoint |
| `11-analytics.tf` | Optional analytics lake: daily export, Glue job and crawler, Athena, QuickSight |
| `12-ops-assistant.tf` | Optional Bedrock ops assistant and guardrail |
| `13-identity-center.tf` | Optional IAM Identity Center permission sets and groups |
| `14-cost-controls.tf` | Optional office-hours schedule |
| `moved.tf` | Keeps resources from the first release in place after renames |

## Profiles

| Setting | `dev.tfvars` | `full.tfvars` (demo day) |
|---|---|---|
| NAT gateways | 1 | 3 (one per AZ) |
| Interface VPC endpoints | off | on |
| RDS | single-AZ `db.t4g.micro` | Multi-AZ `db.t4g.small` + read replica in AZ 3 |
| ElastiCache | off | Valkey primary (AZ 1) + replica (AZ 2) |
| Instances per fleet | 1 (scales to 3) | 3, one per AZ (scales to 6) |
| CloudTrail, GuardDuty, Security Hub, Macie, Config | on | on |
| SageMaker, analytics lake, Bedrock assistant | off | on |
| Approximate cost | USD 6–7 per day | USD 30 per day |

Turn anything on or off individually with the `enable_*` variables in `variables.tf`.

## First deployment

1. **Bootstrap once** (creates the state bucket, lock table and the GitHub deploy role):
   ```bash
   cd infra/bootstrap && terraform init && terraform apply
   terraform output -raw backend_hcl > ../backend.hcl
   ```
2. **Deploy**:
   ```bash
   cd infra
   terraform init -backend-config=backend.hcl
   terraform apply -var-file=dev.tfvars -var alert_email=you@example.com
   ```
   Confirm the SNS subscription emails AWS sends you.
3. **Open the site**: `terraform output website_url`. Sign up, set up an authenticator app, verify your identity (any small PDF/PNG/JPG), open an account and add money.
4. **Enable CI deploys**: in GitHub, create an environment `dev` with the secret `AWS_DEPLOY_ROLE_ARN` (bootstrap output) and variable `TF_STATE_BUCKET`, optionally `ALERT_EMAIL` and `SECURITY_ALERT_EMAIL`. Add required reviewers to the environment so every apply needs an approval.

### Optional components

- **Bedrock ops assistant**: request access to Anthropic models in the Bedrock console (ap-southeast-1), run `python scripts/build_lambdas.py`, then set `enable_bedrock_assistant = true`.
- **QuickSight**: subscribe to QuickSight, grant it access to Athena and the analytics bucket, then set `quicksight_user_arn`. Build dashboards on the three saved Athena queries.
- **IAM Identity Center**: enable it in the console, require MFA in its settings, then set `enable_identity_center = true` and add staff to the `FaCaiBank-dev-*` groups.
- **Custom domain**: set `domain_name` and `route53_zone_id`. Certificates are issued and validated automatically, and CloudFront connects to the ALB over HTTPS.
- **Account-level services**: GuardDuty, Security Hub, Macie and Config can each be enabled only once per account and region. If one is already on (for example from another stack), set its `enable_*` variable to `false`.

## Upgrading a stack deployed from the first release

The `moved.tf` blocks keep the frontend and backend fleets, roles and launch templates. Expect these changes on the first apply:

- The single `workers` fleet becomes `outbox-workers` and a new `statement-workers` fleet is created.
- Deployment bundles move from the KYC bucket to the new artifacts bucket.
- The regional WAF on the ALB is removed (WAF now runs on CloudFront) and the ALB stops accepting traffic from anywhere but CloudFront. Use `website_url`, not the ALB address.
- **Cognito MFA**: if AWS refuses to switch an existing pool to `mfa_configuration = "ON"`, recreate it with `terraform apply -replace=aws_cognito_user_pool.main` (test users must sign up again).
- **Audit bucket**: objects already written under the old `COMPLIANCE` retention stay locked for 5 years even after the default changes to `GOVERNANCE`, so that bucket cannot be emptied or deleted before then.
- **Local state**: if you applied with local state before, run `terraform init -migrate-state -backend-config=backend.hcl` once to move it to S3.

## Cleaning up

```bash
terraform destroy -var-file=dev.tfvars
```

The audit bucket is the exception: Object Lock keeps its objects for the retention period. In `GOVERNANCE` mode an administrator can delete them with `s3:BypassGovernanceRetention`. See [docs/runbooks/cleanup.md](../docs/runbooks/cleanup.md).
