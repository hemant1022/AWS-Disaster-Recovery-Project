# Configuration

Three places hold configuration, and each has one job. Nothing is duplicated between them.

| File | Holds | Committed |
|---|---|---|
| `.env` | Values the shell and CLI need: profile, default region, project name, state bucket name | No — `.env.example` is committed |
| `terraform/<root>/terraform.tfvars` | Every input to that Terraform root | No — `terraform.tfvars.example` is committed |
| `terraform/<root>/backend.hcl` | State bucket coordinates for that root | No — `backend.hcl.example` is committed |

Load the shell values before running anything:

```powershell
. .\scripts\env.ps1
```

```bash
source scripts/env.sh
```

## Terraform inputs

### bootstrap

| Variable | Default | Meaning |
|---|---|---|
| `project` | `drlab` | Name prefix and cost tag |
| `state_region` | `us-east-1` | Region for the state bucket |

The bootstrap root uses local state. It creates one bucket and nothing else, so its state file holds no secrets. Keep it.

### persistent

| Variable | Default | Meaning |
|---|---|---|
| `project` | `drlab` | Name prefix and cost tag |
| `primary_region` | `us-east-1` | Application region |
| `dr_region` | `us-east-2` | Recovery region |
| `domain_name` | none | Public zone name, e.g. `dr-demo.mooo.com`. Must match the FreeDNS subdomain you delegate. |
| `alert_email` | none | SNS subscriber for CloudWatch alarms |
| `github_repo` | `hemant1022/AWS-Disaster-Recovery-Project` | Used to pin the OIDC trust policy |
| `github_environment_app` | `app-deploy` | GitHub Environment allowed to push images and deploy |
| `github_environment_terraform` | `terraform` | GitHub Environment allowed to run Terraform |
| `evidence_retention_days` | `30` | Lifecycle expiry for drill evidence |

Changing `domain_name` after the zone exists recreates the zone, which assigns new nameservers and breaks the FreeDNS delegation. Pick one name and keep it.

### ephemeral

Added with the workload. It will hold the session identifier, instance size, and network ranges.

## Secrets

There are no secrets in `.env`, `terraform.tfvars`, or `backend.hcl`. There are exactly two:

1. **The database password.** Stored in SSM Parameter Store as a `SecureString` in both regions, and read by the instance role at boot. It does reach Terraform state through `aws_db_instance.password`, which is why the state bucket uses KMS encryption, blocks all public access, and rejects non-TLS requests.
2. **The GitHub OIDC trust.** No stored credential. GitHub mints a short-lived token, AWS exchanges it for temporary credentials, and the role's trust policy pins the exact repository and environment.

The local AWS identity is an IAM user, not SSO. That is acceptable for a laptop but it is a long-lived credential. Do not copy its keys anywhere, and do not add them to GitHub.

## SSM parameters

Every parameter exists in both regions under the same name, because the application reads from its own region:

```
/drlab/use1/db_host
/drlab/use1/db_name
/drlab/use1/db_user
/drlab/use1/db_password      (SecureString)
/drlab/use2/db_host
/drlab/use2/db_name
/drlab/use2/db_user
/drlab/use2/db_password      (SecureString)
```

One drill deliberately deletes a single parameter in `us-east-2` and starts the DR instance. The application fails to boot for a reason unrelated to the database, which is the point: configuration is state that needs replication too.

### Deployment parameters

The ephemeral root also publishes where to deploy, so the pipeline does not need
Terraform state:

```
/drlab/deploy/primary_region
/drlab/deploy/dr_region
/drlab/deploy/primary_instance_id
/drlab/deploy/dr_instance_id
/drlab/deploy/primary_eip
/drlab/deploy/dr_eip
/drlab/deploy/app_port
/drlab/deploy/app_fqdn
/drlab/deploy/evidence_bucket
```

`scripts/deploy.py` reads these by default. The application role can read
`/drlab/*`; nothing else can write it.

## Backend configuration

`terraform/persistent` and `terraform/ephemeral` keep their state in the bucket created by `bootstrap`. The bucket name depends on the account ID, so it is not hardcoded. After bootstrap, write it into a local `backend.hcl`:

```powershell
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply
terraform -chdir=terraform/bootstrap output -raw backend_hcl | Set-Content terraform/persistent/backend.hcl
Copy-Item terraform/persistent/backend.hcl terraform/ephemeral/backend.hcl
terraform -chdir=terraform/persistent init -backend-config=backend.hcl
```

`use_lockfile = true` means no DynamoDB table. State locking happens through the S3 object itself, which requires Terraform 1.10 or newer.
