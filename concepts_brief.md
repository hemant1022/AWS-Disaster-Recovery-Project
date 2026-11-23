# Measured Multi-Region DR Lab

* **Author:** hemant1022
* **Date:** 2026-11-21

## Overview

A Terraform repository that stands up a small Dockerized event-sequence app on EC2 in two AWS regions, with an RDS MySQL primary and a cross-region read replica, an S3 bucket for state and evidence, and Route 53 failover records. It runs a baseline session, one combined regional-failover drill with a per-layer timeline, and one failback drill. It measures RPO and RTO with stated definitions, publishes the numbers, then destroys the workload.

Application delivery runs through a small GitHub Actions pipeline: tests, image build, push to ECR in both regions, and deployment over SSM. Infrastructure changes go through `plan` on pull request and `apply` behind a protected environment. The drills are triggered from the same pipeline, and the environment approval is the human gate. CloudWatch alarms on replication lag, health-check status, and a custom RPO-gap metric provide the detection event that the RTO measurement starts from.

The audience is the author first (learning) and a cloud/DevOps/SRE hiring process second. For the second audience, the artifact that matters is not the Terraform. It is a table of measured numbers next to expected numbers, next to what broke.

The project is reframed during review from "a DR lab" to **a recovery-time measurement harness with a priced architecture decision attached**. That distinction drives every decision below, because a lab that produces unverifiable numbers is worse than a demo, and a measurement project invites scrutiny that a demo does not.

## Analysis

### The measurement is the product, and as specified it cannot be measured

Six of seven review lenses independently found the same defect. "RPO from missing sequence numbers" is not computable as designed.

The reference point — the highest sequence number the primary acknowledged — lives inside the process and the database you just killed. Reconstruct it from the survivor and RPO comes out near zero *by construction*, because you are measuring loss from the thing that survived. A second error compounds it: InnoDB burns auto-increment values on rollback, so a gap in the sequence is not the same thing as a lost write. A third: if the app keeps writing during failover, you cannot separate lost writes from writes that landed in the wrong region.

Resolution — a three-count ledger, recorded outside both regions:

- **attempted** — client-side counter, appended to the persistent S3 bucket with a timestamp, fsynced per batch
- **committed** — highest sequence the primary acknowledged, plus the primary's binlog position polled on a timer into the same witness store
- **replicated** — highest sequence present on the promoted replica at promotion

RPO = committed − replicated. Rejected writes become a separate column labelled availability, not data loss. Gaps are reported as gaps and explicitly not equated with loss.

RTO has the same problem in a different place. "Wall-clock" measures when the author typed the command. It has no start event, no stop event, no clock source, and no prober location. If the prober runs in the active region it dies with the patient.

Resolution — RTO is reported as four segments, always: **detect → decide → act → verify**. The start event is the alarm firing, not the command. The stop event is a sustained successful write on the new primary for a stated duration, not a single HTTP 200. Two numbers are published separately and never averaged: time-to-detection and time-to-service. Human decision time is either inside the number or explicitly excluded, and the brief says which.

### The drill taxonomy does not match the failure mechanisms

The hypothesis proposed three drills: app tier, data tier, control plane. Two of them are the same drill.

Without an ALB, the only way traffic leaves a dead instance is the Route 53 health check. So "kill the app instance" and "flip DNS" are one mechanism observed twice. Separately, the data tier is not DNS-routable at all: RDS owns its endpoint, and a promoted replica keeps *its own* endpoint — the endpoint that disappears is the old primary's. That means failover needs no database reconfiguration if the region-2 app is pre-pointed at the region-2 replica. The endpoint problem belongs to failback, not failover.

Resolution — re-taxonomise by mechanism, then collapse:

1. **Baseline** (no failure) — steady-state write rate, replication-lag distribution at p50/p95/max, and proof the harness works before it is trusted.
2. **Combined regional failover** — one scripted sequence that fences the primary, captures replication position, promotes the replica, verifies writability, flips DNS, and stops the clock. Reported as a per-layer waterfall within a single run, which is both cheaper and more legible than three separate sessions.
3. **Failback** — a separate drill with a separate number.

Naming each drill by fault and assertion, not by tier: *"Terminate primary RDS, promote cross-region replica; assert no acknowledged write is lost; expected RTO 5–15 min."* A reader can then predict the outcome and judge whether the result is good.

### Failover has no enforced order, no fencing, and no owner for "which region is active"

