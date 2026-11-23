# Deployment

Order matters. Bootstrap creates the state bucket, the persistent root creates
everything that survives teardown, and the ephemeral root is applied and
destroyed per session.

```
bootstrap  ->  persistent  ->  ephemeral  ->  first deploy  ->  drill  ->  teardown
```

## 0. Prerequisites

| Tool | Version | Check |
|---|---|---|
| Terraform | >= 1.10 | `terraform version` |
| AWS CLI | v2 | `aws --version` |
| Python | >= 3.11 | `python --version` |
| Docker | any recent | `docker version` |

Credentials come from a local profile. This project never reads access keys from
the repository.

```powershell
aws sts get-caller-identity
. .\scripts\env.ps1
```

```bash
aws sts get-caller-identity
source scripts/env.sh
```

## 1. Billing guard, before anything else

Do this first. The lab is cheap but a forgotten resource is not free.

1. AWS Console → **Billing** → **Budgets** → create a monthly cost budget of
   **$40** with alerts at 50%, 80%, and 100%.
2. Enable **Cost Anomaly Detection** with a $10 threshold.
3. Tag every resource. Terraform applies `Project`, `ManagedBy`, and `Root`
   automatically through `default_tags`.

## 2. Bootstrap the state bucket

Creates one S3 bucket with versioning, KMS encryption, public access blocked,
and TLS-only access. Costs nothing.

```powershell
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply
terraform -chdir=terraform/bootstrap output -raw backend_hcl |
  Set-Content terraform/persistent/backend.hcl
Copy-Item terraform/persistent/backend.hcl terraform/ephemeral/backend.hcl
```

The bucket has `prevent_destroy`. It is the one thing that is never torn down.

## 3. Pick a name and delegate it (90-minute timebox)

No domain is purchased. Register a free subdomain at a provider that supports
NS records on a shared domain, then delegate it to Route 53.

1. Create the subdomain, for example `drlab-demo.mooo.com`.
2. Apply the persistent root (next step) and read its nameservers:
   ```powershell
   terraform -chdir=terraform/persistent output zone_name_servers
   ```
   For this deployment they are `ns-665.awsdns-19.net`, `ns-2042.awsdns-63.co.uk`,
   `ns-181.awsdns-22.com`, and `ns-1241.awsdns-27.org`.
3. Add four `NS` records at the provider pointing at those nameservers.
4. Verify from outside Route 53:
   ```bash
   dig +short NS drlab-demo.mooo.com
   dig +short app.drlab-demo.mooo.com @ns-665.awsdns-19.net
   ```

If the provider refuses NS records, **stop and move on**. The drill measures
Route 53 behaviour through the zone's own authoritative nameservers, so
delegation is a fidelity bonus, not a gate. Record the outcome in
`docs/drills.md`.

## 4. Apply the persistent root

```powershell
terraform -chdir=terraform/persistent init -backend-config=backend.hcl
terraform -chdir=terraform/persistent apply
```

Creates:

- the hosted zone (`prevent_destroy`)
- the evidence bucket, versioned, KMS-encrypted, lifecycle-expired
- one ECR repository per region
- one SNS topic per region with an email subscription (confirm the email)
- the GitHub OIDC provider and two roles: narrow deploy, broad Terraform

Values live in `terraform/persistent/terraform.tfvars` (git-ignored). Copy
`terraform.tfvars.example` and fill in `domain_name` and `alert_email`.

## 5. Apply the ephemeral root

```powershell
terraform -chdir=terraform/ephemeral init -backend-config=backend.hcl
terraform -chdir=terraform/ephemeral plan
terraform -chdir=terraform/ephemeral apply
```

Creates, per region: a VPC with two public subnets and no NAT gateway, one
application instance with an Elastic IP, one RDS instance, security groups,
SSM parameters, a Route 53 health check, and CloudWatch alarms. The recovery
region's RDS instance is a cross-region read replica of the primary.

First apply takes 15–25 minutes, almost all of it RDS.

## 6. Deploy the first image

The application workflow does this on push. To do it by hand:

