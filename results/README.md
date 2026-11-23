# Drill results

Every file here is written by a script and uploaded to
`s3://drlab-evidence-<account>/results/`. Nothing is hand-edited.

| File | Produced by | Contents |
|---|---|---|
| `baseline-*.json` | `scripts/baseline.py` | committed rows, rejected rows, replication-lag distribution |
| `rehearsal-*.json` | `scripts/drill.py --fault none` | preflight evidence: both regions healthy, measurement wired |
| `failover-*.json` | `scripts/drill.py --fault regional` | fault, alarm, declaration, promotion, DNS, RPO |

The first regional attempt on 2026-11-23 is recorded as a defect in the design
document rather than as a result: a parser defect meant the harness never saw
an alarm that had already fired. The published failover number comes from the
corrected run.

The baseline is five minutes, not the full hour. It is enough to prove the
write path and the lag distribution; the longer session is the same code with a
larger sample.
