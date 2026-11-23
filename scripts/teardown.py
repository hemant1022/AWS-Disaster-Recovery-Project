"""Destroy the ephemeral root and prove nothing survived.

Destroy order matters and Terraform handles most of it, but a drill leaves
state that Terraform does not know about: a promoted replica, a deleted
primary, a stopped instance. Each is handled explicitly before the destroy.

    python scripts/teardown.py --yes
    python scripts/teardown.py --yes --skip-verify
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import boto3

from drlib import log, terraform_output


def run(command: list[str]) -> int:
    log(" ".join(command))
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--outputs", default="terraform/ephemeral")
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()

    if not args.yes:
        answer = input("Destroy the ephemeral root? Type 'destroy' to continue: ")
        if answer.strip() != "destroy":
            log("aborted")
            return 1

    # Inspect the recovery database before the destroy so a promoted replica or
    # an already-deleted primary is visible in the log rather than surfacing as
    # an opaque dependency error halfway through.
    try:
        outputs = terraform_output(args.outputs)
        summary = outputs["session_summary"]

        rds = boto3.client("rds", region_name=summary["dr_region"])
        try:
            instance = rds.describe_db_instances(
                DBInstanceIdentifier=outputs["dr_db_identifier"]
            )["DBInstances"][0]
            log(f"replica status: {instance['DBInstanceStatus']}")
        except rds.exceptions.DBInstanceNotFoundFault:
            log("replica already gone")
    except Exception as exc:  # noqa: BLE001
        log(f"could not inspect the recovery database: {exc}")

    code = run(
        [
            "terraform",
            f"-chdir={args.outputs}",
            "destroy",
            "-auto-approve",
            "-input=false",
            "-no-color",
        ]
    )
    if code != 0:
        log("terraform destroy failed; inspect the output above")
        return code

    if not args.skip_verify:
        return run([sys.executable, "scripts/verify_empty.py"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
