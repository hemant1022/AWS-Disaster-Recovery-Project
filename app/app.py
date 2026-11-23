"""Synthetic write-path application for the DR lab.

It does three things and nothing else:

1. Commits a row to MySQL on a fixed interval.
2. Appends every committed row to an S3 ledger, so the record of what was
   acknowledged lives off the instance that acknowledged it.
3. Reports whether it is alive and whether the database is currently writable.

The ledger is the ground truth for RPO. A row that reached S3 as committed was
acknowledged to the client; a row missing from the promoted database after
failover is a real loss, not an inferred one.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
import pymysql
from flask import Flask, jsonify

# --- Configuration ----------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/drlab/use1")
LEDGER_BUCKET = os.environ["LEDGER_BUCKET"]
LEDGER_PREFIX = os.environ.get("LEDGER_PREFIX", "ledger/")
STATE_KEY = os.environ.get("STATE_KEY", "state/dr-state.json")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
WRITE_INTERVAL_MS = int(os.environ.get("WRITE_INTERVAL_MS", "200"))
LEDGER_FLUSH_MS = int(os.environ.get("LEDGER_FLUSH_MS", "2000"))
HEALTH_DB_CHECK = os.environ.get("HEALTH_DB_CHECK", "true").lower() == "true"

APP_STARTED_AT = datetime.now(timezone.utc)

app = Flask(__name__)


# --- Instance identity ------------------------------------------------------

def _instance_id() -> str:
    """Read the instance id from IMDSv2. Falls back to the hostname."""
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(token_req, timeout=2).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return urllib.request.urlopen(req, timeout=2).read().decode()
    except Exception:
        return socket.gethostname()


INSTANCE_ID = _instance_id()


# --- Database ---------------------------------------------------------------

class Database:
    """One connection, guarded by a lock, reconnecting on failure."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = None
        self._config = None
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    def _load_config(self) -> dict:
        ssm = boto3.client("ssm", region_name=REGION)

        def get(name: str, decrypt: bool = False) -> str:
            return ssm.get_parameter(
                Name=f"{SSM_PREFIX}/{name}", WithDecryption=decrypt
            )["Parameter"]["Value"]

        return {
            "host": get("db_host"),
            "user": get("db_user"),
            "password": get("db_password", decrypt=True),
            "database": get("db_name"),
            "port": 3306,
            "connect_timeout": 5,
            "read_timeout": 10,
            "write_timeout": 10,
            "charset": "utf8mb4",
            "autocommit": False,
        }

    def connect(self) -> None:
        if self._config is None:
            self._config = self._load_config()
        self._conn = pymysql.connect(**self._config)

    def _ensure(self) -> None:
        if self._conn is None:
            self.connect()
            return
        try:
            self._conn.ping(reconnect=False)
        except Exception:
            self._conn = None
            self.connect()

    def table_exists(self) -> bool:
        with self._lock:
            self._ensure()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = DATABASE() AND table_name = 'events'
                    """
                )
                return int(cur.fetchone()[0]) > 0

    def init_schema(self) -> None:
        # A read-only replica already has the table, replicated from the
        # primary, and rejects DDL. Checking first turns that from a startup
        # failure into a no-op.
        if self.table_exists():
            return
        with self._lock:
            self._ensure()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                        epoch INT NOT NULL DEFAULT 0,
                        region VARCHAR(32) NOT NULL,
                        instance_id VARCHAR(64) NOT NULL,
                        payload VARCHAR(255) NOT NULL,
                        created_at DATETIME(6) NOT NULL,
                        PRIMARY KEY (id),
                        KEY idx_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            self._conn.commit()

    def insert(self, epoch: int, payload: str) -> tuple[int, str]:
        """Commit one row and return (id, committed_at). Raises on failure."""
        created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._lock:
            self._ensure()
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO events (epoch, region, instance_id, payload, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (epoch, REGION, INSTANCE_ID, payload, created_at),
                    )
                    row_id = cur.lastrowid
                self._conn.commit()
            except Exception:
                # A rejected write must not leave an open transaction. On the
                # read-only replica it would freeze the snapshot, and every
                # later MAX(id) would report the replication position from the
                # moment of the first rejection.
                self._rollback()
                raise
        committed_at = datetime.now(timezone.utc)
        self.last_success_at = time.time()
        self.last_error = None
        return int(row_id), committed_at.isoformat()

    def _rollback(self) -> None:
        try:
            if self._conn is not None:
                self._conn.rollback()
        except Exception:  # noqa: BLE001 - a dead connection is reconnected later
            self._conn = None

    def ping(self) -> None:
        with self._lock:
            self._ensure()
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

    def is_writable(self) -> bool:
        """A replica answers read_only=1; a promoted primary answers 0."""
        with self._lock:
            self._ensure()
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT @@global.read_only")
                    return int(cur.fetchone()[0]) == 0
            finally:
                self._rollback()

    def max_id(self) -> int:
        with self._lock:
            self._ensure()
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(MAX(id), 0) FROM events")
                    value = int(cur.fetchone()[0])
            finally:
                # End the read transaction so the next call sees fresh rows
                # rather than the snapshot this one established.
                self._rollback()
            return value


db = Database()


# --- DR state ---------------------------------------------------------------

class DrState:
    """Cached view of the single record that owns 'which region is active'.

    The record carries two separate facts:

    * ``write_enabled_region`` - the only region whose application may commit.
    * ``active_region`` - the region DNS currently points at.

    They are separated because promotion and DNS are separated. AWS promotes a
    cross-Region MySQL replica automatically when its source is deleted, so the
    standby database can become writable before the drill has captured the
    replication position. Without the write gate the standby would commit rows
    during that window, the position would move past the acknowledged count,
    and the reported RPO would silently collapse to zero.
    """

    # Two seconds: one S3 GET per instance per window. A 10s cache would put up
    # to 10 seconds of the promotion decision inside the measured RTO.
    REFRESH_SECONDS = 2

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epoch = 0
        self._active_region = None
        self._write_region = None
        self._loaded_at = 0.0
        self._s3 = boto3.client("s3", region_name=REGION)

    def get(self) -> tuple[int, str | None, str | None]:
        with self._lock:
            if time.time() - self._loaded_at > self.REFRESH_SECONDS:
                try:
                    obj = self._s3.get_object(Bucket=LEDGER_BUCKET, Key=STATE_KEY)
                    state = json.loads(obj["Body"].read())
                    self._epoch = int(state.get("promotion_epoch", 0))
                    self._active_region = state.get("active_region")
                    # Absent on a record written before the write gate existed:
                    # fall back to the serving region so an upgrade does not
                    # stop the live writer.
                    self._write_region = state.get(
                        "write_enabled_region", self._active_region
                    )
                except Exception:
                    # No declaration yet, or S3 is unreachable. Epoch 0 is the
                    # honest answer in both cases.
                    pass
                self._loaded_at = time.time()
            return self._epoch, self._active_region, self._write_region

    def may_write(self) -> bool:
        """Single-writer guarantee: only the named region may commit."""
        _, _, write_region = self.get()
        return write_region is None or write_region == REGION


dr_state = DrState()


# --- Ledger -----------------------------------------------------------------

class Ledger:
    """Buffers committed rows and flushes them to S3 as JSONL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._s3 = boto3.client("s3", region_name=REGION)
        self.last_flush_at: float | None = None
        self.last_error: str | None = None

    def append(self, row: dict) -> None:
        with self._lock:
            self._buffer.append(row)

    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            rows, self._buffer = self._buffer, []

        now = datetime.now(timezone.utc)
        key = (
            f"{LEDGER_PREFIX}{now:%Y-%m-%d}/"
            f"{now:%H%M%S}-{INSTANCE_ID}-{uuid.uuid4().hex[:8]}.jsonl"
        )
        body = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"

        try:
            self._s3.put_object(
                Bucket=LEDGER_BUCKET,
                Key=key,
                Body=body.encode(),
                ContentType="application/x-ndjson",
            )
            self.last_flush_at = time.time()
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - the buffer must not grow forever
            with self._lock:
                self._buffer = rows + self._buffer
            self.last_error = str(exc)


