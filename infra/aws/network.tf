# The network, and the one decision in it worth arguing about.
#
# There is no NAT gateway. The tasks run in private subnets with no route to the
# internet, and reach the three AWS services they need -- S3, ECR, Secrets
# Manager, plus CloudWatch Logs -- through VPC endpoints. A NAT gateway would be
# simpler to write and would cost roughly $32/month per AZ to provide a route
# nothing in this system needs: it calls no third-party API. The payment rail
# would be the first thing that did, and adding a NAT when there is a rail to
# reach is a smaller change than explaining an idle one now.
#
# The endpoints are also the stricter posture. There is no route to the
# internet, and every endpoint carries a policy that refuses any principal
# outside this account, so a compromised worker cannot hand what it read to a
# bucket, a log group or a secret that somebody else owns through the one path
# it does have. (An endpoint without a policy allows any principal; the route
# alone would not have been the control the first draft of this file said it
# was.)

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_iam_policy_document" "endpoint_this_account_only" {
  statement {
    effect    = "Allow"
    actions   = ["*"]
    resources = ["*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# The S3 gateway needs one more allowance. ECR hands a task its image layers as
# presigned S3 URLs signed by ECR's own identity, so the request that fetches a
# layer is not made by this account, and the policy above refused it: the first
# deploy failed with CannotPullContainerError at exactly that point. This is
# AWS's documented minimum for ECR behind a restricted gateway endpoint: read
# access to the regional bucket ECR serves layers from, and nothing else.
data "aws_iam_policy_document" "s3_endpoint" {
  source_policy_documents = [data.aws_iam_policy_document.endpoint_this_account_only.json]

  statement {
    sid       = "EcrImageLayers"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required by interface endpoints

  tags = { Name = local.name }
}

# /20 public, /20 private, carved deterministically so adding an AZ does not
# renumber the existing ones.
resource "aws_subnet" "public" {
  count = var.availability_zones

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false # the ALB gets its address from the ALB, not from this

  tags = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count = var.availability_zones

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = var.availability_zones

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# No default route: this is the property described at the top of the file.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = var.availability_zones

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------- endpoints --

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json

  tags = { Name = "${local.name}-s3" }
}

locals {
  # ECR needs two: the API for authorisation, DKR for the layers. Logs so a task
  # that cannot reach CloudWatch fails loudly rather than silently. Secrets
  # Manager so the task can read its own database credential and no other.
  interface_endpoints = toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
  ])
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  policy              = data.aws_iam_policy_document.endpoint_this_account_only.json

  tags = { Name = "${local.name}-${replace(each.value, ".", "-")}" }
}

# ----------------------------------------------------------- security groups --
#
# Written as separate rule resources rather than inline blocks: an inline block
# is replaced wholesale on change, which drops every rule for the duration of
# the apply.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ingress to the API"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-alb" }
}

# Who may reach the load balancer at all. POST /v1/documents takes no
# credential by design (suppliers are not users), so for a measurement run the
# answer is "the machine doing the measuring", and `make aws-up` passes its own
# address. Opening it to the world is a decision, not a default: see
# `ingress_cidrs` in variables.tf.
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = local.https ? toset(var.ingress_cidrs) : toset([])

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from ${each.value}"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# Port 80 is the API itself when no domain was given, and a redirect to 443
# when one was (compute.tf). Either way the load balancer has to answer it.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from ${each.value}"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Only to the API tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = local.api_port
  to_port                      = local.api_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "API and worker tasks"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "The API port, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = local.api_port
  to_port                      = local.api_port
  ip_protocol                  = "tcp"
}

# Egress to 443 and nowhere else, and no route to the internet exists for it to
# use anyway. Both, because a route added later should not silently become an
# exfiltration path.
#
# Two rules, because the two kinds of endpoint are reached differently. An
# interface endpoint is an address inside the VPC, so the VPC's own CIDR covers
# it. The S3 gateway is a route, not an address: traffic to it leaves for S3's
# public ranges, which the VPC CIDR does not contain. A tasks group with only
# the first rule cannot reach the bucket, and cannot pull an image either,
# because ECR keeps the layers in S3. The gateway publishes those ranges as a
# prefix list, and that is what the second rule allows.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "Interface endpoints"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_s3_gateway" {
  security_group_id = aws_security_group.tasks.id
  # No apostrophes: EC2 rejects them in rule descriptions.
  description    = "S3 through the gateway endpoint: the bucket, and the image layers ECR serves"
  prefix_list_id = aws_vpc_endpoint.s3.prefix_list_id
  from_port      = 443
  to_port        = 443
  ip_protocol    = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_database" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Postgres"
  referenced_security_group_id = aws_security_group.database.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "RDS Postgres"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-database" }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_tasks" {
  security_group_id            = aws_security_group.database.id
  description                  = "From the tasks, and nothing else"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "Interface endpoints"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tasks" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from the tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}
