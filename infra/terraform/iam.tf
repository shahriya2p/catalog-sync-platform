# IAM.
#
# One role per component, each with only the permissions that component needs:
#
# * the exporter reads the PIM and writes raw pages and the CSV. It may not read
#   or write the ledger, and it may not call the WMS.
# * the delivery worker reads raw pages and writes the ledger. It may not write
#   to S3 at all, so a bug in delivery cannot corrupt the 90-day export.
# * nothing is granted s3:DeleteObject or dynamodb:DeleteTable; retention is a
#   lifecycle rule, not an application capability.
#
# This split is what makes the blast radius of a mistake in one stage small.

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_states" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_scheduler" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

# --- shared policy fragments ------------------------------------------------

data "aws_iam_policy_document" "kms_use" {
  statement {
    sid = "UseCatalogueKey"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [aws_kms_key.catalogue.arn]
  }
}

data "aws_iam_policy_document" "write_metrics" {
  statement {
    sid       = "PublishMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData does not support resource-level permissions

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.project]
    }
  }
}

# --- exporter (ECS task) ----------------------------------------------------

resource "aws_iam_role" "exporter_task" {
  name               = "${local.name}-exporter-task"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

data "aws_iam_policy_document" "exporter" {
  source_policy_documents = [
    data.aws_iam_policy_document.kms_use.json,
    data.aws_iam_policy_document.write_metrics.json,
  ]

  statement {
    sid     = "WriteExportObjects"
    actions = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]

    resources = [
      aws_s3_bucket.catalogue.arn,
      "${aws_s3_bucket.catalogue.arn}/raw/*",
      "${aws_s3_bucket.catalogue.arn}/exports/*",
    ]
  }

  statement {
    sid = "CheckpointPages"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]

    resources = [
      aws_dynamodb_table.pages.arn,
      aws_dynamodb_table.runs.arn,
    ]
  }

  statement {
    sid       = "ReadPimKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.product_api_key.arn]
  }
}

resource "aws_iam_role_policy" "exporter" {
  name   = "${local.name}-exporter"
  role   = aws_iam_role.exporter_task.id
  policy = data.aws_iam_policy_document.exporter.json
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_kms" {
  name   = "${local.name}-ecs-execution-kms"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.kms_use.json
}

# --- delivery worker (Lambda) ----------------------------------------------

resource "aws_iam_role" "delivery_worker" {
  name               = "${local.name}-delivery-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "delivery_worker" {
  source_policy_documents = [
    data.aws_iam_policy_document.kms_use.json,
    data.aws_iam_policy_document.write_metrics.json,
  ]

  statement {
    sid       = "ReadRawPages"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.catalogue.arn}/raw/*"]
    # No PutObject: delivery must not be able to alter the retained export.
  }

  statement {
    sid = "WriteLedger"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
    ]

    resources = [
      aws_dynamodb_table.ledger.arn,
      aws_dynamodb_table.batches.arn,
      aws_dynamodb_table.exceptions.arn,
      aws_dynamodb_table.runs.arn,
    ]
  }

  statement {
    sid = "ConsumeQueue"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]

    resources = [aws_sqs_queue.delivery.arn]
  }

  statement {
    sid       = "ReadWmsKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.warehouse_api_key.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.delivery_worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "delivery_worker" {
  name   = "${local.name}-delivery-worker"
  role   = aws_iam_role.delivery_worker.id
  policy = data.aws_iam_policy_document.delivery_worker.json
}

# --- control-plane lambdas (start, enqueue, progress, finalize) -------------

resource "aws_iam_role" "control" {
  name               = "${local.name}-control"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "control" {
  source_policy_documents = [
    data.aws_iam_policy_document.kms_use.json,
    data.aws_iam_policy_document.write_metrics.json,
  ]

  statement {
    sid = "ManageRunState"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
    ]

    resources = [
      aws_dynamodb_table.runs.arn,
      aws_dynamodb_table.pages.arn,
      aws_dynamodb_table.batches.arn,
      aws_dynamodb_table.ledger.arn,
      aws_dynamodb_table.exceptions.arn,
    ]
  }

  statement {
    sid       = "QueuePages"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.delivery.arn]
  }

  statement {
    sid       = "ReadExportObjects"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.catalogue.arn, "${aws_s3_bucket.catalogue.arn}/*"]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.control.arn}:*"]
  }
}

resource "aws_iam_role_policy" "control" {
  name   = "${local.name}-control"
  role   = aws_iam_role.control.id
  policy = data.aws_iam_policy_document.control.json
}

# --- state machine ----------------------------------------------------------

resource "aws_iam_role" "state_machine" {
  name               = "${local.name}-state-machine"
  assume_role_policy = data.aws_iam_policy_document.assume_states.json
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    sid     = "InvokeStageFunctions"
    actions = ["lambda:InvokeFunction"]

    resources = [for function in aws_lambda_function.control : function.arn]
  }

  statement {
    sid     = "RunExporterTask"
    actions = ["ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks"]

    resources = [
      "${aws_ecs_task_definition.exporter.arn_without_revision}:*",
      aws_ecs_cluster.main.arn,
    ]
  }

  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]

    resources = [
      aws_iam_role.exporter_task.arn,
      aws_iam_role.ecs_execution.arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  # Required for the .sync integration pattern that waits for the ECS task.
  statement {
    sid       = "ManageSyncExecution"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
  }

  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]
  }

  statement {
    sid = "LogExecutions"

    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]

    resources = ["*"] # required by Step Functions logging configuration
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "${local.name}-state-machine"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

# --- scheduler --------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume_scheduler.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.sync.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
