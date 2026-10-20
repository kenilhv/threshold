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
    # 17 characters, because "<project>-production-api" has to fit the
    # 32-character limit on load balancer and target group names.
    condition     = can(regex("^[a-z][a-z0-9-]{2,16}$", var.project))
    error_message = "project must be lowercase alphanumeric with hyphens, 3-17 characters."
  }
}

variable "ingress_cidrs" {
  description = "Who may reach the load balancer. The ingest endpoint is deliberately unauthenticated, so a measurement run should list only the address doing the measuring; `make aws-up` fills this with the operator's own /32. Pass 0.0.0.0/0 on purpose, never by default."
  type        = list(string)

  validation {
    condition     = length(var.ingress_cidrs) > 0 && alltrue([for c in var.ingress_cidrs : can(cidrhost(c, 0))])
    error_message = "ingress_cidrs must be a non-empty list of CIDR blocks."
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
  description = "RDS instance class. This database is the job queue as well as the ledger, so it is sized for writes rather than for storage. The burstable micro and small classes work for a short-lived environment; Performance Insights is switched off for them automatically because RDS does not offer it there."
  type        = string
  default     = "db.t4g.medium"
}

variable "database_allocated_storage" {
  description = "Gigabytes. Autoscaling is enabled, so this is a floor rather than a guess at the ceiling. gp3 starts at 20."
  type        = number
  default     = 20

  validation {
    condition     = var.database_allocated_storage >= 20
    error_message = "gp3 storage starts at 20 GiB."
  }
}

variable "api_desired_count" {
  description = "How many API tasks. Stateless, so this is a throughput dial. `make aws-up` applies once with 0 so the schema exists before the first task asks for it, then again with this value."
  type        = number
  default     = 2
}

variable "worker_desired_count" {
  description = "How many worker tasks. Safe to raise: claims are FOR UPDATE SKIP LOCKED and the reaper election is a transaction-scoped advisory lock. One is enough for the load measured in the README."
  type        = number
  default     = 1
}

variable "image_tag" {
  description = "Image tag for every task. They are built from one Dockerfile and deploy together, because a worker running older code than the API is the deploy-ordering failure this project has already had once. `make aws-up` sets it to the git commit."
  type        = string
  default     = "latest"
}

variable "oidc_issuer" {
  description = "The identity provider's issuer URL, or empty for none. An input, not infrastructure: an existing IdP is the normal case and standing up a new one is a decision this module should not make for anybody. Empty leaves the API serving ingest and health only; see main.tf."
  type        = string
  default     = ""
}

variable "oidc_jwks_url" {
  description = "Where the API fetches the issuer's signing keys. Empty derives Keycloak's path from the issuer."
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "The audience claim the API requires."
  type        = string
  default     = "threshold-api"
}

variable "otel_traces_endpoint" {
  description = "OTLP/HTTP base URL for spans, or empty to export none. The application speaks OTLP and does not care what receives it."
  type        = string
  default     = ""
}

variable "otel_metrics_endpoint" {
  description = "OTLP/HTTP base URL for metrics, or empty to export none."
  type        = string
  default     = ""
}

variable "api_domain_name" {
  description = "Hostname the API is served on, or empty to serve plain HTTP on the load balancer's own hostname. With a name, an ACM certificate is requested with DNS validation: the zone belongs to whoever owns the domain, the validation record is theirs to create, and the HTTPS listener attaches only once the certificate is issued."
  type        = string
  default     = ""
}
