output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  value = aws_ecs_service.app.name
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}

output "public_subnet_ids" {
  value = data.aws_subnets.default_public.ids
}
