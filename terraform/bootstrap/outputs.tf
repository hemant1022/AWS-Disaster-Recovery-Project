output "state_bucket" {
  description = "Name of the bucket holding remote state."
  value       = aws_s3_bucket.tfstate.bucket
}

output "state_region" {
  description = "Region of the state bucket."
  value       = var.state_region
}

output "backend_hcl" {
  description = "Paste into terraform/<root>/backend.hcl."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.bucket}"
    region       = "${var.state_region}"
    use_lockfile = true
    encrypt      = true
  EOT
}
