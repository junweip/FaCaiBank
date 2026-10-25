# Deploying and rolling back

## How a release works

1. Every change merges to `main` through a pull request; **CI** runs the unit tests, the frontend syntax check and `terraform validate`.
2. Tag the release: `git tag v1.3.0 && git push origin v1.3.0` (or run **Deploy** manually with a ref).
3. **Deploy** assumes the `facaibank-github-deploy` role through OIDC (no stored AWS keys), runs `terraform plan` against the shared state, saves the plan as a build artifact, waits for the `dev` environment approval, and applies.
4. Application code ships as content-addressed bundles in the artifacts bucket. A code change creates a new launch template version, and each Auto Scaling group runs an **instance refresh**: new instances launch and must pass health checks before old ones terminate (`min_healthy_percentage = 100`), so there's no downtime even with one instance per fleet.
5. The workflow polls `/api/health` until it reports the new version, then writes the URL to the job summary.

`app_version` is stamped on instances (`AppVersion` tag), the `/health` response and the
site footer, so you can always see which release is live.

## Rolling back

**Automatic:** if new instances fail health checks during the refresh, Auto Scaling stops
and rolls back to the previous launch template (`auto_rollback = true`).

**Manual (bad behaviour that passes health checks):**

1. GitHub > Actions > **Deploy** > *Run workflow*, `ref` = the previous good tag (for example `v1.2.0`).
2. The workflow redeploys that commit: Terraform points the launch templates back at the older bundles (still in the artifacts bucket) and the instance refresh replaces the fleet.
3. Confirm the version on `/api/health` and watch the dashboard for 15 minutes.

Rollback redeploys infrastructure from that commit too. If the bad release included a data
migration (new SQL file or ledger item shape), check that the older code tolerates it
before rolling back. Migrations here only add columns, so older code keeps working.

**Emergency (CI unavailable):**
```bash
git checkout v1.2.0
cd infra && terraform init -backend-config=backend.hcl
terraform apply -var-file=dev.tfvars -var app_version=v1.2.0
```

## Rolling back Terraform state

The state bucket is versioned. If state itself is damaged, restore the previous version of
`facaibank/dev/terraform.tfstate` from the S3 console (*Show versions*) while nobody runs
Terraform. A lock file next to the state in S3 stops two applies running at once; release a stuck lock
with `terraform force-unlock <id>` only when you are sure no apply is running.
