# ---------------------------------------------------------------------------
# Instance role. Global (IAM is not regional), used by both instances.
# The app reads its configuration from SSM and writes its ledger to S3, so it
# needs exactly those two permissions plus ECR read and SSM management.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.project}-app"
  description        = "Application instance. Reads configuration, writes the ledger, pulls images."
  assume_role_policy = data.aws_iam_policy_document.app_assume.json
}

resource "aws_iam_role_policy_attachment" "app_ssm_core" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "app_ecr_read" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "ReadConfiguration"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:*:${local.account_id}:parameter/${var.project}/*"]
  }

  statement {
    sid       = "DecryptSecureParameters"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:*:${local.account_id}:key/*"]
  }

  statement {
    sid       = "WriteLedger"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${local.evidence_bucket}/ledger/*"]
  }

  statement {
    sid       = "ReadDrState"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${local.evidence_bucket}/state/*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "app-runtime"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project}-app"
  role = aws_iam_role.app.name
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# AMI ids are regional. The recovery instance needs its own lookup or it will
# try to launch an image that does not exist in that region.
data "aws_ami" "al2023_dr" {
  provider = aws.dr

  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# ---------------------------------------------------------------------------
# Primary application instance.
# ---------------------------------------------------------------------------
resource "aws_instance" "primary" {
  ami           = data.aws_ami.al2023.id
  instance_type = var.instance_type

  subnet_id                   = aws_subnet.primary[0].id
  vpc_security_group_ids      = [aws_security_group.app_primary.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true

  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region            = var.primary_region
    ssm_prefix        = "/${var.project}/${local.primary_slug}"
    evidence_bucket   = local.evidence_bucket
    ledger_prefix     = "ledger/${var.primary_region}/"
    state_key         = "state/dr-state.json"
    app_port          = var.app_port
    write_interval_ms = var.write_interval_ms
    ledger_flush_ms   = var.ledger_flush_ms
  })

  root_block_device {
    volume_type = "gp3"
    volume_size = 10
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = { Name = "${var.project}-app-primary" }

  depends_on = [
    aws_ssm_parameter.primary_db_host,
    aws_ssm_parameter.primary_db_password,
  ]
}

resource "aws_eip" "primary" {
  domain = "vpc"

  tags = { Name = "${var.project}-app-primary" }
}

resource "aws_eip_association" "primary" {
  instance_id   = aws_instance.primary.id
  allocation_id = aws_eip.primary.id
}

# ---------------------------------------------------------------------------
# Standby application instance. Same image, same code, different region.
# It is deployed and left running against the read-only replica, so its writes
# fail until promotion. That is the intended state: no restart is inside the
# measured RTO.
# ---------------------------------------------------------------------------
resource "aws_instance" "dr" {
  provider = aws.dr

  ami           = data.aws_ami.al2023_dr.id
  instance_type = var.instance_type

  subnet_id                   = aws_subnet.dr[0].id
  vpc_security_group_ids      = [aws_security_group.app_dr.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true

  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region            = var.dr_region
    ssm_prefix        = "/${var.project}/${local.dr_slug}"
    evidence_bucket   = local.evidence_bucket
    ledger_prefix     = "ledger/${var.dr_region}/"
    state_key         = "state/dr-state.json"
    app_port          = var.app_port
    write_interval_ms = var.write_interval_ms
    ledger_flush_ms   = var.ledger_flush_ms
  })

  root_block_device {
    volume_type = "gp3"
    volume_size = 10
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = { Name = "${var.project}-app-dr" }

  depends_on = [
    aws_ssm_parameter.dr_db_host,
    aws_ssm_parameter.dr_db_password,
  ]
}

resource "aws_eip" "dr" {
  provider = aws.dr
  domain   = "vpc"

  tags = { Name = "${var.project}-app-dr" }
}

resource "aws_eip_association" "dr" {
  provider = aws.dr

  instance_id   = aws_instance.dr.id
  allocation_id = aws_eip.dr.id
}
