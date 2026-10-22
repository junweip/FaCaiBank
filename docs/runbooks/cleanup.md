# Cleaning up a stack

Destroy the stack whenever nobody is working on it. Idle, the dev profile costs about
USD 6–7 per day ([cost review](../cost-review.md)).

```bash
cd infra
terraform destroy -var-file=dev.tfvars   # use the profile you applied
```

Things that need attention:

- **Audit bucket (Object Lock):** CloudTrail and Config objects are retained for 5 years. With `GOVERNANCE` mode (the dev default) an administrator can remove them:
  ```bash
  aws s3api list-object-versions --bucket <audit bucket> --query 'Versions[].{Key:Key,VersionId:VersionId}' --output json > versions.json
  # for each version:
  aws s3api delete-object --bucket <audit bucket> --key <key> --version-id <id> --bypass-governance-retention
  ```
  Then destroy again. In `COMPLIANCE` mode nobody can delete them until retention ends. Leave the bucket; it costs cents per month in Glacier Deep Archive.
- **KYC bucket:** versioned, so delete all object versions first, or empty it in the S3 console.
- **KMS key:** scheduled for deletion with a 7-day window. Cancel it if you redeploy within that week and need older backups.
- **Account-level services:** GuardDuty, Security Hub, Macie and Config are switched off by destroy. Re-enabling them on the next apply restarts free trials only once per account.
- **Shared state (bootstrap):** keep it. It costs almost nothing and holds the history of every deployment.
