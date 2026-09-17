# One secret per database role, and a task role that can read exactly one.
#
# Migration 0012 split the runtime identity in two: `threshold_api` records what
# people decide and writes no evidence; `threshold_worker` writes the record and
# cannot say a person approved anything. D-035 has the argument.
#
# That boundary is enforced by Postgres grants and row-level policies, which
# hold wherever this runs. Here it is enforced a second time, by IAM: the API
# task role can read the API's secret and is denied the worker's, and the
# reverse. A compromised API cannot read the credential that would let it write
# a match result, so it cannot reach for the privileges the database would have
# refused it anyway.
#
# This is the one place where deploying properly makes a control stronger rather
# than merely preserving it.

resource "aws_secretsmanager_secret" "api_database_url" {
  name        = "${local.name}/database-url/api"
  description = "threshold_api: records decisions, writes no evidence, updates nothing"
}

resource "aws_secretsmanager_secret" "worker_database_url" {
  name        = "${local.name}/database-url/worker"
  description = "threshold_worker: writes the record, cannot record a human decision"
}

# Deliberately no `aws_secretsmanager_secret_version` for either. Putting the
# value here would put a database password in the state file. They are written
# once, out of band, after `migrate` has created the roles -- which is also the
# only moment anybody knows the passwords.

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

# The execution role pulls the image and reads the secret *into* the task. It is
# shared, because it is the agent doing the injecting rather than the code that
# runs afterwards -- but it is scoped to these two secrets and no others.
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
    resources = [
      aws_secretsmanager_secret.api_database_url.arn,
      aws_secretsmanager_secret.worker_database_url.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-database-urls"
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
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.worker_database_url.arn]
  }
}

# The worker reads documents to extract them and never writes one.
data "aws_iam_policy_document" "worker_bucket" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
  }

  statement {
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.api_database_url.arn]
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
