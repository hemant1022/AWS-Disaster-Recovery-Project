"""Shared helpers for the DR lab scripts.

Everything here is deliberately dependency-light: boto3 plus the standard
library, so the scripts run on Windows, Linux, or a GitHub runner without a
build step.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import boto3

STATE_KEY = "state/dr-state.json"


# --- Time and logging -------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(f"[{iso()}] {message}", flush=True)


# --- Terraform outputs ------------------------------------------------------

def terraform_output(root: str = "terraform/ephemeral") -> dict[str, Any]:
    """Read the current outputs of a root. Raises if the root is not applied."""
    result = subprocess.run(
        ["terraform", f"-chdir={root}", "output", "-json"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"terraform output failed for {root}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    raw = json.loads(result.stdout.decode())

    # Terraform 1.8+ wraps complex outputs as {sensitive, type, value}. Flatten
    # them so callers can read outputs["session_summary"]["primary_region"].
    flattened: dict[str, Any] = {}
    for key, value in raw.items():
        if (
            isinstance(value, dict)
            and {"sensitive", "type", "value"} <= set(value.keys())
        ):
            flattened[key] = value["value"]
        else:
            flattened[key] = value
    return flattened


# --- HTTP -------------------------------------------------------------------

def http_json(url: str, timeout: float = 3.0) -> tuple[int, Any]:
    """GET a JSON endpoint. Returns (status, parsed body or error text)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def wait_until(
    description: str,
    probe: Callable[[], Any],
    timeout: float,
    interval: float = 3.0,
) -> tuple[Any, datetime]:
    """Poll until probe returns something truthy. Returns (value, finish time)."""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = probe()
        if last:
            return last, utcnow()
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {description} (last={last!r})")


# --- DR state record --------------------------------------------------------

class StateConflict(RuntimeError):
    """Raised when another writer changed the record between read and write."""


class StateStore:
    """The single persisted record that owns which region is active.

    Writes are compare-and-swap on the S3 ETag, so a second declaration cannot
    silently overwrite the first.
    """

    def __init__(self, bucket: str, key: str = STATE_KEY, region: str = "us-east-1") -> None:
        self.bucket = bucket
        self.key = key
        self.s3 = boto3.client("s3", region_name=region)

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        except self.s3.exceptions.NoSuchKey:
            return None, None
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                return None, None
            raise
        return json.loads(obj["Body"].read()), obj["ETag"]

    def write(self, record: dict[str, Any], etag: str | None) -> str:
        body = json.dumps(record, indent=2, sort_keys=True).encode()
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self.key,
            "Body": body,
            "ContentType": "application/json",
        }
        if etag is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = etag

        try:
            response = self.s3.put_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                raise StateConflict(
                    "the DR state record changed while this run was in progress; "
                    "re-read it and decide again"
                ) from exc
            raise

        return response["ETag"]

    def declare(self, region: str, phase: str, note: str) -> dict[str, Any]:
        """Bump the epoch and record the new phase. Never silently overwrites."""
        current, etag = self.read()
        epoch = int((current or {}).get("promotion_epoch", 0)) + 1

        record = {
            "active_region": (current or {}).get("active_region", region),
            "write_enabled_region": (current or {}).get("write_enabled_region"),
            "promotion_epoch": epoch,
            "phase": phase,
            "last_known_replication_position": (current or {}).get(
                "last_known_replication_position"
            ),
            "updated_at": iso(),
            "note": note,
            "history": ((current or {}).get("history") or [])[-20:]
            + [
                {
                    "epoch": epoch,
                    "phase": phase,
                    "region": region,
                    "at": iso(),
                    "note": note,
                }
            ],
        }
        new_etag = self.write(record, etag)
        record["_etag"] = new_etag
        return record

    def record_promotion(self, region: str, note: str) -> dict[str, Any]:
        """Authorise `region` to write, without moving DNS.

        The record separates two facts: which region may write, and which
        region DNS points at. Promotion comes first; DNS moves after the write
        path has been verified. Keeping them separate is what lets the drill
        report RPO honestly when AWS promotes the replica on its own.
        """
        current, etag = self.read()
        if current is None:
            raise RuntimeError("no DR state record exists; declare before promoting")

        record = dict(current)
        record.update(
            {
                "write_enabled_region": region,
                "phase": "promoted",
                "updated_at": iso(),
                "note": note,
            }
        )
        record["history"] = (record.get("history") or [])[-20:] + [
            {
                "epoch": record.get("promotion_epoch"),
                "phase": "promote",
                "region": region,
                "at": iso(),
                "note": note,
            }
        ]
        self.write(record, etag)
        return record

    def activate(self, region: str, position: int | None, note: str) -> dict[str, Any]:
        """Move the active region. This is the step that flips DNS."""
        current, etag = self.read()
        if current is None:
            raise RuntimeError("no DR state record exists; declare before activating")

        record = dict(current)
        record.update(
            {
                "active_region": region,
                "phase": "steady",
                "last_known_replication_position": position,
                "updated_at": iso(),
                "note": note,
            }
        )
        record["history"] = (record.get("history") or [])[-20:] + [
            {
                "epoch": record.get("promotion_epoch"),
                "phase": "activate",
                "region": region,
                "at": iso(),
                "note": note,
            }
        ]
        self.write(record, etag)
        return record

    def ensure_active(self, region: str) -> dict[str, Any]:
        """Make the record name `region` as the steady-state writer.

        Called at preflight. A run that died mid-promotion leaves
        phase=promoting, and a rebuild leaves the record naming a region that
        no longer holds the database. Without this the next drill starts from a
        state that contradicts the healthy primary the preflight just verified.
        The epoch is not bumped: this is a repair, not a declaration.
        """
        current, etag = self.read()
        record = dict(current or {})
        record.update(
            {
                "active_region": region,
                "write_enabled_region": region,
                "phase": "steady",
                "promotion_epoch": int(record.get("promotion_epoch", 0)),
                "last_known_replication_position": record.get(
                    "last_known_replication_position"
                ),
                "updated_at": iso(),
                "note": "preflight: primary verified healthy and writable",
            }
        )
        record["history"] = (record.get("history") or [])[-20:]
        new_etag = self.write(record, etag)
        record["_etag"] = new_etag
        return record


