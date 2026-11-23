# Drill runbook

Three drills, run in this order. Each produces a JSON result in `results/` and
an identical object in the evidence bucket. Numbers are copied from that file,
never typed from memory.

## Definitions, fixed before the first drill

**RPO — recovery point objective.** The number of acknowledged writes missing
from the recovered database.

```
RPO = committed_rows − rows_on_promoted_primary
```

`committed_rows` is the larger of two independent records: the application's own
counter read immediately before the fault, and the maximum id in the S3 ledger.
The ledger is written by the application after each commit and lives off the
instance, so it survives the instance.

Writes that were attempted and rejected are counted separately and labelled
**availability**. They are not data loss. A row that never committed was never
promised.

**RTO — recovery time objective.** Four segments, never averaged into one
unqualified number:

| Segment | From | To |
|---|---|---|
| detect | fault injected | CloudWatch alarm enters `ALARM` |
| decide | alarm | declaration written |
| act | declaration | replica reports `available` and standalone |
| verify | promotion | writes sustained for 60 seconds and DNS answers the new region |

Published RTO runs from the **alarm**, not from the command. Time to detection
is reported separately, because it is a property of the health check interval
and the failure threshold, not of the recovery procedure.

## Preconditions

Run these before injecting anything. If any fails, stop.

```powershell
python -c "import boto3; print('ok')"

# 1. Both instances healthy
curl.exe -s -o NUL -w "%{http_code}`n" "http://<primary-eip>:8080/health"   # 200
curl.exe -s -o NUL -w "%{http_code}`n" "http://<dr-eip>:8080/health"        # 200

# 2. Primary writable, standby not. The standby returns 503 because it is not
#    the write-enabled region in the state record; the database is read-only too.
curl.exe -s -o NUL -w "%{http_code}`n" "http://<primary-eip>:8080/ready"    # 200
curl.exe -s -o NUL -w "%{http_code}`n" "http://<dr-eip>:8080/ready"         # 503

# 3. Replication current
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReplicaLag `
  --dimensions Name=DBInstanceIdentifier,Value=drlab-replica `
  --start-time (Get-Date).AddMinutes(-15).ToUniversalTime().ToString("s") `
  --end-time (Get-Date).ToUniversalTime().ToString("s") `
  --period 60 --statistics Average --region us-east-2

# 4. Ledger is arriving
aws s3 ls "s3://<evidence-bucket>/ledger/" --recursive | Select-Object -Last 3