ledger = Ledger()


# --- Worker -----------------------------------------------------------------

_stats_lock = threading.Lock()
_stats = {"attempts": 0, "committed": 0, "rejected": 0, "gated": 0}


def _count(key: str) -> None:
    with _stats_lock:
        _stats[key] += 1


def writer_loop() -> None:
    interval = WRITE_INTERVAL_MS / 1000.0
    while True:
        epoch, _, write_region = dr_state.get()

        # Withheld on purpose, not refused by the database. Counted separately
        # so a gated write never appears as either a success or a rejection.
        if write_region is not None and write_region != REGION:
            _count("gated")
            time.sleep(interval)
            continue

        payload = f"{uuid.uuid4().hex[:12]}"
        _count("attempts")
        try:
            row_id, committed_at = db.insert(epoch, payload)
        except Exception as exc:  # noqa: BLE001 - a rejected write is data, not a crash
            db.last_error = str(exc)
            _count("rejected")
        else:
            _count("committed")
            ledger.append(
                {
                    "id": row_id,
                    "epoch": epoch,
                    "region": REGION,
                    "instance_id": INSTANCE_ID,
                    "payload": payload,
                    "committed_at": committed_at,
                }
            )
        time.sleep(interval)


def flusher_loop() -> None:
    interval = LEDGER_FLUSH_MS / 1000.0
    while True:
        time.sleep(interval)
        ledger.flush()


