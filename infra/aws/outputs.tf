# What a deploy would hand back, and nothing that would be a secret.

output "api_url" {
  description = "Where the API answers once DNS points at the load balancer."
  value       = "https://${var.api_domain_name}"
}

output "alb_dns_name" {
  description = "The record api_domain_name should be an alias for."
  value       = aws_lb.api.dns_name
}

output "documents_bucket" {
  description = "Where invoices are stored."
  value       = aws_s3_bucket.documents.bucket
}

output "database_endpoint" {
  description = "Reachable only from the task security group."
  value       = aws_db_instance.main.endpoint
}

# The ARNs, not the values. A secret in an output is a secret in the state file
# and in whatever CI prints the plan.
output "database_url_secret_arns" {
  description = "One per runtime role. Write the values out of band, after migrate has created the roles."
  value = {
    api    = aws_secretsmanager_secret.api_database_url.arn
    worker = aws_secretsmanager_secret.worker_database_url.arn
  }
}

output "migrate_task_definition" {
  description = "Run this to head before deploying the services."
  value       = aws_ecs_task_definition.migrate.arn
}