# --- Ledger -----------------------------------------------------------------

def ledger_max_id(
    bucket: str,
    prefix: str = "ledger/",
    since_minutes: int = 30,
    region: str = "us-east-1",
) -> tuple[int, str | None]:
    """Highest committed row id recorded in S3 within the recent window.

    Returns (max_id, committed_at of that row). This is the durable record of
    what the application acknowledged, independent of any instance.
    """
    s3 = boto3.client("s3", region_name=region)
    cutoff = utcnow() - timedelta(minutes=since_minutes)

    best_id = 0
    best_at: str | None = None

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
            for line in body.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                # The ledger outlives the database. A rebuilt primary restarts
                # auto_increment at 1, so an unfiltered MAX(id) would compare
                # new rows against old ones and invent data loss. Filter by the
                # row's own commit time, not just the object's age.
                committed_at = row.get("committed_at")
                if committed_at:
                    try:
                        when = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
                    except ValueError:
                        when = None
                    if when is not None and when < cutoff:
                        continue
                if int(row.get("id", 0)) > best_id:
                    best_id = int(row["id"])
                    best_at = committed_at
    return best_id, best_at


# --- AWS helpers ------------------------------------------------------------

def client(service: str, region: str):
    return boto3.client(service, region_name=region)


def alarm_state(region: str, alarm_name: str) -> dict[str, Any]:
    cw = client("cloudwatch", region)
    return cw.describe_alarms(AlarmNames=[alarm_name])["MetricAlarms"][0]


def alarm_state_from_history_item(item: dict[str, Any]) -> str | None:
    """State value from a CloudWatch StateUpdate history item.

    CloudWatch nests the state as newState.stateValue. Older shapes use
    newState.state. Accept both: a schema detail that silently disables
    detection is exactly the failure this guards against.
    """
    try:
        data = json.loads(item.get("HistoryData", "{}"))
    except json.JSONDecodeError:
        return None
    new_state = data.get("newState", {})
    return new_state.get("stateValue", new_state.get("state"))


def alarm_transition(
    region: str, alarm_name: str, state: str = "ALARM", since: datetime | None = None
) -> datetime | None:
    """Timestamp of the most recent transition into `state`, from alarm history."""
    cw = client("cloudwatch", region)
    history = cw.describe_alarm_history(
        AlarmName=alarm_name,
        HistoryItemType="StateUpdate",
        MaxRecords=30,
        ScanBy="TimestampDescending",
    ).get("AlarmHistoryItems", [])

    for item in history:
        if since is not None and item["Timestamp"] < since:
            break
        if alarm_state_from_history_item(item) == state:
            return item["Timestamp"]
    return None