Active region is currently implied by three facts that can disagree: the DNS record, which MySQL instance is writable, and what the EC2 user-data was baked with. Nothing reconciles them, so a half-finished failover is indistinguishable from a finished one, and re-running a step toggles rather than sets.

Two concrete failure modes follow. Flip DNS before promotion and region 2's app connects to a read-only replica, recording RTO against a target that cannot accept writes. Promote without fencing region 1 and two writable primaries exist; region-1 writes continue into a database declared dead and are silently absent from the promoted copy, so the RPO number contains writes that were never lost, only misrouted.

Resolution — one persisted record in the surviving S3 bucket, versioned, written with a conditional put:

`{active_region, promotion_epoch, last_known_replication_position, updated_at}`

DNS, database role, and app config become followers of that record. Every log line, object key, and database row carries the epoch. Disaster declaration is a compare-and-swap on the epoch, so a second call is a no-op. Fencing is an explicit step in the sequence, not an afterthought. Nothing except a declaration is allowed to flip DNS.

### Failback is a different procedure, not the inverse of failover

Failover is roughly two steps. Failback, done safely, is five: destroy or permanently fence the old primary, create a new region-1 replica from the region-2 primary, wait for lag zero, fence region 2, promote region 1, flip DNS. The old primary cannot be re-promoted — it has diverged, and async replication without a rewind path cannot reattach it. "Failback" therefore means **rebuild and fail back**.

It is also the most underestimated item in the plan, by 4–8×. Reverse replication depends on binary-log retention on the promoted instance, which is not enabled by default and may be purged before it is needed. When it is unavailable, snapshot restore is the only path, and its RTO is materially worse. That is an honest number and should be published as such.

Consequence for the schedule: failback is moved *before* the write-up weekend so a lost weekend is absorbed by documentation rather than by measurements, and it is prototyped by hand before any Terraform exists.

### FreeDNS delegation is an unverified external dependency carrying a third of the project

FreeDNS supports an NS record type, but delegation on a shared subdomain depends on whether that domain's owner enabled it. The free tier has no reliable API for this. If the chosen domain refuses NS records, the Route 53 zone is authoritative but unreachable, and "public DNS failover" becomes a diagram.

Resolution — decouple the drill from the delegation. Measure control-plane behaviour by querying the zone's own authoritative name servers directly (`dig @ns-xxx.awsdns-xx.com app.<domain>`), which tests Route 53 failover with no third party involved. Delegation becomes a fidelity bonus with a 90-minute timebox in week 1, not a gate. Two further constraints: the hosted zone must live in the persistent root with `prevent_destroy`, because a recreated zone gets four new nameservers and silently breaks delegation; and health checks must target the Elastic IP directly, never a name that resolves through the failover record, which would be circular.

Even when it works, the control-plane number is close to tautological — it equals the TTL you chose plus the health-check interval times the failure threshold. It is reported as a **sensitivity table** (TTL 60 vs 300, interval 10 vs 30) so it teaches a tradeoff instead of merely existing.

### The persistence boundary is drawn in the wrong place, and teardown is the real inner loop

"Destroy everything except S3 and the hosted zone" produces drift and destroy-order failures. S3 cross-region replication is not two buckets; it needs versioning on both, a replication config, and an IAM role. Versioned buckets cannot be deleted while versions exist. `aws_db_instance` defaults `skip_final_snapshot = false`, so the first destroy fails without a final snapshot identifier. Deleting a source before its cross-region replica can leave the replica promoted and untracked, leaking a running MySQL instance between sessions. After a data-tier drill promotes the replica, Terraform state is a lie and the next plan wants to recreate a database that is serving traffic.

Resolution — two Terraform roots with separate state:

- `persistent/` — state bucket, hosted zone, evidence bucket, ECR repositories. Applied once, never destroyed, `prevent_destroy` on the zone.
- `ephemeral/` — VPC, EC2, RDS, replica, failover records, health checks. Applied and destroyed per session.

Plus `skip_final_snapshot = true`, `deletion_protection = false`, `force_destroy` on ephemeral buckets, an explicit teardown order (replica → primary → instances), and a `verify-empty` script that queries both regions and exits non-zero if anything survives. That script is the regression test, and it is the week-1 acceptance criterion.

Drills mutate infrastructure out of band. The reset path is destroy and re-apply, never drift repair. A `terraform apply` freeze applies for the duration of every drill.

### The scope does not fit the calendar

