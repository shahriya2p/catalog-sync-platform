# Compute.
#
# One container image, two execution models:
#
# * the export stage runs as an ECS Fargate task, because a full export of a
#   million products takes several minutes of wall clock and must not be cut off
#   by Lambda's 15-minute ceiling;
# * the delivery stage runs as a Lambda fleet behind SQS, because it is many
#   short, independent units of work and reserved concurrency is the cleanest way
#   to cap the request rate against the WMS.
#
# Using the same image for both means one build, one vulnerability scan and no
# chance of the two paths running different code.

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.catalogue.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# --- exporter: ECS Fargate --------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "exporter" {
  family                   = "${local.name}-exporter"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.exporter_cpu
  memory                   = var.exporter_memory
  task_role_arn            = aws_iam_role.exporter_task.arn
  execution_role_arn       = aws_iam_role.ecs_execution.arn

  container_definitions = jsonencode([{
    name      = "exporter"
    image     = var.app_image_uri
    essential = true

    # The container entrypoint dispatches on AWS_LAMBDA_RUNTIME_API, so outside
    # Lambda this runs the CLI. RUN_ID is injected per execution by the state
    # machine's container override.
    command = ["export"]

    environment = [
      for key, value in local.app_environment : {
        name  = key
        value = value
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.exporter.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "exporter"
      }
    }
  }])
}

# --- delivery worker: Lambda behind SQS -------------------------------------

resource "aws_lambda_function" "delivery_worker" {
  function_name = "${local.name}-delivery-worker"
  role          = aws_iam_role.delivery_worker.arn
  package_type  = "Image"
  image_uri     = var.app_image_uri
  timeout       = 600
  memory_size   = 1024

  # The rate limiter inside each worker is per process, so the fleet's rate is
  # concurrency x per-worker rate. Reserved concurrency is therefore a safety
  # control, not a performance tuning knob: raising it without lowering
  # WMS_RPS would breach the documented 20 req/s.
  reserved_concurrent_executions = var.delivery_worker_concurrency

  image_config {
    command = ["app.aws_handlers.deliver_batches"]
  }

  environment {
    variables = merge(local.app_environment, {
      WMS_RPS = tostring(local.wms_rps_per_worker)
    })
  }

  depends_on = [aws_cloudwatch_log_group.delivery_worker]
}

resource "aws_lambda_event_source_mapping" "delivery" {
  event_source_arn = aws_sqs_queue.delivery.arn
  function_name    = aws_lambda_function.delivery_worker.arn
  batch_size       = 5
  enabled          = true

  # Report per-message failures so a single bad page does not force the other
  # messages in the receive batch to be processed again.
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = max(var.delivery_worker_concurrency, 2)
  }
}

# --- control-plane lambdas --------------------------------------------------

locals {
  control_functions = {
    start_run = {
      handler = "app.aws_handlers.start_run"
      timeout = 60
    }
    enqueue_batches = {
      handler = "app.aws_handlers.enqueue_batches"
      timeout = 300
    }
    check_progress = {
      handler = "app.aws_handlers.check_progress"
      timeout = 60
    }
    finalize_run = {
      handler = "app.aws_handlers.finalize_run"
      timeout = 300
    }
  }
}

resource "aws_lambda_function" "control" {
  for_each = local.control_functions

  function_name = "${local.name}-${replace(each.key, "_", "-")}"
  role          = aws_iam_role.control.arn
  package_type  = "Image"
  image_uri     = var.app_image_uri
  timeout       = each.value.timeout
  memory_size   = 512

  image_config {
    command = [each.value.handler]
  }

  environment {
    variables = local.app_environment
  }

  depends_on = [aws_cloudwatch_log_group.control]
}
