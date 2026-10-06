# Delivery work queue.
#
# Standard, not FIFO: ordering does not matter (the ledger, not the queue,
# decides what may be sent), and FIFO's 300 message/s ceiling would throttle the
# fan-out of 2,000 page messages. Standard queues can deliver a message more
# than once, which is precisely why the ledger exists.

resource "aws_sqs_queue" "delivery_dlq" {
  name                      = "${local.name}-delivery-dlq"
  message_retention_seconds = 1209600 # 14 days: long enough to investigate
  kms_master_key_id         = aws_kms_key.catalogue.arn
}

resource "aws_sqs_queue" "delivery" {
  name = "${local.name}-delivery"

  # Longer than the worst-case time to deliver one page (5 WMS batches, each
  # with retries), so a slow page is not handed to a second worker while the
  # first is still working on it.
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  kms_master_key_id          = aws_kms_key.catalogue.arn

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delivery_dlq.arn
    # Five attempts: transient WMS faults are already retried inside the
    # worker, so a message that fails five times needs a human.
    maxReceiveCount = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "delivery_dlq" {
  queue_url = aws_sqs_queue.delivery_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.delivery.arn]
  })
}