Realistic effort is 64–99 hours against a capacity of 48–72 (six weekends at 8–12 hours). The naive estimate was 30. The largest gaps: failback 4–8×, two-region Terraform 2–2.5×, the measurement harness 2–3×, and the write-up 2–3×. The write-up is the portfolio artifact and is always the thing squeezed.

Money is not the binding constraint. Drills cost roughly $20–25 in total; $100 has fivefold headroom if teardown holds. The odds of exceeding it are 10–15%, almost entirely from one forgotten resource — and the single worst offender is a **NAT gateway at ~$32/month**, which appears by default in most small-VPC Terraform examples. No NAT gateway. Public subnets with security groups.

### CI/CD is in scope, but it must not fight the drills

Adding a pipeline does not conflict with the measurement goal. It conflicts with the drill mechanics, and the conflict has one clean resolution: **the pipeline owns the persistent root and the application image; humans own the ephemeral root and every fault injection.**

Five workflows, deliberately separate:

| Workflow | Trigger | Does |
|---|---|---|
| Application CI/CD | push to main | test, build image, push to ECR in both regions, pre-pull on standby, deploy to primary over SSM |
| Infrastructure plan | pull request | fmt, validate, tflint, checkov, `terraform plan` posted as a comment |
| Infrastructure apply | manual dispatch, protected environment | apply the persistent root only; apply the ephemeral root for a named session |
| DR drill | manual dispatch, protected environment | declare, fence, promote, flip DNS, validate, record |
| Teardown | manual dispatch | destroy the ephemeral root, run `verify-empty` |

The ephemeral root is never applied on push. A pipeline that applies continuously would revert every fault injection within one run, and the drill would measure Terraform's reconciliation instead of the system's recovery.

Mechanics that matter:

- **Push the image to both regional ECR repositories in the same workflow.** ECR is regional. A DR instance in us-east-2 that pulls from us-east-1 adds minutes to RTO and depends on the region that may be failing. Cross-region pull is also charged. Two pushes is simpler than ECR replication and removes the race where the standby boots before the image lands.
- **Pin by digest, not tag.** "Same image version in both regions" is only true if both sides resolve the same digest.
- **Pre-pull on the standby instance at deploy time.** Image pull must not be inside the RTO measurement. The failover step starts a pre-pulled image; it does not fetch one.
- **Deploy over SSM, not SSH.** `aws ssm send-command` runs `docker compose pull && up -d`, then polls `/health`. No bastion, no key management, no instance replacement. The instance profile needs `AmazonSSMManagedInstanceCore` and `AmazonEC2ContainerRegistryReadOnly`.
- **Pin the OIDC trust policy to a specific subject.** `repo:owner/repo:ref:refs/heads/main` or `repo:owner/repo:environment:production`. A wildcard `repo:owner/repo:*` lets any branch, and any pull request from a fork under `pull_request_target`, assume the role.
- **Two roles, not one.** A narrow role for application deploy (ECR push, SSM send-command) and a separate broad role for Terraform. Sharing one role gives the app pipeline infrastructure-destroy rights.
- **Use a protected GitHub Environment as the disaster declaration.** The approval step is the human gate the design already requires, and it produces an audit record for free. The epoch write happens after approval, never before.

Two limits to state in the write-up. The synthetic app has almost nothing worth testing, so the test stage is structure rather than substance. And there is no staging environment: the ephemeral root is the staging environment, and it is destroyed after each session.

Cost is zero. Public repositories get unlimited Actions minutes, OIDC removes stored credentials, and ECR storage for a small image is cents per month.

### Monitoring must not live in the region that fails

The four-segment RTO needs a detection event, and the plan had none. The minimum that fixes it costs about a dollar a month:

- **Alarms on the signals that matter.** `AWS/RDS ReplicaLag` on the replica, `AWS/Route53 HealthCheckStatus` below one, EC2 instance status checks, and one custom metric published by the harness carrying the current RPO gap and the last acknowledged sequence number.
- **A dashboard defined in the repository** as JSON through `aws_cloudwatch_dashboard`, so it is created and destroyed with the stack instead of clicked together in the console.
- **A notification path that survives the failure it is reporting.** CloudWatch alarm actions can only target an SNS topic in the same region. A single topic in us-east-1 goes quiet at exactly the moment it is needed. Create the topic in both regions and subscribe the same email endpoint to each.

Two details decide whether the numbers are trustworthy:

