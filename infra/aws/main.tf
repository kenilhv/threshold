# The AWS shape of this system. Validated in CI, never applied -- see README.md.
#
# Every resource here has a counterpart in docker-compose.yml, and the point of
# the module is that the counterpart is a configuration difference rather than a
# code difference. Where that is not true, the comment says so instead of
# pretending.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Deliberately not configured here. A backend belongs to the environment that
  # owns the state, not to the module, and `terraform init -backend=false` is
  # what CI runs precisely because there is no state to talk to.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

locals {
  name = "${var.project}-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Repository  = "threshold"
  }

  # One port, stated once. The container listens on it, the target group health
  # checks it, and the security group opens it; three literals would drift.
  api_port = 8000

  # What the API and the worker both need, whatever else differs between them.
  # Secrets are injected separately (secrets.tf) because they are not values.
  common_environment = [
    { name = "THRESHOLD_ENVIRONMENT", value = var.environment },
    { name = "THRESHOLD_LOG_LEVEL", value = var.log_level },
    { name = "THRESHOLD_S3_BUCKET_DOCUMENTS", value = aws_s3_bucket.documents.bucket },
    { name = "THRESHOLD_OIDC_ISSUER", value = var.oidc_issuer },
    { name = "THRESHOLD_OIDC_AUDIENCE", value = var.oidc_audience },
    { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = var.otlp_endpoint },
  ]
}
