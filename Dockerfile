# One image, two execution models.
#
# The entrypoint dispatches on AWS_LAMBDA_RUNTIME_API: inside Lambda it starts
# the runtime interface client with the handler given as the image command,
# anywhere else it runs the CLI. That means the ECS exporter task, the Lambda
# workers and a local `docker run` all execute the same artefact, so there is no
# "works in one place" class of bug.

FROM python:3.11-slim AS base

# The process runs as a non-root user and /app is read-only to it, so the
# local-backend defaults point at /tmp. In AWS these are unused: the storage and
# state backends are S3 and DynamoDB.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRATCH_DIR=/tmp/catalogue-sync \
    SQLITE_PATH=/tmp/catalogue-sync/state/catalogue_sync.db \
    LOCAL_STORAGE_ROOT=/tmp/catalogue-sync/s3

WORKDIR /app

# Build tooling is needed only to install awslambdaric, so it is removed again
# in the same layer.
COPY requirements-app.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc-dev \
    && pip install --no-cache-dir -r requirements-app.txt awslambdaric==3.1.1 \
    && apt-get purge -y gcc libc-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Never run as root; the task only needs to write to /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/catalogue-sync \
    && chown -R appuser /tmp/catalogue-sync
USER appuser

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