- Route 53 health-check metrics exist only in us-east-1, whatever region the endpoint sits in. Alarms and dashboard widgets for them must be created there.
- The detection segment of RTO is read from `describe-alarm-history`, using the alarm's state-transition timestamp, not from a stopwatch started by the operator. That is what makes "detect" a measured segment rather than an estimate.

Missing-data behaviour must be set explicitly on every alarm. When a replica is promoted, `ReplicaLag` stops being emitted, and a default can turn that into a false breach or a silent pass. Decide per alarm whether missing means healthy, unhealthy, or unknown, and write the choice down.

Cost: ten alarms, three dashboards, and ten custom metrics sit inside the free allowance. Beyond that, alarms are $0.10 each per month and dashboards $3 each. The whole setup is under a dollar.

### What the plan still fails to demonstrate

- **Observability breadth.** Alarms, one dashboard, and an email path exist, but there is no log aggregation, no tracing, and no SLO. Logs die with the instances unless exported before teardown.
- **Security review depth.** Instance roles are scoped tightly, but the Terraform principal is deliberately broad and the pipeline is authored and approved by one person.
- **Sustained operation.** Destroy-after-every-session is right for cost and wrong for evidence. The mitigation is deliberate artifact capture, not longer uptime.
- **Collaboration.** The pipeline creates pull requests, plan comments, and approvals, but all three come from the same person. That is a process artifact, not a review.

## Goals vs. Non-Goals

**In-scope goals**

- Baseline session producing a replication-lag distribution under a seeded, fixed-rate workload.
- One combined regional-failover drill with a per-segment timeline: detect, decide, act, verify.
- One failback drill, documented as asymmetric, with its own RTO.
- RPO computed from an off-region ledger: attempted, committed, replicated.
- A measured restore of the real database dump into local Docker MySQL, timed, because restore is the segment a senior reviewer asks about first.
- Persistent foundation root, ephemeral workload root, and a passing `verify-empty` check.
- A GitHub Actions pipeline that tests, builds, pushes the image to ECR in both regions by digest, and deploys to the primary over SSM.
- Infrastructure `plan` on every pull request with a policy gate, and `apply` behind a protected environment.
- The DR drill triggered from the same pipeline, with environment approval acting as the disaster declaration.
- CloudWatch alarms on replication lag, health-check status, instance status, and a custom RPO-gap metric, with a dashboard defined in the repository and an SNS path that survives a regional failure.
- Results table, runbook, diagrams, and raw drill logs committed to the repository.
- A small priced comparison: single-region multi-AZ vs. two-region warm standby vs. Aurora Global Database (priced only, not built), with achievable RPO/RTO and operational burden.
- A permanent drill ledger served as a static S3 site, so the evidence survives teardown and the DNS records never dangle at destroyed IPs.

**Out-of-scope non-goals**

- EKS, MSK, AWS DRS, Cloudflare, ALBs, GPU workloads.
- Aurora Global Database (priced, not deployed).
- Jenkins, Kubernetes, Argo CD, or any deployment platform beyond GitHub Actions and SSM.
- Log aggregation and distributed tracing platforms. Logs are exported to the repository before teardown.
- A staging environment. The ephemeral root serves that role and is destroyed.
- Automatic deployment to the secondary region outside a drill.
- Automatic failover. Promotion is manual and scripted; the project states this explicitly rather than implying managed failover.
- Multi-AZ in either region.
- Real incident response. Scheduled, self-inflicted, known-cause failures are rehearsals, and the write-up says so.
- Restore from RDS snapshot as a primary path. Declared a non-goal in one line.

## Alternatives Weighed

