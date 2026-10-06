"""Configuration validation.

The documented API limits are enforced here rather than discovered at runtime:
a page size of 501 is an HTTP 400 from the PIM and a batch of 101 is an HTTP 413
from the WMS, so both are configuration errors.
"""

from __future__ import annotations

import pytest

from app.config import (
    PIM_MAX_PAGE_SIZE,
    WMS_MAX_BATCH_SIZE,
    ConfigError,
    RetrySettings,
    Settings,
    load_settings,
)


def test_defaults_are_the_documented_limits():
    settings = Settings().validate()
    assert settings.page_size == PIM_MAX_PAGE_SIZE == 500
    assert settings.batch_size == WMS_MAX_BATCH_SIZE == 100
    assert settings.pim_requests_per_second == 10
    assert settings.wms_requests_per_second == 20
    assert settings.retention_days == 90
    assert settings.resend_unknown is False, "ambiguous deliveries are never auto-resent"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": 501},
        {"page_size": 0},
        {"batch_size": 101},
        {"batch_size": 0},
        {"pim_requests_per_second": 0},
        {"wms_requests_per_second": -1},
        {"pim_concurrency": 0},
        {"retention_days": 0},
        {"storage_backend": "gcs"},
        {"state_backend": "postgres"},
        {"storage_backend": "s3"},  # no bucket
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ConfigError):
        Settings(**kwargs).validate()


def test_retry_settings_are_validated():
    with pytest.raises(ConfigError):
        Settings(retry=RetrySettings(max_attempts=0)).validate()
    with pytest.raises(ConfigError):
        Settings(retry=RetrySettings(base_delay_seconds=10, max_delay_seconds=1)).validate()


def test_overrides_are_applied_and_revalidated():
    settings = Settings().with_overrides(page_size=100, batch_size=None)
    assert settings.page_size == 100
    assert settings.batch_size == 100, "None means 'leave it alone'"

    with pytest.raises(ConfigError):
        Settings().with_overrides(batch_size=5000)


def test_settings_are_loaded_from_the_environment(monkeypatch):
    monkeypatch.setenv("PRODUCT_API_URL", "https://pim.example.com")
    monkeypatch.setenv("PAGE_SIZE", "250")
    monkeypatch.setenv("WMS_RPS", "5")
    monkeypatch.setenv("RESEND_UNKNOWN", "true")

    settings = load_settings()

    assert settings.product_api_url == "https://pim.example.com"
    assert settings.page_size == 250
    assert settings.wms_requests_per_second == 5
    assert settings.resend_unknown is True


def test_an_s3_backend_requires_a_bucket(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(ConfigError):
        load_settings()


def test_no_api_key_is_fetched_when_no_secret_id_is_configured(monkeypatch):
    """A local run must never need an AWS session."""
    monkeypatch.delenv("PRODUCT_API_KEY_SECRET_ID", raising=False)
    monkeypatch.delenv("WAREHOUSE_API_KEY_SECRET_ID", raising=False)

    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("Secrets Manager must not be contacted locally")

    monkeypatch.setattr("app.config._fetch_secret", explode)
    settings = load_settings()

    assert settings.product_api_key == "challenge-product-key"


def test_api_keys_come_from_secrets_manager_when_configured(monkeypatch):
    monkeypatch.setenv("PRODUCT_API_KEY_SECRET_ID", "arn:aws:secretsmanager:::secret/pim")
    monkeypatch.setattr(
        "app.config._fetch_secret", lambda secret_id, region: f"resolved::{secret_id}"
    )

    settings = load_settings()

    assert settings.product_api_key == "resolved::arn:aws:secretsmanager:::secret/pim"
