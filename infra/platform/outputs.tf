output "vpc_id" {
  description = "Feederline development VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "2-AZ private subnets — feed SAM PrivateSubnetIds + RDS subnet group"
  value       = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}

output "lambda_security_group_id" {
  description = "SG the pilot activity Lambda is launched with (SAM LambdaSecurityGroupId)"
  value       = aws_security_group.lambda.id
}

output "app_security_group_id" {
  description = "SG for future ECS api/worker task definitions"
  value       = aws_security_group.app.id
}

output "db_instance_identifier" {
  value = aws_db_instance.postgres.identifier
}

output "db_endpoint" {
  description = "RDS endpoint host:port"
  value       = "${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}"
}

output "db_secret_arn" {
  description = "Secrets Manager ARN with DB_URI (app DB_SECRET_ARN / SAM DatabaseSecretArn)"
  value       = aws_secretsmanager_secret.db.arn
}

output "event_bus_name" {
  description = "Domain event bus (EVENT_BUS_NAME for app + worker)"
  value       = aws_cloudwatch_event_bus.main.name
}

output "event_bus_arn" {
  value = aws_cloudwatch_event_bus.main.arn
}

output "ecr_api_repository" {
  description = "Where the api + worker containers will be pushed"
  value       = aws_ecr_repository.api.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}
