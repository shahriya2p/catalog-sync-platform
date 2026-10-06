variable "aws_region" {
  description = "Region to deploy into."
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Environment name, used in resource names and tags."
  type        = string
  default     = "prod"
}

variable "project" {
  description = "Project prefix for all resource names."
  type        = string
  default     = "catalogue-sync"
}

variable "app_image_uri" {
  description = <<-EOT
    Container image for every compute component (ECR URI including tag or
    digest). One image serves both the ECS exporter task and the delivery
    Lambda; the entrypoint selects the behaviour, so there is a single build to
    test and promote.
  EOT
  type        = string
}

variable "vpc_subnet_ids" {
  description = <<-EOT
    Private subnets with outbound internet access (NAT or a proxy) for the ECS
    exporter task. This module deliberately does not create a VPC: networking is
    owned by the platform team in most organisations, and inventing one here
    would fight that ownership.
  EOT
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups for the ECS task (egress to the PIM and WMS)."
  type        = list(string)
}

variable "product_api_url" {
  description = "Base URL of the PIM API."
  type        = string
}

variable "warehouse_api_url" {
  description = "Base URL of the WMS API."
  type        = string
}

variable "schedule_expression" {
  description = "When the daily synchronisation starts (UTC)."
  type        = string
  default     = "cron(0 2 * * ? *)"
}

variable "page_size" {
  description = "PIM page size. The PIM rejects anything above 500."
  type        = number
  default     = 500

  validation {
    condition     = var.page_size >= 1 && var.page_size <= 500
    error_message = "page_size must be between 1 and 500 (documented PIM limit)."
  }
}

variable "batch_size" {
  description = "WMS batch size. The WMS rejects anything above 100 with HTTP 413."
  type        = number
  default     = 100

  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 100
    error_message = "batch_size must be between 1 and 100 (documented WMS limit)."
  }
}

variable "pim_requests_per_second" {
  description = "PIM rate budget. The documented ceiling is 10 req/s."
  type        = number
  default     = 10

  validation {
    condition     = var.pim_requests_per_second > 0 && var.pim_requests_per_second <= 10
    error_message = "pim_requests_per_second must be in (0, 10]."
  }
}

variable "wms_requests_per_second" {
  description = "WMS rate budget. The documented ceiling is 20 req/s."
  type        = number
  default     = 20

  validation {
    condition     = var.wms_requests_per_second > 0 && var.wms_requests_per_second <= 20
    error_message = "wms_requests_per_second must be in (0, 20]."
  }
}

variable "delivery_worker_concurrency" {
  description = <<-EOT
    Reserved concurrency for the delivery Lambda. Each worker holds its own
    share of the WMS rate budget, so this is the second half of the rate limit:
    concurrency x per-worker rate must stay at or below the WMS ceiling.
  EOT
  type        = number
  default     = 10
}

variable "export_retention_days" {
  description = "How long raw pages and CSV exports are kept. The business requires 90 days."
  type        = number
  default     = 90
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

variable "run_timeout_minutes" {
  description = "Hard stop for a whole run. The business SLA is 30 minutes."
  type        = number
  default     = 60
}

variable "alarm_email" {
  description = "Optional email subscribed to the alarm topic. Empty means no subscription."
  type        = string
  default     = ""
}

variable "exporter_cpu" {
  description = "Fargate CPU units for the exporter task."
  type        = number
  default     = 1024
}

variable "exporter_memory" {
  description = "Fargate memory (MiB) for the exporter task."
  type        = number
  default     = 2048
}
