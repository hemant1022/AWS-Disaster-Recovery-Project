"""Assert that nothing billable survived teardown.

This is the week-1 acceptance criterion and the regression test for the whole
project. It exits non-zero if anything in either region still carries the
project tag, or if a global resource the lab creates still exists.

    python scripts/verify_empty.py
"""

from __future__ import annotations

import argparse
import sys

import boto3

from drlib import log, terraform_output

PROJECT = "drlab"


def tagged(items, key="Tags"):
    return [i for i in items if any(t.get("Key") == "Project" and t.get("Value") == PROJECT for t in i.get(key, []))]


def check_region(region: str) -> list[str]:
    problems: list[str] = []

    ec2 = boto3.client("ec2", region_name=region)

    instances = []
    for reservation in ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [PROJECT]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )["Reservations"]:
        instances.extend(reservation["Instances"])
    for instance in instances:
        problems.append(f"{region}: EC2 instance {instance['InstanceId']} still exists")

    for address in ec2.describe_addresses()["Addresses"]:
        if address.get("Tags") and tagged([address]):
            problems.append(f"{region}: Elastic IP {address.get('PublicIp')} still allocated")

    for vpc in ec2.describe_vpcs(Filters=[{"Name": "tag:Project", "Values": [PROJECT]}])["Vpcs"]:
        problems.append(f"{region}: VPC {vpc['VpcId']} still exists")

    for nat in ec2.describe_nat_gateways(
        Filters=[{"Name": "state", "Values": ["available", "pending"]}]
    )["NatGateways"]:
        problems.append(f"{region}: NAT gateway {nat['NatGatewayId']} is running (never expected)")

    rds = boto3.client("rds", region_name=region)
    for db in rds.describe_db_instances()["DBInstances"]:
        tags = rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"]
        if any(t["Key"] == "Project" and t["Value"] == PROJECT for t in tags):
            problems.append(f"{region}: RDS instance {db['DBInstanceIdentifier']} still exists")

    ssm = boto3.client("ssm", region_name=region)
    parameters = ssm.describe_parameters(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [f"/{PROJECT}/"]}]
    )["Parameters"]
    for parameter in parameters:
        problems.append(f"{region}: SSM parameter {parameter['Name']} still exists")

    cw = boto3.client("cloudwatch", region_name=region)
    alarms = cw.describe_alarms(AlarmNamePrefix=PROJECT)["MetricAlarms"]
    for alarm in alarms:
        problems.append(f"{region}: CloudWatch alarm {alarm['AlarmName']} still exists")

    return problems


def check_global() -> list[str]:
    problems: list[str] = []

    route53 = boto3.client("route53", region_name="us-east-1")
    for check in route53.list_health_checks()["HealthChecks"]:
        tags = route53.list_tags_for_resource(
            ResourceType="healthcheck", ResourceId=check["Id"]
        )["ResourceTagSet"]["Tags"]
        if any(t["Key"] == "Project" and t["Value"] == PROJECT for t in tags):
            problems.append(f"global: Route 53 health check {check['Id']} still exists")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", default="terraform/persistent")
    args = parser.parse_args()

    try:
        outputs = terraform_output(args.outputs)
        regions = [outputs["state_region"]]
    except Exception:  # noqa: BLE001
        regions = []

    regions = list(dict.fromkeys(regions + ["us-east-1", "us-east-2"]))

    problems: list[str] = []
    for region in regions:
        log(f"checking {region}")
        problems.extend(check_region(region))
    problems.extend(check_global())

    if problems:
        log("teardown incomplete:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    log("clean: no billable lab resources found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
