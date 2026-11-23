# ---------------------------------------------------------------------------
# One password, generated once, stored in SSM SecureString in both regions.
#
# It does reach Terraform state through aws_db_instance.password, which is why
# the state bucket is KMS-encrypted with public access blocked. The alternative
# (manage_master_user_password) keeps it out of state but puts the secret in
# Secrets Manager in the source region only, which is exactly the region a
# failover is trying to leave. The tradeoff is documented in docs/configuration.md.
# ---------------------------------------------------------------------------
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "primary" {
  name        = "${var.project}-primary"
  description = "Primary RDS subnets"
  subnet_ids  = aws_subnet.primary[*].id

  tags = { Name = "${var.project}-primary" }
}

resource "aws_db_subnet_group" "dr" {
  provider = aws.dr

  name        = "${var.project}-dr"
  description = "Recovery RDS subnets"
  subnet_ids  = aws_subnet.dr[*].id

  tags = { Name = "${var.project}-dr" }
}

resource "aws_db_instance" "primary" {
  identifier = "${var.project}-primary"

  engine         = "mysql"
  engine_version = "8.0"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp2"

  # Synthetic data only. Encrypting the source would force a KMS key in the
  # destination region for the cross-region replica, which adds a key with a
  # seven-day deletion wait to every teardown for no benefit to the drill.
  storage_encrypted = false

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 3306

  db_subnet_group_name   = aws_db_subnet_group.primary.name
  vpc_security_group_ids = [aws_security_group.db_primary.id]
  publicly_accessible    = false

  # Cross-region read replicas require automated backups on the source.
  backup_retention_period = 1
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"

  # Both of these defaults break something: skip_final_snapshot = false makes
  # the first destroy fail, and deletion_protection = true makes it fail later.
  skip_final_snapshot = true
  deletion_protection = false

  apply_immediately           = true
  auto_minor_version_upgrade  = false
  allow_major_version_upgrade = false

  tags = { Name = "${var.project}-primary" }

  lifecycle {
    # The password is generated once. Rotating it would rewrite the replica's
    # credentials in the middle of a drill, so it is ignored rather than churned.
    ignore_changes = [password]
  }
}

# Cross-region asynchronous replica. This is the standby data tier. It is
# created read-only and is promoted out of band during a drill, which is why
# the drill script -- not Terraform -- is allowed to change its role.
resource "aws_db_instance" "replica" {
  provider = aws.dr

  identifier          = "${var.project}-replica"
  replicate_source_db = aws_db_instance.primary.arn
  instance_class      = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp2"

  db_subnet_group_name   = aws_db_subnet_group.dr.name
  vpc_security_group_ids = [aws_security_group.db_dr.id]
  publicly_accessible    = false

  # Binary logs on the promoted instance are what make failback possible
  # without a snapshot restore. Keep them for a day.
  backup_retention_period = 1
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"

  skip_final_snapshot = true
  deletion_protection = false

  apply_immediately          = true
  auto_minor_version_upgrade = false

  tags = { Name = "${var.project}-replica" }
}

# ---------------------------------------------------------------------------
# Configuration is state that has to be replicated too. Every parameter exists
# in both regions under the same relative name, because the application reads
# from its own region. One drill deletes a single DR parameter on purpose.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "primary_db_host" {
  name  = "/${var.project}/${local.primary_slug}/db_host"
  type  = "String"
  value = aws_db_instance.primary.address
}

resource "aws_ssm_parameter" "primary_db_name" {
  name  = "/${var.project}/${local.primary_slug}/db_name"
  type  = "String"
  value = var.db_name
}

resource "aws_ssm_parameter" "primary_db_user" {
  name  = "/${var.project}/${local.primary_slug}/db_user"
  type  = "String"
  value = var.db_username
}

resource "aws_ssm_parameter" "primary_db_password" {
  name  = "/${var.project}/${local.primary_slug}/db_password"
  type  = "SecureString"
  value = random_password.db.result
}

resource "aws_ssm_parameter" "dr_db_host" {
  provider = aws.dr

  # The DR app is pointed at the replica from the start. A promoted replica
  # keeps its own endpoint, so failover needs no database reconfiguration.
  name  = "/${var.project}/${local.dr_slug}/db_host"
  type  = "String"
  value = aws_db_instance.replica.address
}

resource "aws_ssm_parameter" "dr_db_name" {
  provider = aws.dr

  name  = "/${var.project}/${local.dr_slug}/db_name"
  type  = "String"
  value = var.db_name
}

resource "aws_ssm_parameter" "dr_db_user" {
  provider = aws.dr

  name  = "/${var.project}/${local.dr_slug}/db_user"
  type  = "String"
  value = var.db_username
}

resource "aws_ssm_parameter" "dr_db_password" {
  provider = aws.dr

  name  = "/${var.project}/${local.dr_slug}/db_password"
  type  = "SecureString"
  value = random_password.db.result
}
