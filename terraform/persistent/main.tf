terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60, < 7.0"
    }
  }
}

provider "aws" {
  region = var.primary_region

  default_tags {
    tags = local.tags
  }
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    Root      = "persistent"
  }
}

# ---------------------------------------------------------------------------
# Public DNS zone for failover. Never destroyed: a recreated zone gets a new
# set of nameservers and silently breaks the FreeDNS delegation.
# ---------------------------------------------------------------------------
resource "aws_route53_zone" "public" {
  name    = var.domain_name
  comment = "${var.project} failover zone"

  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Evidence bucket: drill ledgers, raw logs, results. Survives teardown so the
# numbers outlive the infrastructure that produced them.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "evidence" {
  bucket = "${var.project}-evidence-${local.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-old-evidence"
    status = "Enabled"

    filter {}

    expiration {
      days = var.evidence_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "evidence" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.evidence.arn,
      "${aws_s3_bucket.evidence.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence.json

  depends_on = [aws_s3_bucket_public_access_block.evidence]
}

# ---------------------------------------------------------------------------
# One image repository per region. Pushing to both in one workflow is simpler
# than ECR replication and removes the race where the standby boots before the
# image has landed.
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "primary" {
  name                 = "${var.project}/app"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "dr" {
  provider = aws.dr

  name                 = "${var.project}/app"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

locals {
  ecr_lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "primary" {
  repository = aws_ecr_repository.primary.name
  policy     = local.ecr_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "dr" {
  provider   = aws.dr
  repository = aws_ecr_repository.dr.name
  policy     = local.ecr_lifecycle_policy
}

# ---------------------------------------------------------------------------
# Alarm notification. Two topics, one subscriber: CloudWatch alarm actions
# cannot cross regions, so a single topic would go quiet with its region.
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic" "alarms_dr" {
  provider = aws.dr
  name     = "${var.project}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email_dr" {
  provider  = aws.dr
  topic_arn = aws_sns_topic.alarms_dr.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# GitHub OIDC. No stored AWS credentials in the repository.
# ---------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS validates this provider through its own trusted root, so the thumbprint
  # is informational. The value below is GitHub's current root CA thumbprint.
  thumbprint_list = ["1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

data "aws_iam_policy_document" "github_app_deploy_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:${var.github_environment_app}"]
    }
  }
}

resource "aws_iam_role" "app_deploy" {
  name               = "${var.project}-github-app-deploy"
  description        = "Pushes images to ECR and deploys over SSM. No infrastructure rights."
  assume_role_policy = data.aws_iam_policy_document.github_app_deploy_assume.json
}

data "aws_iam_policy_document" "app_deploy" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushPull"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]

    resources = [
      aws_ecr_repository.primary.arn,
      aws_ecr_repository.dr.arn,
    ]
  }

  statement {
    sid     = "SsmSendCommand"
    effect  = "Allow"
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ec2:${var.primary_region}:${local.account_id}:instance/*",
      "arn:aws:ec2:${var.dr_region}:${local.account_id}:instance/*",
      "arn:aws:ssm:${var.primary_region}::document/AWS-RunShellScript",
      "arn:aws:ssm:${var.dr_region}::document/AWS-RunShellScript",
    ]
  }

  # The deploy script resolves its targets from these parameters before it can
  # send a command. Without this the pipeline authenticates and then fails.
  statement {
    sid    = "ReadDeployTargets"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]

    resources = [
      "arn:aws:ssm:${var.primary_region}:${local.account_id}:parameter/${var.project}/deploy/*",
    ]
  }

  statement {
    sid    = "SsmReadResult"
    effect = "Allow"

    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:DescribeInstanceInformation",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "app_deploy" {
  name   = "app-deploy"
  role   = aws_iam_role.app_deploy.id
  policy = data.aws_iam_policy_document.app_deploy.json
}

data "aws_iam_policy_document" "github_terraform_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:environment:${var.github_environment_terraform}",
        "repo:${var.github_repo}:environment:${var.github_environment_drill}",
      ]
    }
  }
}

# Deliberately broad, and deliberately separate from the deploy role. Writing a
# minimal policy for a principal that creates VPCs, RDS, KMS, IAM and Route 53
# across two regions is a multi-day exercise with no learning value here.
# The role is reachable only from a protected environment on one repository.
resource "aws_iam_role" "terraform" {
  name               = "${var.project}-github-terraform"
  description        = "Runs Terraform plan and apply from a protected environment."
  assume_role_policy = data.aws_iam_policy_document.github_terraform_assume.json
}

resource "aws_iam_role_policy_attachment" "terraform_admin" {
  role       = aws_iam_role.terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
