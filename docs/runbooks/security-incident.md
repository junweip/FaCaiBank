# Security alerts

The security topic receives: root-account use, IAM changes, KMS key changes, CloudTrail
changes and console sign-in without MFA (CloudTrail metric filters); GuardDuty findings of
severity 7 or higher; new HIGH/CRITICAL Security Hub findings; and Macie findings on the
KYC bucket.

## First 15 minutes

1. **Was it us?** Check the team chat for planned changes. A Terraform apply produces IAM and KMS change alarms; match the CloudTrail `userIdentity` to the deploy role (`facaibank-github-deploy`) or a known team member, and the time to a deployment.
2. If not explained, treat it as an incident. Record the time, alarm name and CloudTrail event ID.
3. Look up the event: **CloudTrail > Event history** (or Logs Insights on `/facaibank/<env>/cloudtrail`):
   ```
   fields eventTime, eventName, userIdentity.arn, sourceIPAddress, errorCode
   | filter eventSource in ["iam.amazonaws.com","kms.amazonaws.com","cloudtrail.amazonaws.com"]
   | sort eventTime desc | limit 50
   ```

## Containment

| Situation | Action |
|---|---|
| Root account used | Change the root password, check root MFA, look for new IAM users, access keys or roles. Root should never be used day to day. |
| Unknown IAM user, role or access key | Deactivate keys (`aws iam update-access-key --status Inactive`), detach policies, then investigate. |
| KMS key disabled or deletion scheduled | `aws kms cancel-key-deletion` / `enable-key`. Every encrypted store depends on this key. |
| CloudTrail stopped or changed | `aws cloudtrail start-logging --name facaibank-<env>-trail`, then re-apply Terraform to restore settings. |
| GuardDuty: instance credential exfiltration or crypto mining | Replace the instance: detach it from its Auto Scaling group with the "replace" option, keep it stopped for forensics, and snapshot its volume. |
| Macie: sensitive data in an unexpected place | Identify the object, check who wrote it (CloudTrail S3 data events), move or delete it after review. |

## After the incident

- The audit bucket uses Object Lock, so CloudTrail logs cannot be altered or deleted, even by an attacker with admin rights (in COMPLIANCE mode, not even by root).
- Write a short post-incident note: timeline, cause, fix, prevention. Mark the Security Hub finding as resolved.
- If customer data may have been exposed, the bank would have to notify MAS within 1 hour of discovery (MAS TRM). Escalate to the team lead immediately.
