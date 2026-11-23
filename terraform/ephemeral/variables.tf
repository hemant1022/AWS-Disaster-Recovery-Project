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
  description = "Public zone name owned by the persistent root. Records are created under app.<domain_name>."
  type        = string
}

variable "app_port" {
  description = "Port the container listens on. Also the port the health check probes."
  type        = number
  default     = 8080
}

variable "instance_type" {
  description = "Application instance size. t3.micro is free-tier eligible."
  type        = string
  default     = "t3.micro"
}

variable "db_instance_class" {
  description = "RDS instance size. db.t3.micro is free-tier eligible."
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  description = "Initial database created on the primary."
  type        = string
  default     = "drlab"
}

variable "db_username" {
  description = "Master username. The password is generated and stored in SSM, never typed."
  type        = string
  default     = "drlab_admin"
}

variable "write_interval_ms" {
  description = "How often the app commits a row. 200 is the baseline, 20 is the load test."
  type        = number
  default     = 200

  validation {
    condition     = var.write_interval_ms >= 10 && var.write_interval_ms <= 60000
    error_message = "Keep the write interval between 10 and 60000 milliseconds."
  }
}

variable "ledger_flush_ms" {
  description = "How often the app flushes its committed-row ledger to S3."
  type        = number
  default     = 2000

  validation {
    condition     = var.ledger_flush_ms >= 500 && var.ledger_flush_ms <= 60000
    error_message = "Keep the ledger flush interval between 500 and 60000 milliseconds."
  }
}

variable "health_check_request_interval" {
  description = "Route 53 health-check probe interval: 10 or 30 seconds."
  type        = number
  default     = 10

  validation {
    condition     = contains([10, 30], var.health_check_request_interval)
    error_message = "Route 53 only accepts 10 or 30 seconds."
  }
}

variable "health_check_failure_threshold" {
  description = "Consecutive failed probes before the endpoint is unhealthy. Minimum 3."
  type        = number
  default     = 3

  validation {
    condition     = var.health_check_failure_threshold >= 3 && var.health_check_failure_threshold <= 10
    error_message = "Route 53 requires a threshold between 3 and 10."
  }
}

variable "dns_ttl" {
  description = "TTL on the failover records. The sensitivity table compares 60 against 300."
  type        = number
  default     = 60

  validation {
    condition     = contains([60, 300], var.dns_ttl)
    error_message = "Use 60 or 300 so the published sensitivity table has two comparable points."
  }
}

variable "replica_lag_alarm_seconds" {
  description = "ReplicaLag threshold that raises the DR alarm."
  type        = number
  default     = 60
}