def _start_background() -> None:
    # Configuration must exist. A missing parameter in the standby region is a
    # deliberate drill: the process should fail to start, loudly, so that the
    # failure is attributable to configuration rather than to the database.
    for attempt in range(30):
        try:
            db.connect()
            break
        except Exception as exc:  # noqa: BLE001
            db.last_error = str(exc)
            if attempt == 29:
                raise RuntimeError(f"configuration unavailable at startup: {exc}") from exc
            time.sleep(2)

    # Schema creation is best-effort. On the standby the table arrives through
    # replication and the instance is read-only, which is expected.
    try:
        db.init_schema()
    except Exception as exc:  # noqa: BLE001
        db.last_error = str(exc)

    threading.Thread(target=writer_loop, name="writer", daemon=True).start()
    threading.Thread(target=flusher_loop, name="flusher", daemon=True).start()


# --- HTTP -------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness. Route 53 probes this; it must not depend on writability."""
    if HEALTH_DB_CHECK:
        try:
            db.ping()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "db": "down", "error": str(exc)}), 503

    return jsonify({"ok": True, "region": REGION, "instance_id": INSTANCE_ID})


@app.get("/ready")
def ready():
    """Readiness. True only while this region may write and is writing."""
    if not dr_state.may_write():
        _, _, write_region = dr_state.get()
        return (
            jsonify(
                {
                    "ready": False,
                    "region": REGION,
                    "reason": "not the write-enabled region",
                    "write_enabled_region": write_region,
                }
            ),
            503,
        )

    age = None
    if db.last_success_at is not None:
        age = round(time.time() - db.last_success_at, 3)

    writable = False
    try:
        writable = db.is_writable()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ready": False, "error": str(exc)}), 503

    ready_now = writable and age is not None and age < max(10.0, WRITE_INTERVAL_MS / 1000.0 * 10)
    return (
        jsonify(
            {
                "ready": ready_now,
                "writable": writable,
                "last_success_age_seconds": age,
                "last_error": db.last_error,
            }
        ),
        200 if ready_now else 503,
    )


@app.get("/state")
def state():
    epoch, active_region, write_region = dr_state.get()
    try:
        max_id = db.max_id()
    except Exception:  # noqa: BLE001
        max_id = None

    with _stats_lock:
        stats = dict(_stats)

    return jsonify(
        {
            "region": REGION,
            "instance_id": INSTANCE_ID,
            "epoch": epoch,
            "active_region": active_region,
            "write_enabled_region": write_region,
            "may_write": dr_state.may_write(),
            "max_id": max_id,
            "stats": stats,
            "ledger_pending": ledger.pending(),
            "ledger_last_flush_at": ledger.last_flush_at,
            "ledger_last_error": ledger.last_error,
            "started_at": APP_STARTED_AT.isoformat(),
            "uptime_seconds": round((datetime.now(timezone.utc) - APP_STARTED_AT).total_seconds(), 1),
            "write_interval_ms": WRITE_INTERVAL_MS,
        }
    )


@app.get("/")
def index():
    return jsonify(
        {
            "service": "drlab",
            "region": REGION,
            "instance_id": INSTANCE_ID,
            "endpoints": ["/health", "/ready", "/state"],
        }
    )


_start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
