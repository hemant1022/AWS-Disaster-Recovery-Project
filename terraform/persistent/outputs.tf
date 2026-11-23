output "zone_id" {
  description = "Hosted zone ID for failover records."
  value       = aws_route53_zone.public.zone_id
}

output "zone_name_servers" {
  description = "Delegate these four NS records from FreeDNS."
  value       = aws_route53_zone.public.name_servers
}

output "evidence_bucket" {
  description = "Bucket holding drill ledgers and logs."
  value       = aws_s3_bucket.evidence.bucket
}

output "ecr_repository_primary" {
  description = "Image repository in the primary region."
  value       = aws_ecr_repository.primary.repository_url
}

output "ecr_repository_dr" {
  description = "Image repository in the recovery region."
  value       = aws_ecr_repository.dr.repository_url
}

output "role_app_deploy_arn" {
  description = "Role for the application pipeline."
  value       = aws_iam_role.app_deploy.arn
}

output "role_terraform_arn" {
  description = "Role for the infrastructure workflow."
  value       = aws_iam_role.terraform.arn
}

output "sns_topic_primary_arn" {
  description = "Alarm topic in the primary region."
  value       = aws_sns_topic.alarms.arn
}

output "sns_topic_dr_arn" {
  description = "Alarm topic in the recovery region."
  value       = aws_sns_topic.alarms_dr.arn
}