# 5. No Terraform apply during the drill
#    Destroy/apply is the reset path; drift repair is not.
```

Also confirm the SNS email subscription is **confirmed**, or the alarm will fire
into nothing and time-to-detection becomes unmeasurable.

## Drill 1 — baseline

No failure. Establishes the write rate and the replication-lag distribution
before any number is trusted.

```powershell
# Let it run for 60 minutes with WRITE_INTERVAL_MS=200
python scripts/baseline.py --minutes 60
```

Produces p50, p95, and max lag, the committed row rate, and the rejected-write
count. A baseline with rejected writes is a broken lab, not a result.

Expected: ~5 rows/second, lag p95 under 2 seconds, zero rejected writes.

## Drill 2 — combined regional failover

One scripted sequence. The order is enforced by `scripts/drill.py`, not left to
the operator.

```powershell
python scripts/drill.py --fault regional --yes
```

What it does, in order:

1. **Inject** — stop the primary instance, delete the primary database with
   `--skip-final-snapshot`.
2. **Detect** — wait for `drlab-primary-endpoint-unhealthy` to enter `ALARM`
   and read the transition timestamp from `DescribeAlarmHistory`.
3. **Declare** — compare-and-swap `promotion_epoch` on `state/dr-state.json` in
   S3. A second concurrent run loses the swap and exits.
4. **Fence** — confirm the primary instance is `stopped` before touching the
   replica.
5. **Capture position** — read the ledger's maximum id and the replica's
   `MAX(id)` at the fence.
6. **Authorise writes** — set `write_enabled_region` to the recovery region in
   the state record. Both applications gate commits on this field, so the
   standby cannot write until the position above has been captured.
7. **Promote** — `promote-read-replica` with `--backup-retention-period 1`,
   then wait for `available` and no replication source.
8. **Verify** — poll `/ready` until it returns 200, then require 60 seconds of
   continuous success.
9. **Flip DNS** — rewrite the application A record to the recovery Elastic IP,
   then confirm with `TestDNSAnswer`.
10. **Stop the clock** at the later of sustained writes and a verified DNS answer.

### Why promotion is not the step people assume

For MySQL, AWS promotes a cross-Region read replica **automatically** when its
source DB instance is deleted. By the time the alarm fires, the recovery
database may already be writable. Two consequences:

- `promote-read-replica` is idempotent here: the script treats "not a read
  replica" as success. The `act` segment therefore measures the tail of a
  promotion that began at the fault, not its full duration. The result says so
  rather than presenting a suspiciously short act as the whole story.
- The application must not commit during that window. If it did, the replica's
  `MAX(id)` would move past the acknowledged count and the drill would publish
  an RPO of zero. The `write_enabled_region` gate prevents it: it is a
  single-writer guarantee, deliberately separate from `active_region`, which
  controls DNS.

This is the same class of defect as the frozen-snapshot bug below — a number
that looks plausible and is wrong. The gate makes it structurally impossible
instead of remembering not to do it.

Fault variants:

| `--fault` | Damages | RPO fidelity |
|---|---|---|
| `regional` | primary database and instance | full: replication stops at the fault |
| `instance` | application instance only | low: replication keeps running, expect RPO 0 |
| `none` | nothing | preflight and measurement only; never promotes |

`--fault none` is a non-destructive rehearsal: it checks both regions, reads the
replication position, and writes a `results/rehearsal-*.json`. It does not
declare, fence, promote, or touch DNS. Use it before every real drill and after
any change to the scripts.

The standby must be running before a real drill. Its `/state` is how the drill
learns the replica's `MAX(id)` at the fence; a stopped standby makes that
measurement fail. Start it with `scripts/deploy.py` (without
`--no-start-standby`) and confirm `/health` is 200 and `/ready` is 503.

## First run — 2026-11-23

The first regional run was stopped after the alarm wait timed out. The alarm had
fired at 05:01:39, 138 seconds after the fault, but `drlib.alarm_transition`
read `newState.state` instead of `newState.stateValue` and never saw it. Two
further defects surfaced before the re-run: a rejected insert on the read-only
replica froze its reported `MAX(id)`, and `systemctl start` is a no-op on the
active `Type=oneshot` unit, so redeploys kept the old image. All are fixed, the
parser has a regression test in `scripts/test_drlib.py`, and the lab was reset
with `-replace=aws_db_instance.replica` before the clean run.

Validating the reset found a fifth: AWS promotes a cross-Region MySQL replica
automatically when its source is deleted, so the standby database is writable
before the drill declares anything. Without the `write_enabled_region` gate the
standby application would commit during that window and the RPO would be
reported as zero. The gate is described above and asserted at preflight.

## Drill 3 — failback

Failback is not the inverse of failover. The old primary has diverged and cannot
be reattached; async replication without a rewind path has no way back.

This drill is scheduled for week 5 and is **not scripted yet**. Run it by hand
the first time, record the timings, then automate what actually worked.

The procedure:

1. Permanently fence region 1. It is not coming back as-is.
2. Destroy the region-1 database and instance.
3. Re-apply the ephemeral root with the roles reversed: region 1 becomes the
   replica of region 2.
4. Wait for lag to reach zero.
5. Declare, fence region 2, promote region 1.
6. Flip DNS back.
7. Stop the clock. This is a separate RTO and is published separately.

If binary logs on the promoted instance have already been purged, step 3 falls
back to a snapshot restore. That is a materially worse number and is published
as such rather than hidden.

## Recording a result

The script writes `results/failover-<timestamp>.json` and uploads the same
object to `s3://<evidence-bucket>/results/`. Fill the README table from it:

```powershell
Get-ChildItem results | Sort-Object LastWriteTime | Select-Object -Last 1 |
  Get-Content | ConvertFrom-Json |
  Select-Object time_to_detection_seconds, rto_seconds, rto_segments_seconds,
                @{n='rpo_rows';e={$_.rpo.lost_rows}}
```

Attach to the result, in the same commit:

- the CloudWatch alarm history export for the drill window
- `describe-db-instances` output for both regions, before and after
- the Route 53 `TestDNSAnswer` output before and after the flip
- the first and last ledger object keys used in the calculation

## DNS sensitivity table

The control-plane number is close to tautological: it equals the TTL plus the
health-check interval times the failure threshold. Publish it as a table so it
teaches the tradeoff instead of merely existing.

| TTL | Interval | Threshold | Worst-case detection | Worst-case resolution |
|---|---|---|---|---|
| 60 | 10 | 3 | 30 s | 60 s |
| 60 | 30 | 3 | 90 s | 60 s |
| 300 | 10 | 3 | 30 s | 300 s |
| 300 | 30 | 3 | 90 s | 300 s |

Measure two rows, not four. Change `dns_ttl` and
`health_check_request_interval` in `terraform/ephemeral/terraform.tfvars`,
re-apply, and repeat the drill.

## After every drill

```powershell
python scripts/teardown.py --yes
```

The teardown is not optional and not deferred. A drill leaves a promoted
replica, a deleted primary, and a stopped instance; the next `apply` assumes
none of that. Destroy, verify empty, then stop for the day.

## Known limits to state in the write-up

- The ledger flushes every 2 seconds, so a write committed in the final flush
  window can be invisible to the RPO calculation. That is the measurement floor.
- `MAX(id)` assumes ids are monotonic and that no acknowledged row was rolled
  back. Both hold for this application; neither is true in general.
- DNS failover is declaration-gated. A health-check-gated flip would send
  traffic to a read-only replica before promotion.
- Single-AZ per region. Multi-AZ is a different failure model with a different
  RTO.
