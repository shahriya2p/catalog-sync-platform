terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # State is intentionally not configured here: the backend differs per
  # environment and is supplied with `terraform init -backend-config=...`.
  # Committing a backend block would tie this module to one account.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
