# Where the rows, the bytes, and the image live.

# -------------------------------------------------------------------- image --
#
# The image is built from the repository's one Dockerfile and pushed here by
# `make aws-up`, tagged with the commit. Building is a different pipeline from
# describing where the image runs, but the registry is infrastructure, so it is
# created here rather than assumed.

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  # A non-production registry is deleted with its images so `destroy` is one
  # command. Production keeps them and makes somebody mean it.
  force_delete = var.environment != "production"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# ------------------------------------------------------------------ objects --
#
# D-006 stores the object before the row that describes it, which trades an
# orphaned object for never having a row that promises bytes nobody has.
# Versioning is on for the same reason the trade was made: the recoverable
# failure is the one to prefer.

resource "aws_s3_bucket" "documents" {
  # Bucket names are global, so the account id is part of this one; without it
  # a second person applying this module anywhere in the world collides with
  # the first.
  bucket = "${local.name}-documents-${data.aws_caller_identity.current.account_id}"

  # Same rule as the registry: a staging bucket empties itself on destroy, a
  # production one refuses.
  force_destroy = var.environment != "production"
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Every object is an invoice somebody sent, and an invoice is a record. Nothing
# expires; the lifecycle rule moves cold ones to cheaper storage instead.
resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "archive-old-documents"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }

    # Old *versions* do expire: a version exists because something was
    # overwritten, and D-006's storage keys are content-addressed, so an
    # overwrite means the same bytes were written twice.
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ------------------------------------------------------------------- Postgres --

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

# The application's own parameters. `pgaudit` is not enabled: this system's
# audit trail is a table with a trigger on it (D-004), and duplicating that at
# the engine level would produce a second log nobody reads.
resource "aws_db_parameter_group" "main" {
  name   = local.name
  family = "postgres16"

  # Every statement that takes longer than a second, because the queue's claim
  # is meant to be sub-millisecond and a slow one is the first sign of trouble.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  # The queue polls; a connection storm from a redeploy should not exhaust the
  # server before the tasks settle.
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }

  # A slow statement is logged by its text alone. The bind values of an INSERT
  # into extraction are the fields read off an invoice, and a log group is not
  # where those belong.
  parameter {
    name  = "log_parameter_max_length"
    value = "0"
  }
}

# RDS writes the exported Postgres log to this group and would create it
# itself, with no retention and outside Terraform, where `destroy` could not
# find it. Declared here, RDS adopts it, and it goes when the environment goes.
resource "aws_cloudwatch_log_group" "rds" {
  name              = "/aws/rds/instance/${local.name}/postgresql"
  retention_in_days = 30
}

locals {
  # RDS does not offer Performance Insights on the burstable micro and small
  # classes, and an apply that asks for it there fails outright.
  performance_insights = !contains(
    ["db.t3.micro", "db.t3.small", "db.t4g.micro", "db.t4g.small"],
    var.database_instance_class,
  )
}

resource "aws_db_instance" "main" {
  identifier = local.name

  engine         = "postgres"
  engine_version = "16"
  instance_class = var.database_instance_class

  # Migration 0001 creates the pgvector extension, which RDS ships for
  # Postgres 16. Nothing uses a vector column yet; the extension is there
  # because the schema's first revision asks for it.
  allocated_storage     = var.database_allocated_storage
  max_allocated_storage = var.database_allocated_storage * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "threshold"
  username = "threshold"
  # Generated by AWS and stored in Secrets Manager rather than passed in: a
  # password in a variable is a password in a state file and in somebody's
  # shell history. The migrate task reads it from there (compute.tf).
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false

  multi_az = var.environment == "production"

  backup_retention_period = 7
  copy_tags_to_snapshot   = true

  # This database holds the ledger. In production, deleting it without a final
  # snapshot is not a thing a `terraform destroy` should be able to do quietly.
  # In staging the snapshot would outlive the environment as a line item nobody
  # remembers, so staging skips it.
  deletion_protection       = var.environment == "production"
  skip_final_snapshot       = var.environment != "production"
  final_snapshot_identifier = var.environment == "production" ? "${local.name}-final" : null

  performance_insights_enabled    = local.performance_insights
  enabled_cloudwatch_logs_exports = ["postgresql"]

  auto_minor_version_upgrade = true
  apply_immediately          = var.environment != "production"

  depends_on = [aws_cloudwatch_log_group.rds]
}
