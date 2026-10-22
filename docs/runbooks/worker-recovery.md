# Background workers, queues and DLQs

## Outbox worker (stream age page)

The outbox fleet reads the ledger's DynamoDB stream and publishes committed events to
`events-queue`. Each worker holds leases on a share of the stream's shards
(`facaibank-<env>-outbox-leases`) and checkpoints the last sequence number it published.

**Symptoms:** `dynamodb-stream-iterator-age` in ALARM; customers report missing alerts.

1. Is the metric missing or high? Missing data alarms too, which usually means no worker is running.
2. Check the fleet: **EC2 > Auto Scaling groups > facaibank-&lt;env&gt;-outbox-workers**. Instances InService?
3. Session Manager into an instance:
   ```bash
   sudo systemctl status facaibank-outbox-worker
   sudo tail -100 /var/log/facaibank/outbox-worker.log
   ```
   Look for `AccessDenied` (IAM), `Lost lease` (another worker took over; fine), or repeated AWS errors.
4. Restart if needed: `sudo systemctl restart facaibank-outbox-worker`. It resumes from its checkpoint, so nothing committed is lost. Events published just before a crash may be published again; the notifier drops those duplicates.
5. Inspect leases:
   ```bash
   aws dynamodb query --table-name facaibank-<env>-outbox-leases \
     --key-condition-expression "PK = :p" --expression-attribute-values '{":p":{"S":"LEASE"}}'
   ```
   A lease whose `lease_expires_at` is in the past is free and will be picked up within about 10 seconds.

The stream keeps records for 24 hours. A worker outage longer than that loses notifications
(never money): the ledger is intact, but alerts for that window are not sent.

## Events DLQ (ticket)

A message lands in `events-dlq` after the notifier fails it 3 times: malformed JSON, a
missing `event_id`, or SNS errors.

1. Read the message without deleting it:
   ```bash
   aws sqs receive-message --queue-url "$(terraform output -raw events_dlq_url)" \
     --max-number-of-messages 10 --visibility-timeout 0 --attribute-names All
   ```
2. Find the cause in `/facaibank/<env>/lambda/notifier` (search for the message ID).
3. **Fixable** (for example, SNS was throttled): redrive to the source queue:
   **SQS > events-dlq > Start DLQ redrive**, or
   `aws sqs start-message-move-task --source-arn "$(terraform output -raw events_dlq_arn)"`.
   Redrive is safe because the notifier deduplicates.
4. **Poison message** (invalid payload, test message): note it in the ticket and purge it.
5. The alarm returns to OK when the DLQ is empty.

## Statement jobs (DLQ, oldest job)

1. Check `/facaibank/<env>/statement-worker` for `Statement job failed`.
2. Jobs that fail 3 times are marked `FAILED` on their record and moved to `statement-dlq`. Customers see "FAILED" and can request again.
3. After fixing the cause, redrive `statement-dlq` to `statement-jobs-queue`.
4. If jobs are only slow, check the backlog-per-worker scaling policy and `asg_max_size`.

## Recovery drill

```bash
python tests/recovery_test.py --execute
python tests/validate_all.py --execute     # includes the dedupe and DLQ exercise
```
