#!/bin/sh
set -eu

# Lambda sets AWS_LAMBDA_RUNTIME_API; there the first argument is the handler
# (for example app.aws_handlers.deliver_batches) and the runtime interface
# client takes over. Everywhere else the arguments are CLI arguments.
if [ -n "${AWS_LAMBDA_RUNTIME_API:-}" ]; then
  exec python -m awslambdaric "$@"
fi

# The ECS exporter task is started with the single argument "export" and gets
# its run id from the environment, because the state machine injects RUN_ID as a
# container override.
if [ "${1:-}" = "export" ]; then
  if [ -z "${RUN_ID:-}" ]; then
    echo "RUN_ID must be set for the export command" >&2
    exit 64
  fi
  shift
  exec python -m app.main export "${RUN_ID}" "$@"
fi

exec python -m app.main "$@"
