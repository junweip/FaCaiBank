# FaCaiBank

A fully digital retail bank prototype on AWS (Singapore region), built for IS458 Cloud
Management and Engineering, Group 3. Customers sign up with MFA, verify their identity,
open accounts, move money, get transaction alerts and download PDF statements.

This README explains how to get access to our AWS account, run the site and shut it down
again. If you only want to run the unit tests, skip to [Run the tests](#run-the-tests-no-aws-needed).

---

## How this works (read this first)

**There is no single server you log in to.** The whole bank (network, web servers,
databases, queues, monitoring) is described as code in the `infra/` folder. A tool called
**Terraform** reads that code and creates the real resources in our group's AWS account.
Running it again updates them, and `terraform destroy` deletes them.

```
 your laptop                                our AWS account (ap-southeast-1)
┌──────────────────────┐   AWS access key   ┌────────────────────────────────────────┐
│ repo + Terraform     │ ─────────────────▶ │ creates/updates everything in infra/   │
│ AWS CLI (your login) │                    │                                        │
└──────────────────────┘                    │  S3 "state" bucket ◀── Terraform's     │
                                            │                        shared memory   │
 your browser ──── https://xxxx.cloudfront.net ──▶ website ─▶ API ─▶ databases       │
                                            └────────────────────────────────────────┘
```

Three ideas make it click:

1. **Your laptop talks to AWS with an access key.** You get a personal key for our account
   (step 1) and save it with `aws configure` (step 3). Terraform and the test scripts use
   it automatically.
2. **Terraform keeps "state":** its record of what it has already created. We store it in a
   shared S3 bucket, so whoever runs Terraform sees the same stack, and a lock stops two
   people changing it at the same time.
3. **You use the bank through its website URL**, not through the servers. The servers have
   no public IP addresses. If you ever need a terminal on one, use **Session Manager** in
   the AWS console ([step 7](#step-7-look-around-in-aws)).

---

## Step 1: Get access to the AWS account

We share **one AWS account** (the one with the USD 200 credit). The account owner gives
each member their own login. Don't share the root password.

**Account owner, once per member:**

1. Sign in to the [AWS console](https://console.aws.amazon.com/) and open **IAM → Users → Create user**.
2. User name: the member's name. Tick **Provide user access to the AWS Management Console** if they should use the website console too.
3. Permissions: **Attach policies directly → `AdministratorAccess`**. Terraform creates IAM roles, so narrower policies get in the way for a student project.
4. After creating the user, open it → **Security credentials → Create access key → Command Line Interface (CLI)**. Copy the **Access key ID** and **Secret access key**.
5. Send them to the member privately (not in the group chat history, and never in this repo). Ask everyone to turn on MFA for their user (**Security credentials → Assign MFA device**).

**Each member** ends up with:

- an Access key ID (starts with `AKIA…`) and a Secret access key
- optionally a console sign-in link `https://<account-id>.signin.aws.amazon.com/console`, a user name and a password

> Lost or leaked key? The account owner deletes it in IAM and creates a new one. Bots scan
> GitHub for keys within minutes, so never commit one.

---

## Step 2: Install the tools

| Tool | Why | macOS | Windows (PowerShell) |
|---|---|---|---|
| Terraform ≥ 1.10 | Creates the AWS resources | `brew install terraform` | `winget install Hashicorp.Terraform` |
| AWS CLI v2 | Stores your key; useful commands | `brew install awscli` | `winget install Amazon.AWSCLI` |
| Python 3.12 | Tests and test scripts | `brew install python@3.12` | `winget install Python.Python.3.12` |
| Git | Getting the code | preinstalled | `winget install Git.Git` |

Close and reopen the terminal afterwards, then check: `terraform -version`, `aws --version`.

---

## Step 3: Connect your laptop to our AWS account

```bash
aws configure --profile facaibank
#   AWS Access Key ID:     <your AKIA… key>
#   AWS Secret Access Key: <your secret>
#   Default region name:   ap-southeast-1
#   Default output format: json
```

Tell every terminal session to use that profile:

```bash
export AWS_PROFILE=facaibank            # macOS / Linux
$env:AWS_PROFILE = "facaibank"          # Windows PowerShell
```

Check it worked. You should see our account ID and your user name, not an error:

```bash
aws sts get-caller-identity
```

That's the whole "connection". Everything after this uses that profile.

---

## Step 4: First-time setup (one person, once per AWS account)

This creates the shared state bucket and lock table. **Check with the group first. If
someone already did it, skip to step 5.**

```bash
git clone https://github.com/junweip/FaCaiBank.git
cd FaCaiBank
git checkout la-changes          # or main, once this branch is merged

cd infra/bootstrap
terraform init
terraform apply                  # review, type: yes
cd ../..
```

---

## Step 5: Start the site

**5a. Point Terraform at the shared state** (each member, once per laptop):

```bash
cd infra
cp backend.hcl.example backend.hcl
aws sts get-caller-identity --query Account --output text   # prints our 12-digit account ID
```

Open `infra/backend.hcl` and replace `123456789012` with that account ID. It isn't secret,
but the file is git-ignored so everyone keeps their own copy. Then:

```bash
terraform init -backend-config=backend.hcl
```

**5b. Create (or update) the stack:**

```bash
terraform apply -var-file=dev.tfvars -var alert_email=you@example.com
```

- Terraform lists everything it will create; type `yes`. The first run takes **15–25 minutes** (CloudFront and the database are slow to create). Later runs take a few minutes.
- AWS emails `alert_email` some "Subscription confirmation" messages. Click them to receive alarms.
- If it fails saying **GuardDuty / Security Hub / Macie / Config is already enabled**, open `infra/dev.tfvars`, set that `enable_…` line to `false`, and run the same command again.

**5c. Get the address:**

```bash
terraform output website_url       # e.g. https://d1a2b3c4.cloudfront.net
```

Give the servers about **5 minutes** after `apply` finishes to install and start. Until
then the site may show errors.

---

## Step 6: Use the site

1. Open the `website_url` and choose **Create account**. Enter your email and a password (12+ characters with upper and lower case, a number and a symbol).
2. Enter the 6-digit code emailed to you.
3. Scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, …) and enter its code. You need this app every time you sign in.
4. Enter your name, then **Verify now** and upload any small PDF, PNG or JPG. Identity verification is simulated.
5. **Open an account**, then **Add money**: bank **DBS**, account number **123456789**, any amount up to 20,000.
6. Try a transfer (open a second account, or use a teammate's account number from the ⧉ button), request a statement, and turn on email alerts with the bell icon.

Good to know for demos:

- Transfers of **S$1,000 or more** ask for your password and authenticator code again (step-up security).
- The mock partner bank declines external account numbers ending in **0000** and amounts over **S$20,000**. A declined withdrawal shows the money being returned.
- Transfers of **S$10,000 or more** are blocked by fraud screening.

---

## Step 7: Look around in AWS

Sign in to the console with your link from step 1 and make sure the region (top right)
is **Asia Pacific (Singapore)**.

| What | Where in the console | Or run |
|---|---|---|
| Health and traffic dashboard | CloudWatch → Dashboards → `facaibank-dev` | `terraform output dashboard_url` |
| Web, API and worker servers | EC2 → Auto Scaling Groups | `terraform output asg_names` |
| Ledger (balances, transactions) | DynamoDB → Tables → `facaibank-ledger` → Explore items | |
| Customer accounts database | RDS → Databases → `facaibank-dev-postgres` | |
| Logs from each service | CloudWatch → Log groups → `/facaibank/dev/…` | |
| Alarms | CloudWatch → All alarms | |
| Security findings | Security Hub, GuardDuty | |
| Cost so far | Billing → Bills, and Budgets | |

**Terminal on a server:** EC2 → Instances → pick one → **Connect → Session Manager →
Connect**. Useful commands there:

```bash
sudo systemctl status facaibank-backend           # or facaibank-outbox-worker, facaibank-statement-worker
sudo tail -50 /var/log/facaibank/backend.log
```

---

## Step 8: Stop it when you're done (saves our credit)

The dev stack costs about **USD 6.50 per day** while it exists, whether or not anyone uses
it, so a whole month uses the entire USD 200 credit. Delete it at the end of each session:

```bash
cd infra
terraform destroy -var-file=dev.tfvars            # type: yes
```

Bring it back any time with step 5b. Destroying deletes the data (test customers and
balances), but that's fine for a prototype. If `destroy` stops at the audit bucket, see
[docs/runbooks/cleanup.md](docs/runbooks/cleanup.md). Don't destroy `infra/bootstrap`: it's
the shared state and costs almost nothing.

---

## Working on the stack as a group

- **Say it in the group chat before `apply` or `destroy`.** It changes the stack for everyone.
- **Pull the latest code before `apply`.** Terraform makes AWS match the code on *your*
  laptop, so applying an old copy undoes teammates' changes.
- If Terraform says the state is **locked**, someone else is running it. Wait for them to finish.
- Change `infra/dev.tfvars` (and commit it) rather than clicking settings in the console.
  Console changes get overwritten by the next `apply`.
- Once the repo's GitHub environment is set up ([infra/README.md](infra/README.md), step 4),
  you can deploy by pushing a tag such as `v1.0.0` instead of running `apply` yourself.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Unable to locate credentials` or `ExpiredToken` | Run step 3 again and set `AWS_PROFILE` in this terminal |
| `Backend configuration changed` / `please run terraform init` | `terraform init -backend-config=backend.hcl` |
| `Error acquiring the state lock` | Someone else is applying. Wait; only if you're sure nobody is: `terraform force-unlock <ID>` |
| `…already exists` (bucket, table, role) | The stack, or an older copy of it, already exists in the account. Ask the group before deleting anything |
| Site shows 502/503 right after apply | Servers are still starting; wait 5 minutes |
| `Direct access is not allowed` | You opened the load balancer address. Use `website_url` |
| No verification email | Check spam; Cognito sends from `no-reply@verificationemail.com` |
| Authenticator code rejected | Wait for the next code; your phone clock must be on automatic time |

---

## Run the tests (no AWS needed)

```bash
python3.12 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\Activate.ps1
pip install -r app/backend_api/requirements-dev.txt
python -m pytest -q
```

The tests emulate DynamoDB, S3, SQS and SNS on your laptop, so they cost nothing. To test a
deployed stack (load, failover, restore, access tests for the report), see
[docs/validation.md](docs/validation.md).

---

## What's in this repository

| Path | What it is |
|---|---|
| [app/backend_api](app/backend_api) | Banking API (FastAPI): sign-in checks, ledger transactions, mock partner bank, fraud scoring, statements, alerts |
| [app/frontend](app/frontend) | The website |
| [app/workers](app/workers) | Background workers: notification outbox and PDF statements |
| [app/lambda_*](app) | Small serverless functions: customer alerts, Slack alerts, AI ops assistant |
| [app/fraud_model](app/fraud_model), [app/analytics](app/analytics) | Fraud model (SageMaker) and analytics job (Glue) |
| [infra](infra) | Terraform code for the whole stack ([infra/README.md](infra/README.md) has the details and options) |
| [tests](tests) | Unit tests and scripts that test a deployed stack |
| [docs](docs) | Runbooks, cost review, validation guide, proposal-to-code map |

Further reading: [ARCHITECTURE.md](ARCHITECTURE.md) (design rules and deviations from the
proposal), [docs/traceability.md](docs/traceability.md) (each proposal slide → code),
[docs/cost-review.md](docs/cost-review.md) (what it costs).
