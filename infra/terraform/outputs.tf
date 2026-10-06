output "state_machine_arn" {
  description = "Run this to trigger a synchronisation manually."
  value       = aws_sfn_state_machine.sync.arn
}

output "catalogue_bucket" {
  description = "Bucket holding raw PIM pages and CSV exports (90 day retention)."
  value       = aws_s3_bucket.catalogue.id
}

output "delivery_queue_url" {
  value = aws_sqs_queue.delivery.url
}

output "delivery_dlq_url" {
  description = "Pages that failed delivery repeatedly land here."
  value       = aws_sqs_queue.delivery_dlq.url
}

output "ecr_repository_url" {
  description = "Push the application image here."
  value       = aws_ecr_repository.app.repository_url
}

output "dynamodb_tables" {
  value = {
    runs       = aws_dynamodb_table.runs.name
    pages      = aws_dynamodb_table.pages.name
    batches    = aws_dynamodb_table.batches.name
    ledger     = aws_dynamodb_table.ledger.name
    exceptions = aws_dynamodb_table.exceptions.name
  }
}

output "secret_arns" {
  description = "Write the API keys into these secrets after apply."
  value = {
    product_api_key   = aws_secretsmanager_secret.product_api_key.arn
    warehouse_api_key = aws_secretsmanager_secret.warehouse_api_key.arn
  }
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.sync.dashboard_name
}
