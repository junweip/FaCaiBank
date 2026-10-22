# RDS failover

The accounts database (customers, accounts, KYC status) runs on RDS PostgreSQL. Balances
and the ledger are in DynamoDB and are not affected by an RDS failover.

## What happens automatically

With `rds_multi_az = true`, RDS keeps a synchronous standby in a second AZ. If the primary
or its AZ fails, RDS promotes the standby and moves the DNS name, usually in 60–120 seconds.
The API's connection pool reconnects to the same hostname. During the switch:

- Balance, history and transfers keep working (DynamoDB and cached profiles).
- Sign-up, opening accounts, KYC and anything that needs a profile not yet cached returns errors for about 1–2 minutes.

## When the RDS event page arrives

1. Confirm it's a failover: **RDS > Databases > facaibank-&lt;env&gt;-postgres > Logs & events** shows "Multi-AZ instance failover started/completed".
2. Watch the dashboard: API 5xx should drop back within about 2 minutes.
3. If errors continue after RDS reports *Available*:
   - Check the API logs for `password authentication failed`. A failover doesn't change credentials, but a coinciding secret rotation does; the pool re-reads the secret automatically within 5 minutes. To force it, restart the fleet with an instance refresh.
   - Check the security group: RDS must allow 5432 from the API and statement-worker groups.
4. Record the start and end times in the incident log. Downtime counts against the 52-minute yearly budget (99.99%).

If the stack runs single-AZ (`dev.tfvars`), a failure means RDS replaces the instance or
you restore it ([restore.md](restore.md)). That is a known gap of the cost-saving profile.

## Failover drill

```bash
terraform apply -var-file=full.tfvars          # or set rds_multi_az = true
python tests/failover_test.py --execute rds
```

The drill reboots the primary with failover while probing `/me` (database-backed) and the
balance endpoint (ledger) every second, then records the outage window of each and the AZ
change. To simulate losing a whole AZ for the compute tier:

```bash
python tests/failover_test.py --execute az --zone ap-southeast-1a   # needs asg_min_size >= 2
```

## Replica lag

The read replica (AZ 3) serves statements and reports. Lag above 5 seconds raises a ticket.
Statements only show account-holder details from the replica, never balances, so lag delays
nothing customer-critical. If lag keeps growing: check replica CPU and storage, and whether
a long query is running (`pg_stat_activity`). Workers fall back to the primary if you
remove `DATABASE_READ_HOST`.
