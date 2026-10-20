# One secret per database role, and a task role that can read exactly one.
#
# Migration 0012 split the runtime identity in two: `threshold_api` records what
# people decide and writes no evidence; `threshold_worker` writes the record and
# cannot say a person approved anything. D-035 has the argument.
#
# That boundary is enforced by Postgres grants and row-level policies, which
# hold wherever this runs. Here it is enforced a second time, by IAM. Neither
# task role may read any secret at all: the execution role injects exactly one
# into each task before the code runs, and each task role carries an explicit
# Deny on the others, so a later Allow added in haste cannot widen it. A
# compromised API cannot fetch the credential that would let it write a match
# result, so it cannot reach for the privileges the database would have refused
# it anyway.
#
# This is the one place where deploying properly makes a control stronger rather
# than merely preserving it.

locals {
  # The two runtime roles, plus the legacy single role from 0010 that 0012
  # stripped of every grant but did not drop. It gets a real password too, so
  # that no role in the cluster is left on the local default.
  database_roles = toset(["api", "worker", "app"])

  database_role_names = {
    api    = "threshold_api"
    worker = "threshold_worker"
    app    = "threshold_app"
  }
}

# The passwords are generated here and written into Secrets Manager, which puts
# them in the state file. For an environment that lives for an afternoon with
# local, gitignored state that is destroyed with it, that is the right trade:
# it is what makes `make aws-up` a single command, because the migrate task
# (compute.tf) receives the same values and creates the roles with them, so the
# roles exist with their passwords before the first API task asks to connect.
#
# A long-lived environment would not do this. It would create the secrets empty,
# write the values out of band after the first migrate, and rotate them without
# Terraform knowing -- the shape the first draft of this file described and
# never ran.
resource "random_password" "database" {
  for_each = local.database_roles

  length = 32
  # Letters and digits only. Settings quotes the password into the URL and the
  # migration passes it through Postgres's own format(%L), so punctuation would
  # be safe; it is simply not worth a third place to be wrong about.
  special = false
}

resource "aws_secretsmanager_secret" "database_password" {
  for_each = local.database_roles

  name        = "${local.name}/database-password/${each.key}"
  description = "Postgres password for ${local.database_role_names[each.key]}"

  # Staging deletes immediately so a destroy followed by an apply the same day
  # does not meet a secret that is still scheduled for deletion under the name
  # it wants. Production keeps the recovery window.
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "database_password" {
  for_each = local.database_roles

  secret_id     = aws_secretsmanager_secret.database_password[each.key].id
  secret_string = random_password.database[each.key].result
}

# ------------------------------------------------------------------- roles --

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The execution role pulls the image and reads the secrets *into* the task. It
# is shared, because it is the agent doing the injecting rather than the code
# that runs afterwards -- but it is scoped to exactly these secrets: the three
# role passwords, and the RDS-managed master password the migrate task needs.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [for s in aws_secretsmanager_secret.database_password : s.arn],
      [aws_db_instance.main.master_user_secret[0].secret_arn],
    )
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-database-passwords"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# The task roles are what the *running code* holds, and they are separate.
resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

# Ingest stores the object before writing the row (D-006), so the API writes to
# the bucket. It never deletes: nothing in this system deletes a document, and
# the orphan sweep D-006 still owes would be its own task with its own role.
data "aws_iam_policy_document" "api_bucket" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
  }

  statement {
    effect  = "Deny"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_password["worker"].arn,
      aws_secretsmanager_secret.database_password["app"].arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ]
  }
}

# The worker reads documents to extract them and never writes one.
data "aws_iam_policy_document" "worker_bucket" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
  }

  statement {
    effect  = "Deny"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_password["api"].arn,
      aws_secretsmanager_secret.database_password["app"].arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "documents"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_bucket.json
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "documents"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_bucket.json
}
