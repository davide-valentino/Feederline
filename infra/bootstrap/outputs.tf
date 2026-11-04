output "tfstate_bucket" {
  description = "S3 bucket holding Terraform state and SAM pilot build artifacts"
  value       = aws_s3_bucket.tfstate.id
}

output "account_id" {
  value = local.account_id
}
