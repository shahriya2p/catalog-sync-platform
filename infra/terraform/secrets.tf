# API keys.
#
# Only the containers are created here; the values are written out of band (CLI,
# or a rotation Lambda). Terraform never sees a key, so none is stored in state,
# in a plan file, or in a task definition. The application reads the secret at
# start-up via PRODUCT_API_KEY_SECRET_ID / WAREHOUSE_API_KEY_SECRET_ID.

resource "aws_secretsmanager_secret" "product_api_key" {
  name                    = "${local.name}/product-api-key"
  description             = "PIM API key used by the catalogue export stage"
  kms_key_id              = aws_kms_key.catalogue.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "warehouse_api_key" {
  name                    = "${local.name}/warehouse-api-key"
  description             = "WMS API key used by the delivery workers"
  kms_key_id              = aws_kms_key.catalogue.arn
  recovery_window_in_days = 7
}
