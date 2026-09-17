# Every input, and why it is one.
#
# The rule applied here: a value is a variable when a second environment would
# genuinely choose differently, and a local when it would not. A knob nobody
# turns is a knob somebody has to read.

variable "project" {
  description = "Name prefix for every resource. One system per name."
  type        = string
  default     = "threshold"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "project must be lowercase alphanumeric with hyphens, 3-21 characters."
  }
}

variable "environment" {
  description = "Which deployment this is. Named rather than free text: the application's own Settings accepts exactly these."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production; local and ci do not run here."
  }
}

variable "region" {
  description = "AWS region. Ireland by default because the seeded vendors are European and data residency is the kind of thing that is decided once."
  type        = string
  default     = "eu-west-1"
}

variable "vpc_cidr" {
  description = "Address space for the VPC. /16 leaves room for the subnets below without arithmetic in three places."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "availability_zones" {
  description = "How many AZs to spread across. RDS multi-AZ and an ALB both need at least two."
  type        = number
  default     = 2

  validation {
    condition     = var.availability_zones >= 2 && var.availability_zones <= 3
    error_message = "availability_zones must be 2 or 3: one is not survivable and four is not cheaper."
  }
}

variable "log_level" {
  description = "Application log level."
  type        = string
  default     = "INFO"
}

variable "database_instance_class" {
  description = "RDS instance class. This database is the job queue as well as the ledger, so it is sized for writes rather than for storage."
  type        = string
  default     = "db.t4g.medium"
}

variable "database_allocated_storage" {
  description = "Gigabytes. Autoscaling is enabled, so this is a floor rather than a guess at the ceiling."
  type        = number
  default     = 50
}

variable "api_desired_count" {
  description = "How many API tasks. Stateless, so this is a throughput dial."
  type        = number
  default     = 2
}

variable "worker_desired_count" {
  description = "How many worker tasks. See the comment in compute.tf: more than one is what the reaper's advisory lock does not yet handle correctly."
  type        = number
  default     = 1
}

variable "image_tag" {
  description = "Image tag for both services. They are built from one Dockerfile and deploy together, because a worker running older code than the API is the deploy-ordering failure this project has already had once."
  type        = string
  default     = "latest"
}

variable "ecr_repository_url" {
  description = "Where the image lives. An input because building and pushing it is a different pipeline from describing where it runs."
  type        = string
}

variable "oidc_issuer" {
  description = "The identity provider's issuer URL. An input, not infrastructure: an existing IdP is the normal case and standing up a new one is a decision this module should not make for anybody."
  type        = string
}

variable "oidc_audience" {
  description = "The audience claim the API requires."
  type        = string
  default     = "threshold-api"
}

variable "otlp_endpoint" {
  description = "Where traces and metrics go. The application speaks OTLP and does not care what receives it."
  type        = string
  default     = "http://localhost:4318"
}

variable "alarm_topic_arn" {
  description = "SNS topic for alarms. Empty means the alarms are still created and simply have nowhere to send, which is better than not creating them."
  type        = string
  default     = ""
}

variable "api_domain_name" {
  description = "Hostname the API is served on. An input because the DNS zone belongs to whoever owns the domain, and validating the certificate is their record to create."
  type        = string
}
