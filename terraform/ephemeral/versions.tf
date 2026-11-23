terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60, < 7.0"
    }

    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
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

# Route 53 health-check metrics exist only in us-east-1 regardless of where the
# endpoint sits, so the alarms that watch them have to be created there too.
provider "aws" {
  alias  = "global"
  region = "us-east-1"

  default_tags {
    tags = local.tags
  }
}