- **Rejected by author:** EKS and MSK — cost and complexity exceed a $100 budget with no learning gain over EC2 and RDS.
- **Rejected by author:** AWS DRS — no free tier, ~$40/month per protected server, and block-level replication can be learned from documentation without a standing bill.
- **Rejected by author:** Cloudflare Load Balancing — $5/month plus $0.50 per origin, and it only pays off in front of an ALB that costs another $16/month per region.
- **Rejected by author:** purchasing a domain — deferred to a $10–15/year contingency rather than a line item.
- **Rejected by author:** ALBs — Route 53 pointed at Elastic IPs demonstrates the same runbook mechanics for $33/month less.
- **Rejected by author:** Jenkins and Kubernetes — the deployment platform would become the project.
- **Rejected during review:** Argo CD and other GitOps controllers — Argo CD is a Kubernetes delivery controller, not a monitoring system. It cannot see RDS replication lag, Route 53 health-check state, or S3 replication, so a green dashboard would coexist with a broken data path. It also requires a cluster (EKS at roughly $73/month per region, or k3s at roughly $15/month per region), and a controller running in the primary region fails with the region it is meant to recover.
- **Rejected during review:** scheduled `terraform plan` drift detection — useful in principle, but it adds a workflow to a calendar that is already over capacity, and it detects configuration drift rather than the recovery signals the drills measure.
- **Rejected during review:** three separate tier drills — app-tier and control-plane drills share one mechanism, and the data tier is not DNS-routable, so three sessions produce overlapping, non-comparable numbers.
- **Rejected during review:** S3 cross-region replication as a phase-1 drill — four hours of IAM and destroy-order work for a metric that reads "0–5 seconds". The restore measurement uses a dump fetched before the drill instead. CRR remains a stretch goal.
- **Rejected during review:** automatic reverse-replication failback — replaced by snapshot restore plus re-point where reverse replication is unavailable. Slower and uglier, and honest.
- **Rejected during review:** DynamoDB state locking — `use_lockfile = true` in Terraform 1.10+ removes the table entirely.
- **Rejected during review:** ECR cross-region replication — two pushes in one workflow is simpler and has no asynchronous race.
- **Rejected during review:** Docker Hub as the registry — public images, no regional presence, and a separate trust boundary; ECR keeps the image next to the compute and survives teardown inside the persistent root.
- **Rejected during review:** tag-based deploys — digests are the only way to guarantee both regions run identical bits.
- **Rejected during review:** applying the ephemeral root on push — Terraform would revert every fault injection and the drill would measure reconciliation.
- **Rejected during review:** private subnets with a NAT gateway — ~$32/month and a class of "instance has no route" failures for no phase-1 learning.
- **Rejected during review:** a minimal hand-written Terraform IAM policy — a multi-day `AccessDenied` exercise. Scope the *instance* roles tightly, accept a broad Terraform principal, and document the decision.

## Key Decisions & Staging for Next Steps

**Decisions carried into drafting**

1. RPO is a three-count ledger, never a single gap number. The ledger is written off-instance and fsynced.
2. RTO is four segments, with alarm-fired as the start event and sustained write as the stop event.
3. One persisted DR state record with a promotion epoch owns "which region is active". Nothing infers it.
4. Failover order is fixed: declare → fence → capture position → promote → verify writable → flip DNS → stop clock.
5. Failback is "rebuild and fail back", reported separately, scheduled before the write-up weekend.
6. Sequence numbers are minted by MySQL `auto_increment` with `auto_increment_increment` pinned before any rows exist, single writer, writes stopped at a recorded instant before promotion.
7. Two Terraform roots; the hosted zone and evidence bucket carry `prevent_destroy`; `verify-empty` is a first-class deliverable.
8. FreeDNS delegation is a 90-minute timeboxed experiment, not a dependency. Control-plane evidence comes from authoritative nameserver queries.
9. No NAT gateway, no ALB, no private subnets in phase 1.
10. Every resource carries a cost-allocation tag and a session ID.
11. The pipeline owns the persistent root and the image; humans own the ephemeral root and every injection.
12. Deploy by digest, pre-pull on standby, and never pull an image inside the RTO measurement.
13. OIDC subject pinned to a branch or environment; separate roles for application deploy and Terraform.
14. The protected environment approval is the disaster declaration, and the epoch write follows it.
15. Monitoring is CloudWatch alarms plus one repository-defined dashboard. No log platform, no tracing, no GitOps controller.
16. The SNS notification topic exists in both regions with the same subscriber, because alarm actions cannot cross regions.
17. The detection segment of RTO is read from alarm history, not from an operator stopwatch.

**Staging**

Adding the pipeline costs roughly 4–6 hours beyond the SSM deploy path the plan already needed, because that deploy mechanism was going to exist anyway. The six-weekend target holds only if the minimum-viable cuts are taken; seven weekends is the realistic expectation.

