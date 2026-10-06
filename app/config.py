"""Typed configuration for the catalogue synchronisation.

Everything that differs between the local run and AWS is a setting, so the same
code path runs in both places. Values that the external APIs document as hard
limits (PIM page size, WMS batch size) are validated here rather than trusted,
because exceeding them is a guaranteed 400/413 at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Optional

PIM_MAX_PAGE_SIZE = 500
"""Documented PIM limit. A larger page size is rejected with HTTP 400."""

WMS_MAX_BATCH_SIZE = 100
"""Documented WMS limit. A larger batch is rejected with HTTP 413."""

PIM_MAX_REQUESTS_PER_SECOND = 10.0
WMS_MAX_REQUESTS_PER_SECOND = 20.0


class ConfigError(ValueError):
    """Raised when configuration would violate a documented API limit."""


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class RetrySettings:
    """Bounded exponential backoff with full jitter.

    ``max_attempts`` counts the first attempt, so ``6`` means one call plus up
    to five retries. The worst-case wait is bounded by
    ``max_delay_seconds * (max_attempts - 1)``, which keeps a single page or
    batch from consuming the whole run budget.
    """

    max_attempts: int = 6
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("retry max_attempts must be >= 1")
        if self.base_delay_seconds <= 0:
            raise ConfigError("retry base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ConfigError("retry max_delay_seconds must be >= base_delay_seconds")


@dataclass(frozen=True)
class Settings:
    """Immutable run configuration.

    Built from the environment by :func:`load_settings`; tests construct it
    directly so they never depend on the ambient environment.
    """

    # --- external systems -------------------------------------------------
    product_api_url: str = "http://localhost:8001"
    warehouse_api_url: str = "http://localhost:8002"
    product_api_key: str = "challenge-product-key"
    warehouse_api_key: str = "challenge-warehouse-key"
    product_api_key_secret_id: Optional[str] = None
    warehouse_api_key_secret_id: Optional[str] = None

    # --- throughput -------------------------------------------------------
    page_size: int = PIM_MAX_PAGE_SIZE
    batch_size: int = WMS_MAX_BATCH_SIZE
    pim_requests_per_second: float = PIM_MAX_REQUESTS_PER_SECOND
    wms_requests_per_second: float = WMS_MAX_REQUESTS_PER_SECOND
    pim_concurrency: int = 8
    wms_concurrency: int = 16
    request_timeout_seconds: float = 30.0
    retry: RetrySettings = field(default_factory=RetrySettings)

    # --- storage ----------------------------------------------------------
    storage_backend: str = "local"  # local | s3
    local_storage_root: str = "./runtime/s3"
    s3_bucket: Optional[str] = None
    s3_prefix: str = ""
    s3_kms_key_id: Optional[str] = None
    scratch_dir: str = "./runtime/scratch"
    retention_days: int = 90

    # --- run state --------------------------------------------------------
    state_backend: str = "sqlite"  # sqlite | dynamodb
    sqlite_path: str = "./runtime/state/catalogue_sync.db"
    runs_table: str = "catalogue-sync-runs"
    pages_table: str = "catalogue-sync-pages"
    batches_table: str = "catalogue-sync-batches"
    ledger_table: str = "catalogue-sync-ledger"
    exceptions_table: str = "catalogue-sync-exceptions"
    aws_region: Optional[str] = None

    # --- queue (AWS only; the local runner dispatches in-process) ---------
    batch_queue_url: Optional[str] = None

    # --- observability ----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"  # json | text
    metrics_namespace: str = "CatalogueSync"
    emit_emf_metrics: bool = False

    # --- behaviour switches ----------------------------------------------
    resend_unknown: bool = False
    """Never resend an ambiguous ('UNKNOWN') delivery unless explicitly asked.

    See ARCHITECTURE.md section 8: a timed-out batch may already have been
    accepted by the WMS, so resending it is the one operation that can create a
    genuine duplicate. It is therefore an operator decision, not a default.
    """

    def validate(self) -> "Settings":
        if not 1 <= self.page_size <= PIM_MAX_PAGE_SIZE:
            raise ConfigError(
                f"page_size must be between 1 and {PIM_MAX_PAGE_SIZE} (PIM limit), "
                f"got {self.page_size}"
            )
        if not 1 <= self.batch_size <= WMS_MAX_BATCH_SIZE:
            raise ConfigError(
                f"batch_size must be between 1 and {WMS_MAX_BATCH_SIZE} (WMS limit), "
                f"got {self.batch_size}"
            )
        if self.pim_requests_per_second <= 0 or self.wms_requests_per_second <= 0:
            raise ConfigError("request rates must be > 0")
        if self.pim_concurrency < 1 or self.wms_concurrency < 1:
            raise ConfigError("concurrency must be >= 1")
        if self.storage_backend not in {"local", "s3"}:
            raise ConfigError("storage_backend must be 'local' or 's3'")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ConfigError("s3_bucket is required when storage_backend is 's3'")
        if self.state_backend not in {"sqlite", "dynamodb"}:
            raise ConfigError("state_backend must be 'sqlite' or 'dynamodb'")
        if self.retention_days < 1:
            raise ConfigError("retention_days must be >= 1")
        self.retry.validate()
        return self

    def with_overrides(self, **kwargs: object) -> "Settings":
        """Return a copy with ``kwargs`` applied (used by the CLI and tests)."""
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None}).validate()


def load_settings() -> Settings:
    """Build settings from environment variables and validate them."""
    settings = Settings(
        product_api_url=_env("PRODUCT_API_URL", "http://localhost:8001"),
        warehouse_api_url=_env("WAREHOUSE_API_URL", "http://localhost:8002"),
        product_api_key=_env("PRODUCT_API_KEY", "challenge-product-key"),
        warehouse_api_key=_env("WAREHOUSE_API_KEY", "challenge-warehouse-key"),
        product_api_key_secret_id=os.getenv("PRODUCT_API_KEY_SECRET_ID") or None,
        warehouse_api_key_secret_id=os.getenv("WAREHOUSE_API_KEY_SECRET_ID") or None,
        page_size=_env_int("PAGE_SIZE", PIM_MAX_PAGE_SIZE),
        batch_size=_env_int("BATCH_SIZE", WMS_MAX_BATCH_SIZE),
        pim_requests_per_second=_env_float("PIM_RPS", PIM_MAX_REQUESTS_PER_SECOND),
        wms_requests_per_second=_env_float("WMS_RPS", WMS_MAX_REQUESTS_PER_SECOND),
        pim_concurrency=_env_int("PIM_CONCURRENCY", 8),
        wms_concurrency=_env_int("WMS_CONCURRENCY", 16),
        request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 30.0),
        retry=RetrySettings(
            max_attempts=_env_int("RETRY_MAX_ATTEMPTS", 6),
            base_delay_seconds=_env_float("RETRY_BASE_DELAY_SECONDS", 0.5),
            max_delay_seconds=_env_float("RETRY_MAX_DELAY_SECONDS", 30.0),
        ),
        storage_backend=_env("STORAGE_BACKEND", "local"),
        local_storage_root=_env("LOCAL_STORAGE_ROOT", "./runtime/s3"),
        s3_bucket=os.getenv("S3_BUCKET") or None,
        s3_prefix=_env("S3_PREFIX", ""),
        s3_kms_key_id=os.getenv("S3_KMS_KEY_ID") or None,
        scratch_dir=_env("SCRATCH_DIR", "./runtime/scratch"),
        retention_days=_env_int("RETENTION_DAYS", 90),
        state_backend=_env("STATE_BACKEND", "sqlite"),
        sqlite_path=_env("SQLITE_PATH", "./runtime/state/catalogue_sync.db"),
        runs_table=_env("RUNS_TABLE", "catalogue-sync-runs"),
        pages_table=_env("PAGES_TABLE", "catalogue-sync-pages"),
        batches_table=_env("BATCHES_TABLE", "catalogue-sync-batches"),
        ledger_table=_env("LEDGER_TABLE", "catalogue-sync-ledger"),
        exceptions_table=_env("EXCEPTIONS_TABLE", "catalogue-sync-exceptions"),
        aws_region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None,
        batch_queue_url=os.getenv("BATCH_QUEUE_URL") or None,
        log_level=_env("LOG_LEVEL", "INFO"),
        log_format=_env("LOG_FORMAT", "json"),
        metrics_namespace=_env("METRICS_NAMESPACE", "CatalogueSync"),
        emit_emf_metrics=_env_bool("EMIT_EMF_METRICS", False),
        resend_unknown=_env_bool("RESEND_UNKNOWN", False),
    ).validate()
    return _resolve_secrets(settings)


def _resolve_secrets(settings: Settings) -> Settings:
    """Replace API keys with Secrets Manager values when a secret id is set.

    In AWS the keys never appear in task definitions or Terraform state; only
    the secret ARN does. Locally no secret id is configured, so the documented
    mock keys are used and nothing is fetched.
    """
    overrides = {}
    if settings.product_api_key_secret_id:
        overrides["product_api_key"] = _fetch_secret(
            settings.product_api_key_secret_id, settings.aws_region
        )
    if settings.warehouse_api_key_secret_id:
        overrides["warehouse_api_key"] = _fetch_secret(
            settings.warehouse_api_key_secret_id, settings.aws_region
        )
    return replace(settings, **overrides) if overrides else settings


def _fetch_secret(secret_id: str, region: Optional[str]) -> str:
    import boto3  # imported lazily so local runs need no AWS session

    client = boto3.client("secretsmanager", region_name=region)
    return client.get_secret_value(SecretId=secret_id)["SecretString"]
