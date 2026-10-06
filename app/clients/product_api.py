"""PIM (Product Information Management) client.

Responsibilities kept deliberately narrow: fetch one page, respect the rate
limit, retry transient failures, and hand back the page unchanged. Pagination
strategy, checkpointing and storage live in the export service, because that is
where a restart has to make decisions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from app.clients.rate_limiter import TokenBucket
from app.clients.retry import (
    PermanentApiError,
    RetryPolicy,
    classify_response,
    classify_transport_error,
    execute_with_retries,
)
from app.config import Settings
from app.models import PimPage
from app.observability import Metrics, get_logger


class ProductApiClient:
    """Paginated, rate-limited, retrying reader for the PIM catalogue."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Optional[httpx.Client] = None,
        limiter: Optional[TokenBucket] = None,
        policy: Optional[RetryPolicy] = None,
        metrics: Optional[Metrics] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.product_api_url,
            timeout=settings.request_timeout_seconds,
            headers={"X-API-Key": settings.product_api_key},
            # Enough connections for the configured concurrency; the rate
            # limiter, not the pool, is what bounds request rate.
            limits=httpx.Limits(
                max_connections=max(settings.pim_concurrency * 2, 10),
                max_keepalive_connections=max(settings.pim_concurrency, 5),
            ),
        )
        self.limiter = limiter or TokenBucket(
            settings.pim_requests_per_second, name="pim"
        )
        self.policy = policy or RetryPolicy(settings.retry)
        self.metrics = metrics or Metrics(settings.metrics_namespace)
        self.log = logger or get_logger("app.clients.product_api")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ProductApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- API ---------------------------------------------------------------

    def fetch_page(self, page: int, page_size: Optional[int] = None) -> PimPage:
        """Fetch one page, retrying transient failures.

        GETs are idempotent, so every transport error is safe to repeat; an
        ambiguous outcome cannot exist for a read.
        """
        size = page_size or self.settings.page_size
        params = {"page": page, "page_size": size}
        context = f"GET /products page={page}"

        def attempt(attempt_number: int) -> PimPage:
            self.limiter.acquire()
            self.metrics.incr("PimRequests")
            try:
                response = self._client.get(
                    "/products",
                    params=params,
                    headers={"X-API-Key": self.settings.product_api_key},
                )
            except httpx.HTTPError as error:
                self.metrics.incr("PimTransportErrors")
                raise classify_transport_error(error, idempotent=True, context=context)
            self._record_status(response.status_code)
            classify_response(response, context=context)
            try:
                payload: Dict[str, Any] = response.json()
            except ValueError as error:
                # A 200 that is not JSON is a contract break, not a transient
                # fault; retrying will not fix it.
                raise PermanentApiError(f"{context} returned invalid JSON: {error}")
            return PimPage.from_payload(payload, page=page, page_size=size)

        result = execute_with_retries(
            attempt,
            policy=self.policy,
            context=context,
            logger=self.log,
            on_retry=lambda *_: self.metrics.incr("PimRetries"),
        )
        self.metrics.incr("PimProductsFetched", len(result.products))
        return result

    def page_count(self, total: int, page_size: Optional[int] = None) -> int:
        size = page_size or self.settings.page_size
        return (total + size - 1) // size

    # -- internals ---------------------------------------------------------

    def _record_status(self, status_code: int) -> None:
        if status_code == 429:
            self.metrics.incr("Pim429")
        elif 500 <= status_code < 600:
            self.metrics.incr("Pim5xx")
        elif status_code == 401:
            self.metrics.incr("PimAuthFailures")
