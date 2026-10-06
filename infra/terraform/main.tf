locals {
  name = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Component   = "pim-to-wms-catalogue-sync"
  }

  # Configuration shared by every compute component. Keys are never in here:
  # only the ARNs of the secrets that hold them.
  app_environment = {
    PRODUCT_API_URL             = var.product_api_url
    WAREHOUSE_API_URL           = var.warehouse_api_url
    PRODUCT_API_KEY_SECRET_ID   = aws_secretsmanager_secret.product_api_key.arn
    WAREHOUSE_API_KEY_SECRET_ID = aws_secretsmanager_secret.warehouse_api_key.arn
    STORAGE_BACKEND             = "s3"
    S3_BUCKET                   = aws_s3_bucket.catalogue.id
    S3_KMS_KEY_ID               = aws_kms_key.catalogue.arn
    STATE_BACKEND               = "dynamodb"
    RUNS_TABLE                  = aws_dynamodb_table.runs.name
    PAGES_TABLE                 = aws_dynamodb_table.pages.name
    BATCHES_TABLE               = aws_dynamodb_table.batches.name
    LEDGER_TABLE                = aws_dynamodb_table.ledger.name
    EXCEPTIONS_TABLE            = aws_dynamodb_table.exceptions.name
    BATCH_QUEUE_URL             = aws_sqs_queue.delivery.url
    PAGE_SIZE                   = tostring(var.page_size)
    BATCH_SIZE                  = tostring(var.batch_size)
    PIM_RPS                     = tostring(var.pim_requests_per_second)
    METRICS_NAMESPACE           = var.project
    EMIT_EMF_METRICS            = "true"
    RETENTION_DAYS              = tostring(var.export_retention_days)
    SCRATCH_DIR                 = "/tmp/catalogue-sync"
    LOG_LEVEL                   = "INFO"
  }

  # Each delivery worker gets an equal slice of the WMS rate budget, so the
  # fleet as a whole stays inside the documented 20 req/s even at full
  # concurrency. See ARCHITECTURE.md section 7 for why this is a per-worker
  # share rather than a shared bucket today.
  wms_rps_per_worker = var.wms_requests_per_second / var.delivery_worker_concurrency
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
