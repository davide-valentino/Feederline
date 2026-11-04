# Bootstrap: creates the Terraform state backend.
# This stack itself uses local state (default) — it defines its own remote state.
#
# Applies once, by hand:
#   terraform -chdir=infra/bootstrap init
#   terraform -chdir=infra/bootstrap apply

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# Strict: the feederline-development profile for every AWS call (per guidance).
provider "aws" {
  profile = "feederline-development"
  region  = "eu-west-1"
}
