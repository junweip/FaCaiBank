# Monitoring and alerts

Every alarm traces back to a target on proposal slide 4. Alarms publish to one of three SNS
topics; subscribe people with `alert_email` / `security_alert_email`, or to Slack with
`slack_webhook_url`. With the ops assistant enabled, page and security alerts also arrive
as a plain-language summary on the ops-summaries topic.

| Alarm (`facaibank-<env>-…`) | Fires when | Topic | First response |
|---|---|---|---|
| `balance-latency-p95` | Balance p95 > 200 ms for 5 consecutive minutes | page | [Latency](#latency) |
| `transfer-latency-p95` | Transfer p95 > 1 s for 5 minutes | page | [Latency](#latency) |
| `alb-5xx-error-rate` | 5xx > 1% in 2 of 3 minutes | page | [Errors](#errors) |
| `frontend-unhealthy-targets`, `backend-unhealthy-targets` | A target fails health checks for 5 min | page | [Errors](#errors) |
| `dynamodb-stream-iterator-age` | Outbox more than 60 s behind, or no metric (worker down) | page | [worker-recovery.md](worker-recovery.md) |
| RDS event subscription | Any failover, failure, availability or low-storage event | page | [rds-failover.md](rds-failover.md) |
| `events-dlq-visible`, `statement-dlq-visible` | A message reached a DLQ | ticket | [worker-recovery.md](worker-recovery.md) |
| `statement-oldest-job` | A statement job waited > 15 min | ticket | [worker-recovery.md](worker-recovery.md) |
| `notifier-errors` | Notification Lambda errors | ticket | [worker-recovery.md](worker-recovery.md) |
| `ledger-write-throttles` | DynamoDB throttled ledger writes | ticket | [Throttling](#throttling) |
| `rds-replica-lag` | Read replica > 5 s behind | ticket | [rds-failover.md](rds-failover.md#replica-lag) |
| `security-root-account-usage`, `-iam-policy-changes`, `-kms-key-changes`, `-cloudtrail-changes`, `-console-signin-without-mfa` | Matching CloudTrail event | security | [security-incident.md](security-incident.md) |
| GuardDuty ≥ 7, Security Hub HIGH/CRITICAL, Macie findings | New finding | security | [security-incident.md](security-incident.md) |
| Budget 50 / 80 / 100% (actual) and 100% (forecast) | Monthly spend | ticket + budget emails | [../cost-review.md](../cost-review.md) |

The **facaibank-&lt;env&gt;** CloudWatch dashboard (`terraform output dashboard_url`) shows latency
against targets, traffic and 5xx, instances per fleet, background-work lag and queue
depth, ledger capacity and RDS load.

## Latency

1. Open the dashboard. Check whether request volume rose (scaling should follow) or latency rose at flat volume.
2. **Auto Scaling > Activity** for `facaibank-<env>-backend`: is a scale-out in progress or blocked (for example, `max_size` reached)?
3. **CloudWatch Logs Insights** on `/facaibank/<env>/backend`: `fields @timestamp, @message | filter @message like /ERROR|WARNING/ | sort @timestamp desc`.
4. For transfers, check the DynamoDB `TransactionConflict` metric (hot accounts) and whether the fraud endpoint is timing out (`Fraud endpoint … unavailable; using rules` in the logs).
5. Temporary relief: raise `asg_max_size` or `app_instance_type` and apply.

## Errors

1. Find which tier: `HTTPCode_ELB_5XX` (no healthy targets) vs `HTTPCode_Target_5XX` (application).
2. If targets are unhealthy, open a shell with **Session Manager** on an instance from the fleet: `sudo journalctl -u facaibank-backend -n 100` and `sudo tail -100 /var/log/facaibank-bootstrap.log`.
3. If errors started right after a deployment, roll back ([deploy-and-rollback.md](deploy-and-rollback.md)). Instance refresh also rolls back automatically if new instances fail health checks.
4. Check dependencies: RDS status, the Secrets Manager secret (password rotation), DynamoDB `SystemErrors`.

## Throttling

On-demand tables throttle when one partition gets more than about 1,000 writes per second
(a hot account or clearing shard) or the account table limit is reached. Check
CloudWatch Contributor Insights for the hot key; raise `CLEARING_SHARDS` if clearing
accounts are hot.

## Escalation

Page: on-call engineer acknowledges within 15 minutes. If it isn't resolved in 30 minutes,
or customer money may be affected (ledger errors, wrong balances), escalate to the whole
team. Security topic: the security owner, immediately.
