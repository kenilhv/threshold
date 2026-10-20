# The AWS shape of this system. Validated in CI on every push; applied on demand
# with `make aws-up` and torn down with `make aws-down` -- see README.md here for
# what a deploy proves, what it costs, and what it deliberately leaves out.
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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Deliberately no backend block. `make aws-up` keeps local state next to these
  # files, gitignored, which is right for an environment that exists for an
  # afternoon and is destroyed with its state. A long-lived environment would put
  # the state in S3 with locking, and that belongs to whoever owns the
  # environment, not to the module. CI runs `init -backend=false` for the same
  # reason: there is no state to talk to.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

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

  # A domain name turns the ALB listener into HTTPS with an ACM certificate.
  # Without one the API is served over plain HTTP on the load balancer's own
  # hostname, which is the honest shape of a deploy nobody owns a domain for.
  https = var.api_domain_name != ""

  # What every process needs, whatever else differs between them.
  #
  # The database arrives as parts rather than a URL: RDS and Secrets Manager
  # provide a host and a password, not a DSN, and a URL pasted together in a
  # shell loses a password containing '#' or '@'. Settings composes it
  # (config/settings.py). The user and the password differ per task and are set
  # on each task definition; the host, port and name are shared.
  #
  # No static S3 key pair is set anywhere. The task role is the credential, and
  # Settings treats the pair as optional for exactly this case.
  common_environment = concat(
    [
      { name = "THRESHOLD_ENVIRONMENT", value = var.environment },
      { name = "THRESHOLD_LOG_LEVEL", value = var.log_level },
      { name = "THRESHOLD_DB_HOST", value = aws_db_instance.main.address },
      { name = "THRESHOLD_DB_PORT", value = tostring(aws_db_instance.main.port) },
      { name = "THRESHOLD_DB_NAME", value = aws_db_instance.main.db_name },
      { name = "THRESHOLD_S3_REGION", value = var.region },
      { name = "THRESHOLD_S3_BUCKET_DOCUMENTS", value = aws_s3_bucket.documents.bucket },
      { name = "THRESHOLD_OIDC_AUDIENCE", value = var.oidc_audience },
    ],
    local.identity_environment,
    local.telemetry_environment,
  )

  # No issuer means the API answers every authenticated route with 503 rather
  # than waving anyone through, and serves ingest and health only (api/auth.py).
  # That is the honest shape of a deploy with no identity provider in front of
  # it: the pipeline is real, the console is not reachable. Setting an issuer
  # also needs a route to its JWKS, which this VPC does not have by design
  # (network.tf), so it comes with a NAT gateway or a peering of its own.
  identity_environment = var.oidc_issuer == "" ? [] : [
    { name = "THRESHOLD_OIDC_ISSUER", value = var.oidc_issuer },
    {
      name  = "THRESHOLD_OIDC_JWKS_URL"
      value = coalesce(var.oidc_jwks_url, "${var.oidc_issuer}/protocol/openid-connect/certs")
    },
  ]

  # Two endpoints, not one, because the application sends spans and metrics to
  # two different receivers (config/settings.py). Unset means the SDK still
  # produces real spans and trace ids for the audit rows and simply exports
  # nothing, which is the supported shape when no collector is running.
  telemetry_environment = concat(
    var.otel_traces_endpoint == "" ? [] : [
      { name = "THRESHOLD_OTEL_TRACES_ENDPOINT", value = var.otel_traces_endpoint },
    ],
    var.otel_metrics_endpoint == "" ? [] : [
      { name = "THRESHOLD_OTEL_METRICS_ENDPOINT", value = var.otel_metrics_endpoint },
    ],
  )
}
