# The two long-running services, the one-shot migration task, and the ALB.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

locals {
  image = "${var.ecr_repository_url}:${var.image_tag}"

  log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.main.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "threshold"
    }
  }

  # /healthz, not /readyz. Liveness asks whether the process is alive; a
  # container health check that consults the database restarts every healthy
  # task during a failover, turning a recoverable blip into an outage. The
  # target group asks the other question.
  api_health_command = join("", [
    "python -c \"import urllib.request,sys; ",
    "sys.exit(0 if urllib.request.urlopen('http://localhost:",
    tostring(local.api_port),
    "/healthz', timeout=3).status == 200 else 1)\"",
  ])
}

# ---------------------------------------------------------------------- API --

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image
      essential = true
      command = [
        "uvicorn", "threshold.api.main:app",
        "--host", "0.0.0.0",
        "--port", tostring(local.api_port),
      ]

      portMappings = [{ containerPort = local.api_port, protocol = "tcp" }]

      environment = concat(local.common_environment, [
        { name = "THRESHOLD_SERVICE_NAME", value = "threshold-api" },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DATABASE_URL"
          valueFrom = aws_secretsmanager_secret.api_database_url.arn
        },
      ]

      healthCheck = {
        command     = ["CMD-SHELL", local.api_health_command]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.api_port
  }

  # Long enough for readiness to pass on a cold start: /readyz checks Postgres
  # and the bucket rather than answering immediately.
  health_check_grace_period_seconds = 60

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]
}

# ------------------------------------------------------------------- worker --

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024 # it parses PDFs; the API does not
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true
      command   = ["python", "-m", "threshold.workers.main"]

      environment = concat(local.common_environment, [
        { name = "THRESHOLD_SERVICE_NAME", value = "threshold-worker" },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DATABASE_URL"
          valueFrom = aws_secretsmanager_secret.worker_database_url.arn
        },
      ]

      logConfiguration = local.log_configuration
    }
  ])
}

# `worker_desired_count` defaults to 1, and that default is a known limitation
# rather than a capacity judgment.
#
# The queue itself is safe to run wide: `claim` is `FOR UPDATE SKIP LOCKED`, so
# N workers take disjoint jobs. What is not yet safe is the reaper election. It
# uses a session-level `pg_try_advisory_lock` taken on a pooled connection and
# caches the outcome in a Python flag, so the lock outlives the session that
# took it and a worker can believe it holds a lock it has handed back. The
# threat model's weakness table carries it. Raising this number is a one-line
# change once that is a transaction-level lock instead.
resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

# ------------------------------------------------------------ one-shot task --
#
# A task definition rather than a service, because running migrations
# continuously is exactly what must not happen: two API replicas racing to
# migrate the same database is a real outage, which is why `migrate` is a
# separate compose service locally too.
#
# It holds the schema owner's credential -- the RDS-managed master secret --
# and it exits. Nothing long-running holds that credential, which is the
# property D-034 is about.

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = local.image
      essential = true
      command   = ["alembic", "upgrade", "head"]

      environment = concat(local.common_environment, [
        { name = "THRESHOLD_SERVICE_NAME", value = "threshold-migrate" },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DATABASE_URL"
          valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:url::"
        },
      ]

      logConfiguration = local.log_configuration
    }
  ])
}

# --------------------------------------------------------------------- ALB --

resource "aws_lb" "api" {
  name               = "${local.name}-api"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "production"
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = local.api_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # /readyz here and /healthz on the container: this is the question "should
  # traffic be routed to this instance", which is the one readiness answers.
  health_check {
    path                = "/readyz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }

  # Ingest accepts a PDF and returns; the pipeline is asynchronous, so no
  # request is long-lived and 30s is generous for the slowest of them.
  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.api.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}
