# What a deploy hands back, and nothing that would be a secret.

output "api_url" {
  description = "Where the API answers. The load balancer's own hostname over HTTP unless a domain was given."
  value       = local.https ? "https://${var.api_domain_name}" : "http://${aws_lb.api.dns_name}"
}

output "alb_dns_name" {
  description = "The record api_domain_name should be an alias for, when there is one."
  value       = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  description = "Where `make aws-up` pushes the image."
  value       = aws_ecr_repository.app.repository_url
}

output "cluster_name" {
  description = "The ECS cluster the services and the one-shot tasks run in."
  value       = aws_ecs_cluster.main.name
}

output "admin_task_definition" {
  description = "The owner-credentialled task definition: migrate by default, seed and verify by command override."
  value       = aws_ecs_task_definition.migrate.arn
}

output "admin_container_name" {
  description = "The container name a command override must address."
  value       = "migrate"
}

output "private_subnet_ids" {
  description = "Where every task runs; a one-shot task needs them to be launched."
  value       = aws_subnet.private[*].id
}

output "tasks_security_group_id" {
  description = "The security group every task runs with."
  value       = aws_security_group.tasks.id
}

output "target_group_arn" {
  description = "The API's target group, for watching a deploy become healthy."
  value       = aws_lb_target_group.api.arn
}

output "log_group" {
  description = "Where every container's output goes. Streams are threshold/<container>/<task id>."
  value       = aws_cloudwatch_log_group.main.name
}

output "documents_bucket" {
  description = "Where invoices are stored."
  value       = aws_s3_bucket.documents.bucket
}

output "database_endpoint" {
  description = "Reachable only from the task security group."
  value       = aws_db_instance.main.endpoint
}

output "database_instance_class" {
  description = "Recorded with any measurement taken against this deploy."
  value       = aws_db_instance.main.instance_class
}

# What is actually running, for the measurement record: read from the state
# rather than recomputed by the script, so the conditions written next to a
# number are the conditions it was taken under.
output "image" {
  description = "The image every task runs."
  value       = local.image
}

output "service_sizes" {
  description = "Task counts and sizes as deployed."
  value = {
    api    = { count = var.api_desired_count, cpu = 512, memory_mib = 1024 }
    worker = { count = var.worker_desired_count, cpu = 1024, memory_mib = 2048 }
  }
}

output "ingress_cidrs" {
  description = "Who the load balancer admits."
  value       = var.ingress_cidrs
}

output "certificate_validation_records" {
  description = "With a domain: the CNAME the zone's owner must create before the second apply."
  value       = local.https ? aws_acm_certificate.api[0].domain_validation_options : []
}

# The ARNs, not the values. A secret in an output is a secret in whatever CI
# prints the plan.
output "database_password_secret_arns" {
  description = "One per database role."
  value       = { for k, s in aws_secretsmanager_secret.database_password : k => s.arn }
}
