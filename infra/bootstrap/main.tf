data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  tags = {
    project     = "feederline"
    environment = "development"
    managed-by  = "terraform"
  }
}

# ---------------------------------------------------------------------------
# Terraform state bucket. Also hosts SAM build artifacts (key prefix: pilot/)
# so the pilot child deploy has one place to upload its Lambda zip.
#
# Locking uses the S3-native lockfile (Terraform >= 1.10, use_lockfile),
# so no DynamoDB table is required.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "tfstate" {
  bucket = "feederline-development-tfstate-${local.account_id}"
  tags   = merge(local.tags, { purpose = "terraform-state" })
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "pilot_artifacts" {
  # SAM upload artifacts are transient builds; prune them.
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-pilot-artifacts"
    status = "Enabled"
    filter {
      prefix = "pilot/"
    }
    expiration {
      days = 30
    }
  }
}
