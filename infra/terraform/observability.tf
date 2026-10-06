# Logs, metrics, alarms.
#
# The alarms are chosen to answer the questions operations will actually ask at
# 08:00: did the run finish, did it finish in time, and is there anything whose
# outcome we do not know? "ProductsUnknown > 0" is the important one - it is the
# only state that can hide a duplicate or a missing product in the warehouse,
# and it needs a human decision rather than an automatic retry.

resource "aws_cloudwatch_log_group" "exporter" {
  name              = "/aws/ecs/${local.name}-exporter"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.catalogue.arn
}

resource "aws_cloudwatch_log_group" "delivery_worker" {
  name              = "/aws/lambda/${local.name}-delivery-worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.catalogue.arn
}

resource "aws_cloudwatch_log_group" "control" {
  name              = "/aws/lambda/${local.name}-control"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.catalogue.arn
}

resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/${local.name}-sync"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = aws_kms_key.catalogue.arn
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# --- run level --------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "run_failed" {
  alarm_name          = "${local.name}-run-failed"
  alarm_description   = "The daily catalogue synchronisation failed or timed out."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "failures"
    expression  = "failed + timedout + aborted"
    label       = "Executions that did not succeed"
    return_data = true
  }

  metric_query {
    id = "failed"

    metric {
      namespace   = "AWS/States"
      metric_name = "ExecutionsFailed"
      period      = 3600
      stat        = "Sum"
      dimensions  = { StateMachineArn = aws_sfn_state_machine.sync.arn }
    }
  }

  metric_query {
    id = "timedout"

    metric {
      namespace   = "AWS/States"
      metric_name = "ExecutionsTimedOut"
      period      = 3600
      stat        = "Sum"
      dimensions  = { StateMachineArn = aws_sfn_state_machine.sync.arn }
    }
  }

  metric_query {
    id = "aborted"

    metric {
      namespace   = "AWS/States"
      metric_name = "ExecutionsAborted"
      period      = 3600
      stat        = "Sum"
      dimensions  = { StateMachineArn = aws_sfn_state_machine.sync.arn }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "run_too_slow" {
  alarm_name          = "${local.name}-run-exceeded-sla"
  alarm_description   = "A run took longer than the 30 minute business SLA."
  namespace           = "AWS/States"
  metric_name         = "ExecutionTime"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.sync.arn }
  statistic           = "Maximum"
  period              = 3600
  comparison_operator = "GreaterThanThreshold"
  threshold           = 30 * 60 * 1000 # milliseconds
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# --- application level ------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "products_unknown" {
  alarm_name          = "${local.name}-products-unknown"
  alarm_description   = <<-EOT
    At least one product has an ambiguous delivery outcome: the WMS may or may
    not hold it. These are never resent automatically, so an operator must
    decide (see the reconcile command).
  EOT
  namespace           = var.project
  metric_name         = "ProductsUnknown"
  statistic           = "Sum"
  period              = 3600
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "pages_failed" {
  alarm_name          = "${local.name}-pages-failed"
  alarm_description   = "PIM pages could not be fetched, so the export is incomplete."
  namespace           = var.project
  metric_name         = "PagesFailed"
  statistic           = "Sum"
  period              = 3600
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "throttling_pressure" {
  alarm_name          = "${local.name}-throttling-pressure"
  alarm_description   = <<-EOT
    Sustained 429s from the external APIs. Not an outage, but the run is
    spending its 30-minute budget on backoff and will eventually breach the SLA
    as the catalogue grows.
  EOT
  comparison_operator = "GreaterThanThreshold"
  threshold           = 200
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "throttles"
    expression  = "pim + wms"
    label       = "429 responses from the PIM and WMS"
    return_data = true
  }

  metric_query {
    id = "pim"

    metric {
      namespace   = var.project
      metric_name = "Pim429"
      period      = 3600
      stat        = "Sum"
    }
  }

  metric_query {
    id = "wms"

    metric {
      namespace   = var.project
      metric_name = "Wms429"
      period      = 3600
      stat        = "Sum"
    }
  }
}

# --- infrastructure level ---------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-delivery-dlq"
  alarm_description   = "Pages failed delivery five times and are now on the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.delivery_dlq.name }
  statistic           = "Maximum"
  period              = 300
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${local.name}-delivery-worker-errors"
  alarm_description   = "The delivery worker is raising unhandled errors."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.delivery_worker.function_name }
  statistic           = "Sum"
  period              = 300
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_dashboard" "sync" {
  dashboard_name = local.name

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Products by outcome"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 300
          metrics = [
            [var.project, "ProductsAccepted"],
            [var.project, "ProductsRejected"],
            [var.project, "ProductsUnknown"],
            [var.project, "ProductsFailedValidation"],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "External API pressure"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 300
          metrics = [
            [var.project, "PimRequests"],
            [var.project, "PimRetries"],
            [var.project, "Pim429"],
            [var.project, "WmsRequests"],
            [var.project, "WmsRetries"],
            [var.project, "Wms429"],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Run duration against the 30 minute SLA"
          region = data.aws_region.current.name
          stat   = "Maximum"
          period = 3600
          metrics = [
            ["AWS/States", "ExecutionTime", "StateMachineArn", aws_sfn_state_machine.sync.arn],
          ]
          annotations = {
            horizontal = [{
              label = "30 minute SLA"
              value = 30 * 60 * 1000
            }]
          }
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Delivery queue"
          region = data.aws_region.current.name
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.delivery.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.delivery_dlq.name],
          ]
        }
      },
    ]
  })
}
