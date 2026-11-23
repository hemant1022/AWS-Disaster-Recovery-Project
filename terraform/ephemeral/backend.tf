terraform {
  backend "s3" {
    # bucket, region, use_lockfile and encrypt come from backend.hcl at init time.
    key = "ephemeral/terraform.tfstate"
  }
}
