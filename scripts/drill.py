"""Run the combined regional-failover drill and record the numbers.

The order is fixed and enforced here, not left to the operator:

    inject fault -> wait for the alarm -> declare -> fence -> capture position
    -> authorise writes -> promote -> verify writable -> flip DNS -> stop the clock

RTO runs from the alarm firing to the later of sustained writes on the new
primary and a verified DNS answer, matching docs/drills.md. Time-to-detection
(fault to alarm) is reported separately and never folded in.

    python scripts/drill.py --fault regional --yes
    python scripts/drill.py --fault instance --yes
    python scripts/drill.py --fault none --yes          # rehearsal, no damage

Every run writes a JSON result to results/ and uploads it to the evidence
bucket. Nothing is inferred: each number comes from an API timestamp or an
HTTP response.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3

from drlib import (
    StateConflict,
    StateStore,
    alarm_transition,
    http_json,
    iso,
    ledger_max_id,
    log,
    terraform_output,
    utcnow,
    wait_until,
)

PRIMARY_ALARM = "drlab-primary-endpoint-unhealthy"


# --- Probes -----------------------------------------------------------------

def probe(base: str, path: str) -> tuple[int, dict | str]:
    return http_json(f"{base}{path}")


def app_state(base: str) -> dict:
    status, body = probe(base, "/state")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"{base}/state returned {status}: {body}")
    return body


def replica_lag(region: str, identifier: str) -> float | None:
    cw = boto3.client("cloudwatch", region_name=region)
    response = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="ReplicaLag",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": identifier}],
        StartTime=datetime.now().astimezone().replace(microsecond=0).timestamp() - 600,
        EndTime=datetime.now().astimezone().timestamp(),
        Period=60,
        Statistics=["Average"],
    )
    points = sorted(response["Datapoints"], key=lambda p: p["Timestamp"])
    return float(points[-1]["Average"]) if points else None


# --- Fault injection --------------------------------------------------------

def inject_fault(fault: str, region: str, instance_id: str, db_identifier: str) -> dict:
    """Damage the primary region. Returns what was actually done."""
    actions: dict = {"fault": fault, "at": iso()}

    if fault == "none":
        log("rehearsal: no fault injected")
        return actions

    ec2 = boto3.client("ec2", region_name=region)
    log(f"stopping primary instance {instance_id}")
    ec2.stop_instances(InstanceIds=[instance_id])
    actions["stopped_instance"] = instance_id

    if fault == "regional":
        rds = boto3.client("rds", region_name=region)
        log(f"deleting primary database {db_identifier}")
        try:
            rds.delete_db_instance(
                DBInstanceIdentifier=db_identifier,
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )
            actions["deleted_db"] = db_identifier
        except Exception as exc:  # noqa: BLE001
            # If AWS refuses (for example because a replica still references
            # it), the instance stop still removes the writer. RPO fidelity is
            # lower because replication keeps running, so this is recorded.
            log(f"database delete refused: {exc}")
            actions["delete_error"] = str(exc)

    return actions


def fence(region: str, instance_id: str) -> dict:
    """Guarantee the old primary cannot accept writes, then confirm it."""
    ec2 = boto3.client("ec2", region_name=region)
    ec2.stop_instances(InstanceIds=[instance_id])

    def stopped() -> bool:
        state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
            "Instances"
        ][0]["State"]["Name"]
        return state == "stopped"

    try:
        wait_until("primary instance to stop", stopped, timeout=180, interval=5)
        return {"fenced": True, "at": iso()}
    except TimeoutError:
        return {"fenced": False, "at": iso(), "note": "instance did not reach stopped"}


# --- Promotion --------------------------------------------------------------

def promote(region: str, identifier: str, timeout: int = 1800) -> datetime:
    rds = boto3.client("rds", region_name=region)

    log(f"promoting {identifier} in {region}")
    try:
        rds.promote_read_replica(
            DBInstanceIdentifier=identifier,
            BackupRetentionPeriod=1,
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        # Deleting the source of a cross-Region MySQL replica promotes it
        # automatically, and the promotion may still be running. Either way the
        # call is redundant, not failed: the wait below is the real assertion.
        # Anything else is a genuine error and must surface.
        if any(
            token in message
            for token in ("not a read replica", "invalid db instance state", "already")
        ):
            log(f"promotion call not needed: {exc}")
        else:
            raise

    def available() -> bool:
        instance = rds.describe_db_instances(DBInstanceIdentifier=identifier)[
            "DBInstances"
        ][0]
        if instance["DBInstanceStatus"] == "available" and not instance.get(
            "ReadReplicaSourceDBInstanceIdentifier"
        ):
            return True
        log(f"  status: {instance['DBInstanceStatus']}")
        return False

    _, finished = wait_until("promotion", available, timeout=timeout, interval=15)
    return finished


# --- DNS --------------------------------------------------------------------

def flip_dns(zone_id: str, fqdn: str, ttl: int, target_ip: str) -> datetime:
    """Rewrite the application record so the name resolves to the new region."""
    route53 = boto3.client("route53", region_name="us-east-1")

    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": "drlab drill: activate recovery region",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": fqdn,
                        "Type": "A",
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": target_ip}],
                    },
                }
            ],
        },
    )
    return utcnow()


def dns_answer(zone_id: str, fqdn: str) -> str | None:
    route53 = boto3.client("route53", region_name="us-east-1")
    response = route53.test_dns_answer(
        HostedZoneId=zone_id,
        RecordName=fqdn,
        RecordType="A",
    )
    answers = response.get("RecordData", [])
    return answers[0] if answers else None


def local_resolution(fqdn: str) -> str | None:
    try:
        return socket.gethostbyname(fqdn)
    except OSError:
        return None


# --- Main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fault",
        choices=["regional", "instance", "none"],
        default="regional",
        help="regional deletes the primary database; instance only stops the app instance",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--outputs", default="terraform/ephemeral")
    parser.add_argument("--sustain-seconds", type=int, default=60)
    parser.add_argument("--alarm-timeout", type=int, default=600)
    parser.add_argument("--promote-timeout", type=int, default=1800)
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()

    outputs = terraform_output(args.outputs)
    summary = outputs["session_summary"]

    primary_region = summary["primary_region"]
    dr_region = summary["dr_region"]
    primary_eip = summary["primary_eip"]
    dr_eip = summary["dr_eip"]
    primary_base = f"http://{primary_eip}:{outputs['app_port']}"
    dr_base = f"http://{dr_eip}:{outputs['app_port']}"
    bucket = outputs["evidence_bucket"]
    zone_id = outputs["zone_id"]
    fqdn = outputs["app_fqdn"]

    state = StateStore(bucket)

    # --- Preflight ----------------------------------------------------------
    log("preflight")
    for name, base in (("primary", primary_base), ("standby", dr_base)):
        status, body = probe(base, "/health")
        if status != 200:
            raise RuntimeError(f"{name} is not healthy before the drill: {status} {body}")

    status, body = probe(primary_base, "/ready")
    if status != 200:
        raise RuntimeError(f"primary is not writable before the drill: {status} {body}")

    lag = replica_lag(dr_region, outputs["dr_db_identifier"])
    log(f"replica lag before drill: {lag if lag is not None else 'no datapoint'} s")

    before = app_state(primary_base)
    committed_before = int(before["max_id"])
    log(f"primary committed rows: {committed_before}")

    standby_before = app_state(dr_base)
    if standby_before.get("max_id") is None:
        # Without the replica's position the RPO calculation would treat every
        # acknowledged row as lost. That is a fake number, not a measurement.
        raise RuntimeError(
            "the standby cannot read the events table; replication has not "
            "caught up or the schema never arrived. Refusing to measure."
        )
    log(
        f"standby replicated rows: {standby_before['max_id']} "
        f"(write-enabled: {standby_before.get('may_write')})"
    )
    if standby_before.get("may_write"):
        # The single-writer gate is the thing that keeps a pre-promotion write
        # from moving the replica's MAX(id) past the acknowledged count. If it
        # is not working, every number below is wrong, so refuse to measure.
        raise RuntimeError(
            "the standby reports may_write=true before promotion; the write "
            "gate is not working and any RPO measured now would be understated"
        )

    # A run that died mid-promotion, or a rebuilt lab, can leave the record
    # naming a region that no longer holds the database. The probes above have
    # just proven this one is healthy and writable, so make the record agree
    # before anything is measured.
    state_record = state.ensure_active(primary_region)
    log(
        f"state: write-enabled region is {state_record['write_enabled_region']} "
        f"(epoch {state_record['promotion_epoch']})"
    )

    # A rehearsal must not promote anything: promotion is what breaks
    # replication, so a "no damage" run that promoted would be a lie. This mode
    # validates connectivity, the state store, and the measurement, then stops.
    if args.fault == "none":
        result = {
            "drill": "rehearsal",
            "fault": "none",
            "regions": {"primary": primary_region, "recovery": dr_region},
            "preflight": {
                "primary_health": "ok",
                "standby_health": "ok",
                "primary_writable": True,
                "replica_lag_seconds": lag,
                "primary_max_id": committed_before,
                "standby_max_id": standby_before["max_id"],
                "standby_committed": standby_before["stats"]["committed"],
                "standby_rejected": standby_before["stats"]["rejected"],
                "standby_gated": standby_before["stats"].get("gated", 0),
                "standby_may_write": standby_before.get("may_write"),
            },
            "no_damage": True,
            "note": "no fault injected, no promotion, no DNS change",
            "recorded_at": iso(),
        }
        results_dir = Path(args.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        (results_dir / f"rehearsal-{stamp}.json").write_text(
            json.dumps(result, indent=2)
        )
        key = f"results/rehearsal-{stamp}.json"
        boto3.client("s3", region_name=primary_region).put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(result, indent=2).encode(),
            ContentType="application/json",
        )
        log("")
        log("=" * 68)
        log("rehearsal passed: both regions healthy, measurement wired")
        log(f"primary={committed_before} replicated={standby_before['max_id']} "
            f"lag={lag}s")
        log(f"result: s3://{bucket}/{key}")
        log("=" * 68)
        return 0

    if not args.yes:
        answer = input(
            f"\nThis will destroy the primary database in {primary_region}. "
            f"Type the region name to continue: "
        )
        if answer.strip() != primary_region:
            log("aborted")
            return 1

    # --- Inject -------------------------------------------------------------
    t_fault = utcnow()
    actions = inject_fault(
        args.fault,
        primary_region,
        outputs["primary_instance_id"],
        outputs["primary_db_identifier"],
    )

    # --- Detect -------------------------------------------------------------
    log("waiting for the primary health alarm")
    t_alarm = wait_until(
        "primary alarm",
        lambda: alarm_transition("us-east-1", PRIMARY_ALARM, "ALARM", since=t_fault),
        timeout=args.alarm_timeout,
        interval=5,
    )[0]
    detection_seconds = (t_alarm - t_fault).total_seconds()
    log(f"alarm fired {detection_seconds:.1f}s after the fault")

    # --- Declare ------------------------------------------------------------
    try:
        record = state.declare(
            dr_region,
            phase="promoting",
            note=f"{args.fault} fault drill",
        )
    except StateConflict as exc:
        log(f"declaration lost a race: {exc}")
        return 2
    t_declare = utcnow()
    log(f"declared epoch {record['promotion_epoch']}")

    # --- Fence --------------------------------------------------------------
    fence_result = fence(primary_region, outputs["primary_instance_id"])
    log(f"fence: {fence_result}")

    # --- Capture position ---------------------------------------------------
    # Two independent records of what was acknowledged: the app's own counter
    # just before it died, and the durable S3 ledger. Take the larger.
    committed_from_ledger, ledger_at = ledger_max_id(bucket, since_minutes=10)
    committed = max(committed_before, committed_from_ledger)

    try:
        replica_at_fence = int(app_state(dr_base)["max_id"])
    except Exception as exc:  # noqa: BLE001
        # Falling back to 0 here would publish the entire dataset as lost.
        raise RuntimeError(
            f"could not read the replica position at the fence: {exc}"
        ) from exc

    rpo_rows = max(0, committed - replica_at_fence)
    log(
        f"position: committed={committed} (ledger={committed_from_ledger}, "
        f"app={committed_before}) replicated={replica_at_fence} rpo_rows={rpo_rows}"
    )

    # --- Authorise writes ---------------------------------------------------
    # AWS promotes a cross-Region MySQL replica automatically when its source
    # is deleted, so by now the database may already be writable. The
    # application is still gated on this record, which is why the position
    # above is the true replication position and not one polluted by standby
    # writes that started before the measurement.
    state.record_promotion(dr_region, note=f"{args.fault} promotion authorised")
    log(f"write-enabled region is now {dr_region}")

    # --- Promote ------------------------------------------------------------
    t_promote_start = utcnow()
    t_promoted = promote(dr_region, outputs["dr_db_identifier"], args.promote_timeout)
    log(f"promotion complete in {(t_promoted - t_promote_start).total_seconds():.1f}s")

    # --- Verify writable and sustained --------------------------------------
    log("waiting for the new primary to accept writes")

    def writable() -> bool:
        status, body = probe(dr_base, "/ready")
        return status == 200

    _, t_first_write = wait_until("first successful write", writable, timeout=600, interval=3)

    sustained_since = time.time()
    while time.time() - sustained_since < args.sustain_seconds:
        if not writable():
            sustained_since = time.time()
            log("  write path broke again; restarting the sustain window")
        time.sleep(5)
    t_sustained = utcnow()
    log(f"sustained writes for {args.sustain_seconds}s")

    # --- Flip DNS -----------------------------------------------------------
    t_dns_flip = flip_dns(
        zone_id,
        fqdn,
        outputs.get("dns_ttl", 60),
        dr_eip,
    )
    log(f"DNS record rewritten to {dr_eip}")

    try:
        wait_until(
            "Route 53 to answer with the new region",
            lambda: dns_answer(zone_id, fqdn) == dr_eip,
            timeout=120,
            interval=3,
        )
        dns_verified = True
    except TimeoutError:
        dns_verified = False
        log(f"Route 53 answer did not settle; local resolution is {local_resolution(fqdn)}")

    # Persist which region is now active. The declaration earlier only bumped
    # the epoch and set phase=promoting; without this the state object would
    # still name the old region after a successful failover.
    try:
        state.activate(
            dr_region,
            position=replica_at_fence,
            note=f"{args.fault} failover complete",
        )
    except StateConflict as exc:
        log(f"activation lost a race: {exc}")

    # --- Record -------------------------------------------------------------
    t_stop = max(t_sustained, utcnow())
    rto_seconds = (t_stop - t_alarm).total_seconds()

    result = {
        "drill": "combined-regional-failover",
        "fault": args.fault,
        "actions": actions,
        "fence": fence_result,
        "regions": {"primary": primary_region, "recovery": dr_region},
        "epoch": record["promotion_epoch"],
        "timeline": {
            "fault": iso(t_fault),
            "alarm": iso(t_alarm),
            "declare": iso(t_declare),
            "promote_start": iso(t_promote_start),
            "promoted": iso(t_promoted),
            "first_write": iso(t_first_write),
            "sustained": iso(t_sustained),
            "dns_flip": iso(t_dns_flip),
            "stop": iso(t_stop),
        },
        "time_to_detection_seconds": round(detection_seconds, 1),
        "rto_seconds": round(rto_seconds, 1),
        "rto_segments_seconds": {
            "decide": round((t_declare - t_alarm).total_seconds(), 1),
            "act": round((t_promoted - t_declare).total_seconds(), 1),
            "verify": round((t_stop - t_promoted).total_seconds(), 1),
        },
        "rpo": {
            "committed_rows": committed,
            "committed_from_ledger": committed_from_ledger,
            "committed_from_app": committed_before,
            "replicated_rows_at_fence": replica_at_fence,
            "lost_rows": rpo_rows,
            "ledger_last_commit": ledger_at,
            "note": "ledger flush interval bounds the floor of this measurement",
        },
        "replica_lag_before_seconds": lag,
        "dns_verified_via_route53": dns_verified,
        "dns_local_resolution": local_resolution(fqdn),
        "recorded_at": iso(),
    }

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = t_fault.strftime("%Y%m%dT%H%M%SZ")
    local_path = results_dir / f"failover-{stamp}.json"
    local_path.write_text(json.dumps(result, indent=2))

    key = f"results/failover-{stamp}.json"
    boto3.client("s3", region_name=primary_region).put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(result, indent=2).encode(),
        ContentType="application/json",
    )

    log("")
    log("=" * 68)
    log(f"time to detection : {detection_seconds:8.1f} s   (fault -> alarm)")
    log(f"RTO               : {rto_seconds:8.1f} s   (alarm -> sustained writes)")
    log(f"  decide          : {result['rto_segments_seconds']['decide']:8.1f} s")
    log(f"  act             : {result['rto_segments_seconds']['act']:8.1f} s")
    log(f"  verify          : {result['rto_segments_seconds']['verify']:8.1f} s")
    log(f"RPO               : {rpo_rows:8d} rows")
    log(f"result            : s3://{bucket}/{key}")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
