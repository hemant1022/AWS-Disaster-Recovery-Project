output "app_fqdn" {
  description = "Failover name. Resolves to the primary Elastic IP while it is healthy."
  value       = "app.${var.domain_name}"
}

output "app_port" {
  description = "Port the application listens on."
  value       = var.app_port
}

output "zone_id" {
  description = "Hosted zone id, used by the DNS flip step."
  value       = data.aws_route53_zone.public.zone_id
}

output "dns_ttl" {
  description = "TTL on the failover records, for the sensitivity table."
  value       = var.dns_ttl
}

output "primary_eip" {
  description = "Primary application Elastic IP. Also the health-check target."
  value       = aws_eip.primary.public_ip
}

output "dr_eip" {
  description = "Standby application Elastic IP. Also the health-check target."
  value       = aws_eip.dr.public_ip
}

output "primary_instance_id" {
  description = "Primary EC2 instance, for SSM commands."
  value       = aws_instance.primary.id
}

output "dr_instance_id" {
  description = "Standby EC2 instance, for SSM commands."
  value       = aws_instance.dr.id
}

output "primary_db_endpoint" {
  description = "Primary RDS endpoint. Disappears when the primary is terminated."
  value       = aws_db_instance.primary.address
}

output "dr_db_endpoint" {
  description = "Replica endpoint. Survives promotion unchanged."
  value       = aws_db_instance.replica.address
}

output "primary_db_identifier" {
  description = "Primary RDS identifier, for failover commands."
  value       = aws_db_instance.primary.identifier
}

output "dr_db_identifier" {
  description = "Replica identifier, for promotion commands."
  value       = aws_db_instance.replica.identifier
}

output "health_check_primary_id" {
  description = "Route 53 health check watching the primary."
  value       = aws_route53_health_check.primary.id
}

output "health_check_dr_id" {
  description = "Route 53 health check watching the standby."
  value       = aws_route53_health_check.dr.id
}

output "ssm_prefix_primary" {
  description = "Parameter path the primary instance reads at startup."
  value       = "/${var.project}/${local.primary_slug}"
}

output "ssm_prefix_dr" {
  description = "Parameter path the standby instance reads at startup."
  value       = "/${var.project}/${local.dr_slug}"
}

output "evidence_bucket" {
  description = "Bucket holding drill ledgers, state and logs."
  value       = local.evidence_bucket
}

output "ledger_prefix" {
  description = "Key prefix for committed-row ledger objects."
  value       = "ledger/"
}

output "session_summary" {
  description = "Copy this into the drill log header."
  value = {
    primary_region = var.primary_region
    dr_region      = var.dr_region
    app_fqdn       = "app.${var.domain_name}"
    primary_eip    = aws_eip.primary.public_ip
    dr_eip         = aws_eip.dr.public_ip
    db_primary     = aws_db_instance.primary.address
    db_replica     = aws_db_instance.replica.address
  }
}
