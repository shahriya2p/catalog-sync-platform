# Daily trigger.
#
# EventBridge Scheduler rather than a classic rule: it has a retry policy and a
# dead-letter target of its own, so a failure to *start* the run is visible
# instead of silently skipping a day.

resource "aws_scheduler_schedule" "daily" {
  name                         = "${local.name}-daily"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.sync.arn
    role_arn = aws_iam_role.scheduler.arn

    # The execution name becomes the run id, which makes the Step Functions
    # console searchable by business date and prevents two executions for the
    # same scheduled moment.
    input = jsonencode({ trigger = "schedule" })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 300
    }

    dead_letter_config {
      arn = aws_sqs_queue.delivery_dlq.arn
    }
  }
}