```powershell
# Build once for linux/amd64 with provenance off so both registries get the
# same single-platform digest.
$tag = "local-" + (Get-Date -Format "yyyyMMddHHmmss")
docker buildx build --platform linux/amd64 --provenance=false --sbom=false `
  -t "305678528024.dkr.ecr.us-east-1.amazonaws.com/drlab/app:$tag" --push app
$digest = (docker buildx imagetools inspect `
  "305678528024.dkr.ecr.us-east-1.amazonaws.com/drlab/app:$tag" `
  --format '{{json .Manifest}}' | ConvertFrom-Json).Digest

python scripts/deploy.py --digest $digest --source terraform
```

The script writes `/opt/drlab/image.env` over SSM, pulls the pinned digest,
restarts `drlab-app.service` (a `Type=oneshot` unit, so `start` on an active
service is a no-op), and waits for `/health`.

## 7. Configure GitHub

Create three environments under **Settings → Environments**:

| Environment | Purpose | Protection |
|---|---|---|
| `app-deploy` | image push and deploy | none |
| `terraform` | infrastructure apply and teardown | required reviewer |
| `drill` | disaster declaration | required reviewer |

Add these repository **variables** (not secrets — there are none):

| Variable | Example |
|---|---|
| `AWS_ROLE_APP_ARN` | `arn:aws:iam::123456789012:role/drlab-github-app-deploy` |
| `AWS_ROLE_TF_ARN` | `arn:aws:iam::123456789012:role/drlab-github-terraform` |
| `AWS_REGION_PRIMARY` | `us-east-1` |
| `AWS_REGION_DR` | `us-east-2` |
| `TF_STATE_BUCKET` | `drlab-tfstate-123456789012-us-east-1` |
| `DOMAIN_NAME` | `dr-demo.mooo.com` |
| `ALERT_EMAIL` | your address |

Values come from `terraform -chdir=terraform/persistent output`.

## 8. Verify before trusting it

```powershell
# Both instances answer
curl.exe "http://<primary-eip>:8080/health"
curl.exe "http://<dr-eip>:8080/health"

# The standby is alive but not writable
curl.exe "http://<dr-eip>:8080/ready"     # expect 503

# Replication is current
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReplicaLag \
  --dimensions Name=DBInstanceIdentifier,Value=drlab-replica \
  --start-time (Get-Date).AddMinutes(-10).ToUniversalTime().ToString("s") `
  --end-time (Get-Date).ToUniversalTime().ToString("s") `
  --period 60 --statistics Average --region us-east-2
```

Then run a baseline session for an hour and confirm the ledger appears:

```powershell
aws s3 ls s3://<evidence-bucket>/ledger/ --recursive | Select-Object -First 5
```

## 9. Tear down

```powershell
python scripts/teardown.py --yes
```

Destroys the ephemeral root and then runs `scripts/verify_empty.py`, which exits
non-zero if any tagged instance, database, Elastic IP, VPC, SSM parameter,
alarm, or health check survives in either region. Treat a non-zero exit as a
billing incident, not a warning.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `terraform init` complains about the backend | `backend.hcl` missing | regenerate it from the bootstrap output |
| First `destroy` fails on a final snapshot | `skip_final_snapshot` | already `true` in this code; check for hand-created databases |
| Replica creation fails | source has `backup_retention_period = 0` | already `1` here; check manual changes |
| `/health` returns 503 on the standby | replica is read-only | expected; `/ready` is the writable check |
| Health check permanently healthy | security group blocks Route 53 ranges | port 8080 is open to `0.0.0.0/0` by design |
| App boots then exits | SSM parameter missing in that region | this is the configuration drill; check `/drlab/<slug>/db_host` |
| Deploy succeeds but the app still runs the old image | `systemctl start` is a no-op on an active `Type=oneshot` unit | `deploy.py` uses `systemctl restart`; check the SSM command output |
| Standby `/state` reports a frozen `max_id` | a rejected insert left an open transaction on the read-only replica | fixed in `app.py` by rolling back on failure and after each read |
