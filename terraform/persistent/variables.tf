variable "project" {
  description = "Name prefix for every resource and cost-allocation tag."
  type        = string
  default     = "drlab"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,20}$", var.project))
    error_message = "Use 2-21 lowercase letters, digits, or hyphens, starting with a letter or digit."
  }
}

variable "primary_region" {
  description = "Region running the live application."
  type        = string
  default     = "us-east-1"
}

variable "dr_region" {
  description = "Region running the standby."
  type        = string
  default     = "us-east-2"

  validation {
    condition     = var.dr_region != var.primary_region
    error_message = "The recovery region must differ from the primary region."
  }
}

variable "domain_name" {
  description = "Public zone name. Must match the FreeDNS subdomain you delegate."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]*\\.[a-z]{2,}$", var.domain_name))
    error_message = "Use a lowercase fully qualified name, for example dr-demo.mooo.com."
  }

  validation {
    condition     = var.domain_name != "dr-demo.example.com"
    error_message = "Replace the placeholder with the FreeDNS subdomain you will actually delegate."
  }
}

variable "alert_email" {
  description = "Address subscribed to the CloudWatch alarm topics."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Use a valid email address."
  }
}

variable "github_repo" {
  description = "owner/repository allowed to assume the OIDC roles."
  type        = string
  default     = "hemant1022/AWS-Disaster-Recovery-Project"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repo))
    error_message = "Use the owner/repository form, for example hemant1022/AWS-Disaster-Recovery-Project."
  }
}

variable "github_owner_id" {
  description = "Numeric ID of the GitHub owner, embedded in the immutable OIDC subject."
  type        = string
  default     = "166102426"
}

variable "github_repo_id" {
  description = "Numeric ID of the GitHub repository, embedded in the immutable OIDC subject."
  type        = string
  default     = "1466463063"
}

variable "github_environment_app" {
  description = "GitHub Environment allowed to push images and deploy."
  type        = string
  default     = "app-deploy"
}

variable "github_environment_terraform" {
  description = "GitHub Environment allowed to run Terraform."
  type        = string
  default     = "terraform"
}

variable "github_environment_drill" {
  description = "GitHub Environment allowed to declare a disaster and run the drill."
  type        = string
  default     = "drill"
}

variable "evidence_retention_days" {
  description = "How long drill evidence stays in S3 before expiring."
  type        = number
  default     = 30

  validation {
    condition     = var.evidence_retention_days >= 7 && var.evidence_retention_days <= 365
    error_message = "Keep evidence between 7 and 365 days."
  }
}
