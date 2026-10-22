# Restoring data

Targets (slide 4): RPO 0 and RTO within 10 minutes for an AZ failure. An AZ failure needs no
restore: DynamoDB is replicated across three AZs and RDS fails over to its standby
([rds-failover.md](rds-failover.md)). Restores are for logical damage: bad deployments,
accidental deletes, corrupted data.

## DynamoDB ledger (point-in-time recovery)

PITR is always on: you can restore to any second in the last 35 days.

1. Pick the restore time (UTC), just before the damage. CloudTrail shows when a bad write or delete happened.
2. Restore to a **new** table (you cannot restore in place):
   ```bash
   aws dynamodb restore-table-to-point-in-time \
     --source-table-name facaibank-ledger \
     --target-table-name facaibank-ledger-restore-YYYYMMDDHHMM \
     --restore-date-time 2026-10-22T03:15:00Z \
     --billing-mode-override PAY_PER_REQUEST
   aws dynamodb wait table-exists --table-name facaibank-ledger-restore-YYYYMMDDHHMM
   ```
3. Check the restored copy. The sum of all `SK = BALANCE` items must be 0 (clearing accounts balance the customer balances). `tests/restore_test.py` does this automatically.
4. Recover:
   - **A few items**: copy the correct items from the restored table back into `facaibank-ledger` with a script, after review by two team members. Write correcting ledger entries rather than editing history where possible.
   - **The whole table**: take an on-demand backup of the damaged table (`aws dynamodb create-backup`), stop the API and outbox fleets (set their Auto Scaling groups to 0), delete `facaibank-ledger`, restore again with `--target-table-name facaibank-ledger`, then run `terraform apply` to re-enable Streams, TTL, PITR and deletion protection on it, and scale the fleets back up. Restored tables don't keep Streams, so the outbox workers start on the new stream from its oldest record.
5. Delete the restored copy when you're done.

## RDS accounts database

Automated backups give point-in-time restore inside the `db_backup_retention_days` window (7 days).

1. Restore to a new instance:
   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier facaibank-dev-postgres \
     --target-db-instance-identifier facaibank-dev-postgres-restore \
     --restore-time 2026-10-22T03:15:00Z \
     --db-subnet-group-name facaibank-dev-data \
     --vpc-security-group-ids <rds security group id> \
     --no-publicly-accessible
   aws rds wait db-instance-available --db-instance-identifier facaibank-dev-postgres-restore
   ```
2. Connect from an API instance through Session Manager and compare the affected rows.
3. Either copy the corrected rows back, or replace the database: rename the damaged instance (`aws rds modify-db-instance --new-db-instance-identifier facaibank-dev-postgres-damaged --apply-immediately`), rename the restored one to `facaibank-dev-postgres`, then run `terraform apply` so settings and the managed secret are reconciled. The endpoint hostname follows the identifier, so the API reconnects without changes.
4. Delete the restored instance when done.

## Timed restore test

Run this at least once per term and keep the evidence for the report:

```bash
python tests/restore_test.py --execute              # DynamoDB only, a few minutes
python tests/restore_test.py --execute --include-rds  # adds an RDS restore (10-20 min)
```

The evidence file records the restore point, the time taken, and whether the restored
ledger matches the original item for item.
