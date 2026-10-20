# The two long-running services, the one-shot administrative task, and the ALB.

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
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

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
        { name = "THRESHOLD_DB_USER", value = local.database_role_names.api },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.database_password["api"].arn
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

  depends_on = [
    aws_lb_listener.https,
    aws_lb_listener.http_forward,
    aws_lb_listener.http_redirect,
  ]
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
        { name = "THRESHOLD_DB_USER", value = local.database_role_names.worker },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.database_password["worker"].arn
        },
      ]

      logConfiguration = local.log_configuration
    }
  ])
}

# `worker_desired_count` defaults to 1, and that is now a capacity judgment
# rather than a limitation.
#
# The queue is safe to run wide: `claim` is `FOR UPDATE SKIP LOCKED`, so N
# workers take disjoint jobs, and the reaper election is a transaction-scoped
# advisory lock re-taken every cycle. It was session-level and cached in a
# Python flag until M10, which meant the flag outlived the lock and two workers
# could both believe they were the reaper -- invisible at one worker, which is
# why it survived to be found by a review rather than by an incident.
#
# One is the default because one is enough for the load measured in the README,
# not because more would break.
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
# property D-034 is about. The same definition runs the seed and the in-VPC
# verification (scripts/aws_verify.py) with a command override, because both
# need the owner and neither needs the bucket: it has no task role at all.
#
# It also receives the three role passwords, because migration 0012 creates
# the roles and reads their passwords from exactly these variables; that is
# what lets the API and the worker connect with the secrets above the moment
# the schema exists.

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
        { name = "THRESHOLD_DB_USER", value = aws_db_instance.main.username },
      ])

      secrets = [
        {
          name      = "THRESHOLD_DB_PASSWORD"
          valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
        },
        {
          name      = "THRESHOLD_API_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.database_password["api"].arn
        },
        {
          name      = "THRESHOLD_WORKER_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.database_password["worker"].arn
        },
        {
          name      = "THRESHOLD_APP_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.database_password["app"].arn
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

# With a domain: HTTPS on 443 with an ACM certificate, and 80 redirects to it.
# Without one: plain HTTP on 80 at the load balancer's own hostname.
#
# A domain takes two applies, and the module does not pretend otherwise. The
# certificate is requested with DNS validation, and a listener cannot attach a
# certificate that is still pending, so the first apply with `api_domain_name`
# fails at the HTTPS listener. The `certificate_validation_records` output
# says which CNAME the zone's owner has to create; once ACM has issued, the
# second apply succeeds. Automating that needs a Route 53 zone this module
# does not assume anybody has.

resource "aws_acm_certificate" "api" {
  count = local.https ? 1 : 0

  domain_name       = var.api_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "https" {
  count = local.https ? 1 : 0

  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.api[0].arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  count = local.https ? 1 : 0

  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "http_forward" {
  count = local.https ? 0 : 1

  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
