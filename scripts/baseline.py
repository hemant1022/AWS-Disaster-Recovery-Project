"""Baseline session: no failure, just evidence that the harness works.

Samples the application counters and RDS replication lag for a fixed window,
then reports the lag distribution and the committed write rate. A baseline with
rejected writes is a broken lab, not a result.

    python scripts/baseline.py --minutes 60
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import boto3

from drlib import http_json, iso, log, terraform_output, utcnow


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def lag_sample(region: str, identifier: str) -> float | None:
    cw = boto3.client("cloudwatch", region_name=region)
    now = utcnow()
    response = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="ReplicaLag",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": identifier}],
        StartTime=now.timestamp() - 300,
        EndTime=now.timestamp(),
        Period=60,
        Statistics=["Average"],
    )
    points = sorted(response["Datapoints"], key=lambda p: p["Timestamp"])
    return float(points[-1]["Average"]) if points else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--interval", type=float, default=10, help="seconds between samples")
    parser.add_argument("--outputs", default="terraform/ephemeral")
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()

    outputs = terraform_output(args.outputs)
    summary = outputs["session_summary"]
    base = f"http://{summary['primary_eip']}:{outputs['app_port']}"

    started = utcnow()
    first = None
    samples: list[dict] = []

    log(f"baseline for {args.minutes} minutes against {base}")
    deadline = time.time() + args.minutes * 60

    while time.time() < deadline:
        status, body = http_json(f"{base}/state")
        lag = lag_sample(summary["dr_region"], outputs["dr_db_identifier"])

        if status == 200 and isinstance(body, dict):
            if first is None:
                first = body
            samples.append(
                {
                    "at": iso(),
                    "max_id": body["max_id"],
                    "committed": body["stats"]["committed"],
                    "rejected": body["stats"]["rejected"],
                    "ledger_pending": body["ledger_pending"],
                    "lag": lag,
                }
            )
            log(
                f"max_id={body['max_id']} committed={body['stats']['committed']} "
                f"rejected={body['stats']['rejected']} lag={lag}"
            )
        else:
            log(f"sample failed: {status} {body}")

        time.sleep(args.interval)

    finished = utcnow()
    if not samples or first is None:
        log("no samples collected")
        return 1

    last = samples[-1]
    elapsed = (finished - started).total_seconds()
    committed_delta = last["committed"] - first["stats"]["committed"]
    rejected_delta = last["rejected"] - first["stats"]["rejected"]
    lags = [s["lag"] for s in samples if s["lag"] is not None]

    result = {
        "drill": "baseline",
        "window": {"start": iso(started), "end": iso(finished), "seconds": round(elapsed, 1)},
        "rows": {
            "committed": committed_delta,
            "rejected": rejected_delta,
            "per_second": round(committed_delta / elapsed, 3) if elapsed else None,
            "max_id_start": first["max_id"],
            "max_id_end": last["max_id"],
        },
        "replication_lag_seconds": {
            "samples": len(lags),
            "p50": round(percentile(lags, 0.50), 2) if lags else None,
            "p95": round(percentile(lags, 0.95), 2) if lags else None,
            "max": round(max(lags), 2) if lags else None,
            "mean": round(statistics.fmean(lags), 2) if lags else None,
        },
        "healthy": rejected_delta == 0 and committed_delta > 0,
        "recorded_at": iso(),
    }

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    local_path = results_dir / f"baseline-{stamp}.json"
    local_path.write_text(json.dumps(result, indent=2))

    bucket = outputs["evidence_bucket"]
    boto3.client("s3", region_name=summary["primary_region"]).put_object(
        Bucket=bucket,
        Key=f"results/baseline-{stamp}.json",
        Body=json.dumps(result, indent=2).encode(),
        ContentType="application/json",
    )

    log("")
    log("=" * 68)
    log(f"committed      : {committed_delta} rows ({result['rows']['per_second']}/s)")
    log(f"rejected       : {rejected_delta} rows")
    log(f"lag p50/p95/max: {result['replication_lag_seconds']['p50']} / "
        f"{result['replication_lag_seconds']['p95']} / "
        f"{result['replication_lag_seconds']['max']} s")
    log(f"healthy        : {result['healthy']}")
    log(f"result         : s3://{bucket}/results/baseline-{stamp}.json")
    log("=" * 68)
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
