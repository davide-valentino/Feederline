terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Fed from infra/bootstrap outputs. Locking is the S3-native lockfile.
  backend "s3" {
    bucket       = "feederline-development-tfstate-716357364765"
    key          = "platform/terraform.tfstate"
    region       = "eu-west-1"
    profile      = "feederline-development"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  profile = "feederline-development"
  region  = "eu-west-1"

  default_tags {
    tags = {
      project     = "feederline"
      environment = "development"
      managed-by  = "terraform"
      cost-center = "p0-pilot"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  name       = "feederline-development"
  tags = {
    project     = "feederline"
    environment = "development"
    cost-center = "p0-pilot"
    managed-by  = "terraform"
  }
}
