variable "project" {
  description = "Name prefix for every resource and cost-allocation tag."
  type        = string
  default     = "drlab"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,20}$", var.project))
    error_message = "Use 2-21 lowercase letters, digits, or hyphens, starting with a letter or digit."
  }
}

variable "state_region" {
  description = "Region that holds the Terraform state bucket."
  type        = string
  default     = "us-east-1"
}
