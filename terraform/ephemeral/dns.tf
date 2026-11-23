# ---------------------------------------------------------------------------
# The zone lives in the persistent root; this root only adds and removes
# records, which is safe to do per session.
#
# One plain A record, rewritten by the drill when the recovery region is
# declared active. Deliberately not a Route 53 failover pair: AWS rejects a
# non-alias PRIMARY record without a health check, and attaching one makes
# Route 53 flip the record the moment the primary looks sick -- before the
# replica has been promoted -- so every request lands on a read-only database.
# The health checks below still exist and still drive the detection alarm.
# ---------------------------------------------------------------------------
data "aws_route53_zone" "public" {
  name         = var.domain_name
  private_zone = false
}

# Health checks target the Elastic IP directly, never a name that resolves
# through the record being flipped. That would be circular.
resource "aws_route53_health_check" "primary" {
  provider = aws.global

  type              = "HTTP"
  ip_address        = aws_eip.primary.public_ip
  port              = var.app_port
  resource_path     = "/health"
  request_interval  = var.health_check_request_interval
  failure_threshold = var.health_check_failure_threshold

  tags = {
    Name = "${var.project}-app-primary"
  }
}

resource "aws_route53_health_check" "dr" {
  provider = aws.global

  type              = "HTTP"
  ip_address        = aws_eip.dr.public_ip
  port              = var.app_port
  resource_path     = "/health"
  request_interval  = var.health_check_request_interval
  failure_threshold = var.health_check_failure_threshold

  tags = {
    Name = "${var.project}-app-dr"
  }
}

resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.public.zone_id
  name    = "app.${var.domain_name}"
  type    = "A"
  ttl     = var.dns_ttl
  records = [aws_eip.primary.public_ip]
}
