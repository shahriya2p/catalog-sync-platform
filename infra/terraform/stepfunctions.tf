# The run state machine.
#
# Step Functions owns the *run* (a few dozen transitions, a readable execution
# history, built-in timeouts) while SQS owns the *work* (up to 2,000 page
# messages). Putting the work into the state machine's history would make it
# unreadable and run into its size limits; putting the run into SQS would lose
# the operator view that answers "is this run running, completed or failed?".
#
# Retries here are stage-level and deliberately few: transient HTTP failures are
# already retried inside the application with jittered backoff, so a stage that
# still fails has a real problem and a blind retry would just burn the 30-minute
# budget.

resource "aws_sfn_state_machine" "sync" {
  name     = "${local.name}-sync"
  role_arn = aws_iam_role.state_machine.arn
  type     = "STANDARD"

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    include_execution_data = false # run ids only; no product data in logs
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  definition = jsonencode({
    Comment        = "Daily PIM to WMS catalogue synchronisation"
    StartAt        = "StartRun"
    TimeoutSeconds = var.run_timeout_minutes * 60
    States = {
      StartRun = {
        Type     = "Task"
        Resource = aws_lambda_function.control["start_run"].arn
        Parameters = {
          "run_id.$" = "$$.Execution.Name"
          trigger    = "schedule"
        }
        ResultPath = "$.run"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "NotifyFailure"
        }]
        Next = "ExportPages"
      }

      # The exporter runs to completion as an ECS task (.sync), so the state
      # machine waits without polling. A resume uses the same task with the same
      # run id: completed pages are skipped by the page checkpoints.
      ExportPages = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = aws_ecs_task_definition.exporter.arn_without_revision
          LaunchType     = "FARGATE"
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = var.vpc_subnet_ids
              SecurityGroups = var.security_group_ids
              AssignPublicIp = "DISABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [{
              Name = "exporter"
              Environment = [{
                Name      = "RUN_ID"
                "Value.$" = "$.run.run_id"
              }]
            }]
          }
        }
        ResultPath     = "$.export"
        TimeoutSeconds = var.run_timeout_minutes * 60
        Retry = [{
          # Capacity problems only. An export that failed on its own terms is
          # reported as PARTIAL by the application, not retried here.
          ErrorEquals     = ["ECS.AmazonECSException", "ECS.ServerException"]
          IntervalSeconds = 30
          MaxAttempts     = 2
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "FinalizeRun"
        }]
        Next = "EnqueuePages"
      }

      EnqueuePages = {
        Type     = "Task"
        Resource = aws_lambda_function.control["enqueue_batches"].arn
        Parameters = {
          "run_id.$" = "$.run.run_id"
        }
        ResultPath = "$.enqueue"
        Retry = [{
          ErrorEquals     = ["States.TaskFailed"]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "FinalizeRun"
        }]
        Next = "WaitForDelivery"
      }

      WaitForDelivery = {
        Type    = "Wait"
        Seconds = 30
        Next    = "CheckProgress"
      }

      CheckProgress = {
        Type     = "Task"
        Resource = aws_lambda_function.control["check_progress"].arn
        Parameters = {
          "run_id.$" = "$.run.run_id"
        }
        ResultPath = "$.progress"
        Next       = "DeliveryComplete"
      }

      DeliveryComplete = {
        Type = "Choice"
        Choices = [{
          Variable      = "$.progress.outstanding_batches"
          NumericEquals = 0
          Next          = "FinalizeRun"
        }]
        Default = "WaitForDelivery"
      }

      # Decides COMPLETED / PARTIAL / FAILED from the ledger. UNKNOWN products
      # are never resent here: reconciliation is an explicit operator action.
      FinalizeRun = {
        Type     = "Task"
        Resource = aws_lambda_function.control["finalize_run"].arn
        Parameters = {
          "run_id.$" = "$.run.run_id"
        }
        ResultPath = "$.final"
        Next       = "RunSucceeded"
      }

      RunSucceeded = {
        Type = "Choice"
        Choices = [{
          Variable     = "$.final.status"
          StringEquals = "COMPLETED"
          Next         = "Done"
        }]
        Default = "NotifyFailure"
      }

      NotifyFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.alarms.arn
          Subject     = "Catalogue sync did not complete"
          "Message.$" = "States.JsonToString($)"
        }
        Next = "RunFailed"
      }

      RunFailed = {
        Type  = "Fail"
        Error = "CatalogueSyncIncomplete"
        Cause = "The run finished as PARTIAL or FAILED. See the run record and CloudWatch logs."
      }

      Done = {
        Type = "Succeed"
      }
    }
  })
}
