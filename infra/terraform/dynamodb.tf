# Run state.
#
# On-demand billing: the workload is one burst per day, so provisioned capacity
# would be paid for 23 hours of idleness or would throttle the burst.
#
# Point-in-time recovery is on for the ledger and the run table because losing
# them means losing the record of what was already sent to the warehouse, which
# is the one piece of state that cannot be rebuilt from S3.

resource "aws_dynamodb_table" "runs" {
  name         = "${local.name}-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.catalogue.arn
  }
}

resource "aws_dynamodb_table" "pages" {
  name         = "${local.name}-pages"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "page"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "page"
    type = "N"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.catalogue.arn
  }

  # Page checkpoints are only useful while a run can still be resumed.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "batches" {
  name         = "${local.name}-batches"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "batch_no"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "batch_no"
    type = "N"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.catalogue.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# The per-SKU delivery ledger: one item per product per run, so one million
# items and one million writes in a large run.
#
# The key is "<run_id>#<sku>" rather than (run_id, sku) on purpose. A composite
# key would put every write of a run into a single partition, which is capped at
# 1,000 WCU/s and would throttle the delivery stage; a single hash key spreads
# them across partitions. Access is point read/write only (BatchGetItem of 100
# keys matches one WMS batch exactly), so nothing is lost by giving up the
# range key, and no global secondary index is needed over a million items.
resource "aws_dynamodb_table" "ledger" {
  name         = "${local.name}-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.catalogue.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# Rejected, unknown and locally-invalid products only. Small by design, so
# operations can list everything that went wrong in a run with one query
# instead of scanning the ledger.
resource "aws_dynamodb_table" "exceptions" {
  name         = "${local.name}-exceptions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "sku"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "sku"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.catalogue.arn
  }
}
