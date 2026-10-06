"""Shared test fixtures.

The supplied mock services are driven in-process through Starlette's
``TestClient``, which is an ``httpx.Client``. That gives the tests the real mock
behaviour - the same pagination, the same deterministic throttling, the same
partial-success responses - with no ports, no containers and no sleeping.

The mock modules are loaded fresh for each test that asks for them, so their
module-level request counters start at zero and the "every 11th/13th request
fails" behaviour is reproducible. Their code is never modified.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.config import RetrySettings, Settings
from app.state.sqlite_store import SqliteRunStateStore
from app.storage.object_store import LocalObjectStore

REPO_ROOT = Path(__file__).resolve().parent.parent

PRODUCT_API_KEY = "challenge-product-key"
WAREHOUSE_API_KEY = "challenge-warehouse-key"


def _load_module(name: str, relative_path: str) -> Any:
    """Import a module from a path (the mock directories are not packages)."""
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pim_app() -> Any:
    return _load_module("pim_mock", "mock-services/product-api/app/main.py").app


@pytest.fixture
def wms_app() -> Any:
    return _load_module("wms_mock", "mock-services/warehouse-api/app/main.py").app


@pytest.fixture
def pim_http(pim_app: Any) -> Any:
    with TestClient(pim_app, base_url="http://pim.test", raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def wms_http(wms_app: Any) -> Any:
    with TestClient(wms_app, base_url="http://wms.test", raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for fast, deterministic tests.

    Rates are raised and backoff shortened so the suite does not spend its time
    asleep; the limiter and the backoff maths have their own dedicated tests
    with an injected clock.
    """
    return Settings(
        product_api_url="http://pim.test",
        warehouse_api_url="http://wms.test",
        product_api_key=PRODUCT_API_KEY,
        warehouse_api_key=WAREHOUSE_API_KEY,
        page_size=500,
        batch_size=100,
        pim_requests_per_second=1000.0,
        wms_requests_per_second=1000.0,
        pim_concurrency=4,
        wms_concurrency=4,
        retry=RetrySettings(max_attempts=4, base_delay_seconds=0.001, max_delay_seconds=0.01),
        storage_backend="local",
        local_storage_root=str(tmp_path / "s3"),
        scratch_dir=str(tmp_path / "scratch"),
        state_backend="sqlite",
        sqlite_path=str(tmp_path / "state.db"),
        log_format="text",
    ).validate()


@pytest.fixture
def store(settings: Settings) -> LocalObjectStore:
    return LocalObjectStore(settings.local_storage_root)


@pytest.fixture
def state(settings: Settings) -> SqliteRunStateStore:
    store = SqliteRunStateStore(settings.sqlite_path)
    yield store
    store.close()


class RecordingClient:
    """Wraps an HTTP client and records every WMS exchange.

    Recording the response as well as the request is what allows the tests to
    assert the property that actually matters to the warehouse: a product is
    never written again *after* the WMS has accepted it. A batch that is retried
    because the WMS answered 429 or 5xx is a legitimate resend - the WMS told us
    it had not processed it - and that distinction is invisible unless the
    responses are captured too.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.sent_skus: List[str] = []
        self.requests: List[Dict[str, Any]] = []
        self.exchanges: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        payload = kwargs.get("json") or {}
        products = payload.get("products", [])
        skus = [str(item.get("sku")) for item in products]
        self.requests.append(payload)
        self.sent_skus.extend(skus)
        response = self._inner.post(url, **kwargs)
        accepted: List[str] = []
        if response.status_code == 200:
            try:
                accepted = [str(sku) for sku in (response.json().get("accepted") or [])]
            except ValueError:  # pragma: no cover - defensive
                accepted = []
        self.exchanges.append(
            {"skus": skus, "status": response.status_code, "accepted": accepted}
        )
        return response

    def resent_after_acceptance(self) -> List[str]:
        """SKUs that were sent again after the WMS had already accepted them."""
        accepted_at: Dict[str, int] = {}
        offenders: List[str] = []
        for index, exchange in enumerate(self.exchanges):
            for sku in exchange["skus"]:
                if sku in accepted_at and accepted_at[sku] < index:
                    offenders.append(sku)
            for sku in exchange["accepted"]:
                accepted_at.setdefault(sku, index)
        return offenders

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._inner.get(url, **kwargs)

    def close(self) -> None:
        self._inner.close()

    @property
    def send_count(self) -> int:
        return len(self.requests)

    def count_for(self, sku: str) -> int:
        return self.sent_skus.count(sku)


@pytest.fixture
def recording_wms(wms_http: Any) -> RecordingClient:
    return RecordingClient(wms_http)
