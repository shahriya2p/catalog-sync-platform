# Durable storage for the raw PIM pages and the generated CSV export.
#
# Retention is enforced by the bucket, not the application: a lifecycle rule
# cannot be forgotten by a code change, and the application roles are not given
# s3:DeleteObject at all, so a bug cannot shorten the 90-day audit window.

resource "aws_kms_key" "catalogue" {
  description             = "${local.name} catalogue export encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "catalogue" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.catalogue.key_id
}

resource "aws_s3_bucket" "catalogue" {
  bucket = "${local.name}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "catalogue" {
  bucket                  = aws_s3_bucket.catalogue.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "catalogue" {
  bucket = aws_s3_bucket.catalogue.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "catalogue" {
  bucket = aws_s3_bucket.catalogue.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "catalogue" {
  bucket = aws_s3_bucket.catalogue.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.catalogue.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "catalogue" {
  bucket = aws_s3_bucket.catalogue.id

  # The business requirement is to retain each run's original export for 90
  # days. Raw pages and the CSV expire together so a run is never half-present.
  rule {
    id     = "expire-raw-pages"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    expiration {
      days = var.export_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "expire-exports"
    status = "Enabled"

    filter {
      prefix = "exports/"
    }

    expiration {
      days = var.export_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "catalogue_bucket" {
  statement {
    sid    = "DenyUnencryptedTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.catalogue.arn,
      "${aws_s3_bucket.catalogue.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "catalogue" {
  bucket = aws_s3_bucket.catalogue.id
  policy = data.aws_iam_policy_document.catalogue_bucket.json
}