- **Evening before week 1 — console spike, no Terraform.** Create a cross-region replica by hand, write rows, promote, reverse-replicate, write again, verify no gaps, delete both. Four hours, roughly $2. This is a go/no-go: if failback cannot be made to work by hand in eight hours, permanently switch to snapshot-restore failback and cut S3 CRR the same evening.
- **Week 1 — guardrails and the loop.** `.gitignore` gains Terraform entries and `.gitattributes` sets `eol=lf` before the first apply. Budgets alarms at $10/$25/$50. Persistent root with the hosted zone, state bucket, ECR repositories, and lockfile. One EC2 per region through aliased providers. `verify-empty` passes. FreeDNS test runs in parallel.
- **Week 2 — the app, the harness, and the pipeline.** Dockerized event-sequence writer with a configurable rate, a `/health` that performs a database round trip, retry with backoff, and a client ledger recording sent and acknowledged timestamps. OIDC role with a pinned subject, `fmt`/`validate`/`tflint`/`checkov`, image build, dual-region ECR push by digest, SSM deploy. Bootstrap script stored in S3 and executed via SSM so iteration does not replace instances.
- **Week 3 — the data layer.** RDS primary and cross-region replica, built by hand first and then in Terraform. Encrypted with a destination-region KMS key. Cross-region security group by CIDR, not by reference. Alarms on `ReplicaLag`, instance status, and the custom RPO-gap metric. Baseline session: one hour of steady writes, lag distribution recorded.
- **Week 4 — the combined failover drill.** Route 53 health checks on Elastic IPs, failover records, fencing, promotion, DNS flip. Health-check alarms, the dashboard JSON, and the dual-region SNS path. Per-segment waterfall, with detection read from alarm history. Destroy.
- **Week 5 — failback.** Rebuild, reverse replication or snapshot restore, demote, verify. Destroy.
- **Week 6 — evidence.** Results table, priced comparison, cost method, diagrams, runbook, teardown proof, static ledger site.
- **Week 7 — pipeline hardening.** Move the drill and teardown into the pipeline with the protected environment, record one drill end to end from a workflow dispatch, and document the two limits.

**Kill criteria, committed in advance**

- FreeDNS delegation not working after 90 minutes → abandon delegation permanently, measure against authoritative nameservers.
- Reverse replication not working after 8 hours of manual effort → snapshot-restore failback, reduced claimed RTO.
- `apply` plus `destroy` not clean and repeatable after two weekends → collapse to one region and one drill. One real number beats a broken two-region stack.
- Cross-region replication not demonstrated within one weekend → second region app-only, data-tier drill moved to in-region Multi-AZ, deviation documented.
- Pipeline not deploying over SSM after 6 hours → fall back to a local script, document the omission, and move on.
- No measured number by end of week 5 → freeze scope, run one drill, publish one number.
- Projected spend above $40 by end of week 3 → stop introducing new resource types.
- Any resource type that fails to destroy twice → removed from the stack.

**Known traps to handle in implementation**

- `aws_db_instance` defaults `backup_retention_period = 0`; cross-region replicas require automated backups enabled.
- `replicate_source_db` must be the full ARN across regions; the bare identifier only works same-region.
- An encrypted source needs a KMS key in the destination region; the default `aws/rds` key is regional.
- Providers cannot be looped over with `for_each`; one module invoked twice with explicit `providers` blocks.
- A module without `providers = { aws = aws.east2 }` silently deploys into the default region.
- `default_tags` must be set on both provider blocks.
- Windows checkout: a `#!/bin/bash\r` shebang fails with `bad interpreter`. `eol=lf` is mandatory.
- The working copy sits under OneDrive; `.terraform/` is tens of thousands of files and will thrash sync or lock provider binaries. Move the repository out of OneDrive or exclude `.terraform/`.
- Route 53 health-check telemetry always lives in us-east-1 regardless of endpoint region; alarms and dashboard widgets for it must be created there.
- CloudWatch alarm actions cannot target an SNS topic in another region. Two topics, one subscriber.
- When a replica is promoted, `ReplicaLag` stops being emitted. Set `treat_missing_data` explicitly or the alarm misreports.
- SNS email subscriptions need a confirmation click. It cannot be automated, so it is a manual step in the runbook.
- The security group must allow Route 53 health-checker ranges, or failover is permanently on and reads as success.
- `aws_iam_policy_attachment` is exclusive and silently detaches other policies; use `aws_iam_role_policy_attachment`.
- GitHub Actions jobs that call AWS need `permissions: id-token: write`, or the OIDC token request fails.
- A new EC2 instance takes one to two minutes to register with SSM. A deploy that runs immediately after `apply` will fail with `InvalidInstanceId`; poll for registration first.
- `aws ssm send-command` returns before the command finishes. Poll `get-command-invocation` and treat a timeout as a failed deploy, not a success.
- ECR repositories are regional. A missing repository in the destination region fails the push, not the build.
