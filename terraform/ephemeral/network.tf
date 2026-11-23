data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    Root      = "ephemeral"
  }

  # Short region slugs keep SSM parameter paths readable:
  #   /drlab/use1/db_host, /drlab/use2/db_host
  region_slugs = {
    "us-east-1"      = "use1"
    "us-east-2"      = "use2"
    "us-west-1"      = "usw1"
    "us-west-2"      = "usw2"
    "eu-west-1"      = "euw1"
    "eu-west-2"      = "euw2"
    "eu-central-1"   = "euc1"
    "ap-south-1"     = "aps1"
    "ap-southeast-1" = "apse1"
    "ap-southeast-2" = "apse2"
  }

  primary_slug = lookup(local.region_slugs, var.primary_region, replace(var.primary_region, "-", ""))
  dr_slug      = lookup(local.region_slugs, var.dr_region, replace(var.dr_region, "-", ""))

  primary_cidr = "10.20.0.0/16"
  dr_cidr      = "10.21.0.0/16"

  evidence_bucket = "${var.project}-evidence-${local.account_id}"
}

data "aws_availability_zones" "primary" {
  state = "available"
}

data "aws_availability_zones" "dr" {
  provider = aws.dr
  state    = "available"
}

# ---------------------------------------------------------------------------
# Primary region network. No NAT gateway anywhere: instances sit in public
# subnets and reach ECR and SSM through the internet gateway. A NAT gateway is
# ~$32/month and is the single most common way a small lab blows its budget.
# ---------------------------------------------------------------------------
resource "aws_vpc" "primary" {
  cidr_block           = local.primary_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project}-primary-vpc" }
}

resource "aws_internet_gateway" "primary" {
  vpc_id = aws_vpc.primary.id

  tags = { Name = "${var.project}-primary-igw" }
}

resource "aws_subnet" "primary" {
  count = 2

  vpc_id                  = aws_vpc.primary.id
  cidr_block              = cidrsubnet(local.primary_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.primary.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-primary-public-${count.index + 1}" }
}

resource "aws_route_table" "primary" {
  vpc_id = aws_vpc.primary.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.primary.id
  }

  tags = { Name = "${var.project}-primary-rt" }
}

resource "aws_route_table_association" "primary" {
  count = length(aws_subnet.primary)

  subnet_id      = aws_subnet.primary[count.index].id
  route_table_id = aws_route_table.primary.id
}

resource "aws_security_group" "app_primary" {
  name        = "${var.project}-app"
  description = "Application instance. Port ${var.app_port} is open to Route 53 health checkers."
  vpc_id      = aws_vpc.primary.id

  ingress {
    description = "Application port, probed by Route 53 and reachable by a human"
    from_port   = var.app_port
    to_port     = var.app_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Outbound to ECR, SSM, S3 and RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-app-primary" }
}

resource "aws_security_group" "db_primary" {
  name        = "${var.project}-db"
  description = "RDS. MySQL only, and only from the application security group."
  vpc_id      = aws_vpc.primary.id

  ingress {
    description     = "MySQL from the application instances"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.app_primary.id]
  }

  tags = { Name = "${var.project}-db-primary" }
}

# ---------------------------------------------------------------------------
# Recovery region network. Same shape, different CIDR.
# ---------------------------------------------------------------------------
resource "aws_vpc" "dr" {
  provider = aws.dr

  cidr_block           = local.dr_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project}-dr-vpc" }
}

resource "aws_internet_gateway" "dr" {
  provider = aws.dr
  vpc_id   = aws_vpc.dr.id

  tags = { Name = "${var.project}-dr-igw" }
}

resource "aws_subnet" "dr" {
  provider = aws.dr
  count    = 2

  vpc_id                  = aws_vpc.dr.id
  cidr_block              = cidrsubnet(local.dr_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.dr.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-dr-public-${count.index + 1}" }
}

resource "aws_route_table" "dr" {
  provider = aws.dr
  vpc_id   = aws_vpc.dr.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.dr.id
  }

  tags = { Name = "${var.project}-dr-rt" }
}

resource "aws_route_table_association" "dr" {
  provider = aws.dr
  count    = length(aws_subnet.dr)

  subnet_id      = aws_subnet.dr[count.index].id
  route_table_id = aws_route_table.dr.id
}

resource "aws_security_group" "app_dr" {
  provider = aws.dr

  name        = "${var.project}-app"
  description = "Application instance. Port ${var.app_port} is open to Route 53 health checkers."
  vpc_id      = aws_vpc.dr.id

  ingress {
    description = "Application port, probed by Route 53 and reachable by a human"
    from_port   = var.app_port
    to_port     = var.app_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Outbound to ECR, SSM, S3 and RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-app-dr" }
}

resource "aws_security_group" "db_dr" {
  provider = aws.dr

  name        = "${var.project}-db"
  description = "RDS. MySQL only, and only from the application security group."
  vpc_id      = aws_vpc.dr.id

  ingress {
    description     = "MySQL from the application instances"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.app_dr.id]
  }

  tags = { Name = "${var.project}-db-dr" }
}
