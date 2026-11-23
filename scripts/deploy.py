"""Deploy a pinned image digest to both application instances over SSM.

The pipeline builds and pushes the image; this script only ships it. It never
touches the database, DNS, or Terraform state.

Targets come from SSM parameters published by the ephemeral root, so this works
identically on a laptop and on a GitHub runner without access to Terraform
state.

    python scripts/deploy.py --digest sha256:abc123...
    python scripts/deploy.py --digest sha256:abc... --region primary
    python scripts/deploy.py --digest sha256:abc... --source terraform
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import boto3

from drlib import http_json, log, terraform_output

PROJECT = "drlab"


def targets_from_ssm() -> dict:
    region = os.environ.get("AWS_REGION_PRIMARY", "us-east-1")
    ssm = boto3.client("ssm", region_name=region)

    keys = [
        "primary_region",
        "dr_region",
        "primary_instance_id",
        "dr_instance_id",
        "primary_eip",
        "dr_eip",
        "app_port",
    ]
    response = ssm.get_parameters(
        Names=[f"/{PROJECT}/deploy/{key}" for key in keys], WithDecryption=False
    )
    found = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in response["Parameters"]}
    missing = [key for key in keys if key not in found]
    if missing:
        raise RuntimeError(
            f"missing deploy parameters: {missing}. Apply the ephemeral root first."
        )
    found["app_port"] = int(found["app_port"])
    return found


def targets_from_terraform(root: str) -> dict:
    outputs = terraform_output(root)
    summary = outputs["session_summary"]
    return {
        "primary_region": summary["primary_region"],
        "dr_region": summary["dr_region"],
        "primary_instance_id": outputs["primary_instance_id"],
        "dr_instance_id": outputs["dr_instance_id"],
        "primary_eip": summary["primary_eip"],
        "dr_eip": summary["dr_eip"],
        "app_port": outputs["app_port"],
    }


def send(ssm, instance_id: str, commands: list[str], comment: str) -> str:
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment=comment,
        Parameters={"commands": commands, "executionTimeout": ["300"]},
    )
    return response["Command"]["CommandId"]


def wait_for_command(ssm, command_id: str, instance_id: str, timeout: int = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        status = result["Status"]
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            output = (result.get("StandardOutputContent") or "")[-2000:]
            error = (result.get("StandardErrorContent") or "")[-2000:]
            if status != "Success":
                raise RuntimeError(
                    f"SSM command {command_id} on {instance_id} ended {status}\n"
                    f"stdout:\n{output}\nstderr:\n{error}"
                )
            return output
        time.sleep(3)
    raise TimeoutError(f"SSM command {command_id} did not finish within {timeout}s")


def deploy_one(
    region: str,
    instance_id: str,
    image: str,
    eip: str,
    app_port: int,
    start: bool,
) -> None:
    ssm = boto3.client("ssm", region_name=region)

    log(f"{region}: writing image.env on {instance_id}")
    command_id = send(
        ssm,
        instance_id,
        [
            "set -euo pipefail",
            "install -d -m 0755 /opt/drlab",
            f"printf 'APP_IMAGE=%s\\n' '{image}' > /opt/drlab/image.env",
            "chmod 0644 /opt/drlab/image.env",
            ". /opt/drlab/config.env",
            'APP_IMAGE="$(sed -n \'s/^APP_IMAGE=//p\' /opt/drlab/image.env)"',
            'REGISTRY="${APP_IMAGE%%/*}"',
            'aws ecr get-login-password --region "$APP_REGION" '
            '| docker login --username AWS --password-stdin "$REGISTRY"',
            'docker pull "$APP_IMAGE"',
        ],
        comment="drlab deploy: pull pinned digest",
    )
    wait_for_command(ssm, command_id, instance_id)

    if not start:
        # Pre-pull only. If the standby happens to be running, restart it so it
        # picks up the new digest; otherwise leave it stopped and just cached.
        command_id = send(
            ssm,
            instance_id,
            [
                "set -euo pipefail",
                "if systemctl is-active --quiet drlab-app; then "
                "systemctl restart drlab-app; fi",
            ],
            comment="drlab deploy: refresh standby if running",
        )
        wait_for_command(ssm, command_id, instance_id)
        log(f"{region}: image cached, standby left stopped")
        return

    log(f"{region}: starting drlab-app")
    command_id = send(
        ssm,
        instance_id,
        [
            "set -euo pipefail",
            "systemctl reset-failed drlab-app || true",
            # The unit is Type=oneshot with RemainAfterExit, so `start` on an
            # already-active service is a no-op. `restart` re-runs ExecStart,
            # which pulls the new digest and replaces the container.
            "systemctl restart drlab-app",
            "systemctl is-active drlab-app",
        ],
        comment="drlab deploy: restart",
    )
    wait_for_command(ssm, command_id, instance_id)

    log(f"{region}: waiting for /health on {eip}:{app_port}")
    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        status, body = http_json(f"http://{eip}:{app_port}/health")
        if status == 200:
            log(f"{region}: healthy")
            return
        last = (status, body)
        time.sleep(3)
    raise TimeoutError(f"{region}: /health did not become ready (last: {last})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest", required=True, help="sha256:... image digest")
    parser.add_argument("--region", choices=["primary", "dr", "both"], default="both")
    parser.add_argument("--source", choices=["ssm", "terraform"], default="ssm")
    parser.add_argument("--outputs", default="terraform/ephemeral")
    parser.add_argument("--no-start-standby", dest="start_standby", action="store_false")
    parser.add_argument(
        "--registry-region",
        help="Pull from this region's registry instead of each target's own. "
        "An escape hatch for a registry outage, not the normal path.",
    )
    args = parser.parse_args()

    targets = (
        targets_from_ssm()
        if args.source == "ssm"
        else targets_from_terraform(args.outputs)
    )

    account = boto3.client("sts").get_caller_identity()["Account"]

    def image_for(region: str) -> str:
        registry_region = args.registry_region or region
        return f"{account}.dkr.ecr.{registry_region}.amazonaws.com/{PROJECT}/app@{args.digest}"

    plan = []
    if args.region in ("primary", "both"):
        plan.append(
            (
                targets["primary_region"],
                targets["primary_instance_id"],
                image_for(targets["primary_region"]),
                targets["primary_eip"],
                True,
            )
        )
    if args.region in ("dr", "both"):
        plan.append(
            (
                targets["dr_region"],
                targets["dr_instance_id"],
                image_for(targets["dr_region"]),
                targets["dr_eip"],
                args.start_standby,
            )
        )

    for region, instance_id, image, eip, start in plan:
        deploy_one(region, instance_id, image, eip, targets["app_port"], start)

    log("deploy complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
