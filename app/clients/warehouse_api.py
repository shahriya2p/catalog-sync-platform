"""WMS (Warehouse Management System) client.

This is the risky side of the integration: it writes. Three rules shape the
implementation.

1. Never exceed the documented batch size. Sending 101 products is a guaranteed
   HTTP 413, so the limit is asserted locally instead of being discovered at
   runtime.
2. A 200 response is not "all good". It carries ``accepted`` and ``rejected``
   lists, and a SKU can be absent from both. Anything absent is reported as
   ``unknown`` rather than assumed delivered.
3. A request that was sent but produced no response is ambiguous, not failed.
   It is reported as ``BatchStatus.UNKNOWN`` and is never retried here, because
   a retry is the one action that can create a true duplicate in the warehouse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.clients.rate_limiter import TokenBucket
from app.clients.retry import (
    AmbiguousOutcome,
    ApiError,
    PermanentApiError,
    RetriesExhausted,
    RetryPolicy,
    classify_response,
    classify_transport_error,
    execute_with_retries,
)
from app.config import WMS_MAX_BATCH_SIZE, Settings
from app.models import BatchResult, BatchStatus
from app.observability import Metrics, get_logger


class WarehouseApiClient:
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
            base_url=settings.warehouse_api_url,
            timeout=settings.request_timeout_seconds,
            headers={"X-API-Key": settings.warehouse_api_key},
            limits=httpx.Limits(
                max_connections=max(settings.wms_concurrency * 2, 10),
                max_keepalive_connections=max(settings.wms_concurrency, 5),
            ),
        )
        self.limiter = limiter or TokenBucket(
            settings.wms_requests_per_second, name="wms"
        )
        self.policy = policy or RetryPolicy(settings.retry)
        self.metrics = metrics or Metrics(settings.metrics_namespace)
        self.log = logger or get_logger("app.clients.warehouse_api")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WarehouseApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- API ---------------------------------------------------------------

    def send_batch(
        self, products: Sequence[Dict[str, Any]], *, idempotency_key: Optional[str] = None
    ) -> BatchResult:
        """Send one batch and report a per-SKU outcome for every product.

        Always returns a :class:`BatchResult`; it does not raise for API
        failures, because the caller has to record an outcome for each SKU
        whatever happened.

        ``idempotency_key`` is sent as a header. The current WMS ignores it and
        has no documented idempotency support, so it buys nothing today; it is
        sent anyway so that the day the WMS implements it, ambiguous outcomes
        become safely retryable without a client change. See ARCHITECTURE.md
        section 8.2.
        """
        if not products:
            return BatchResult(status=BatchStatus.SKIPPED)
        limit = min(self.settings.batch_size, WMS_MAX_BATCH_SIZE)
        if len(products) > limit:
            raise ValueError(
                f"batch of {len(products)} exceeds the WMS maximum of {limit}"
            )

        skus = [str(item.get("sku")) for item in products]
        context = f"POST /products/batch size={len(products)}"
        headers = {"X-API-Key": self.settings.warehouse_api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempts_made = 0

        def attempt(attempt_number: int) -> httpx.Response:
            nonlocal attempts_made
            attempts_made = attempt_number
            self.limiter.acquire()
            self.metrics.incr("WmsRequests")
            try:
                response = self._client.post(
                    "/products/batch",
                    json={"products": list(products)},
                    headers=headers,
                )
            except httpx.HTTPError as error:
                self.metrics.incr("WmsTransportErrors")
                # idempotent=False: a POST that was sent but not answered may
                # have been applied.
                raise classify_transport_error(error, idempotent=False, context=context)
            self._record_status(response.status_code)
            classify_response(response, context=context)
            return response

        try:
            response = execute_with_retries(
                attempt,
                policy=self.policy,
                context=context,
                logger=self.log,
                on_retry=lambda *_: self.metrics.incr("WmsRetries"),
                retry_ambiguous=False,
            )
        except AmbiguousOutcome as error:
            self.metrics.incr("WmsAmbiguousOutcomes")
            self.metrics.incr("ProductsUnknown", len(skus))
            self.log.error(
                "ambiguous WMS outcome; products marked UNKNOWN and not resent",
                extra={"size": len(products), "error": str(error)},
            )
            return BatchResult(
                status=BatchStatus.UNKNOWN,
                unknown=skus,
                attempts=attempts_made,
                detail=f"ambiguous outcome: {error}",
            )
        except PermanentApiError as error:
            # 400/401/413: our payload or configuration is wrong. Retrying
            # cannot help and would burn the rate budget.
            self.metrics.incr("WmsPermanentFailures")
            self.log.error(
                "permanent WMS rejection; batch not delivered",
                extra={
                    "size": len(products),
                    "status_code": error.status_code,
                    "error": str(error),
                },
            )
            return BatchResult(
                status=BatchStatus.FAILED_PERMANENT,
                http_status=error.status_code,
                attempts=attempts_made,
                detail=str(error),
            )
        except RetriesExhausted as error:
            # Every attempt came back as a complete HTTP error response, so the
            # batch was not processed (assumptions A1/A2). Safe to retry on a
            # later resume.
            self.metrics.incr("WmsTransientFailures")
            self.log.error(
                "WMS batch failed after retries; will be retried on resume",
                extra={
                    "size": len(products),
                    "status_code": error.status_code,
                    "attempts": attempts_made,
                    "error": str(error),
                },
            )
            return BatchResult(
                status=BatchStatus.FAILED_TRANSIENT,
                http_status=error.status_code,
                attempts=attempts_made,
                detail=str(error),
            )
        except ApiError as error:  # pragma: no cover - defensive
            return BatchResult(
                status=BatchStatus.FAILED_TRANSIENT,
                attempts=attempts_made,
                detail=str(error),
            )

        return self._parse_success(response, skus, attempts_made)

    # -- internals ---------------------------------------------------------

    def _parse_success(
        self, response: httpx.Response, skus: List[str], attempts: int
    ) -> BatchResult:
        try:
            payload = response.json()
        except ValueError:
            # A 200 we cannot read is ambiguous: the WMS may well have stored
            # the batch. Treat it as unknown, not as success.
            self.metrics.incr("WmsUnparseableResponses")
            return BatchResult(
                status=BatchStatus.UNKNOWN,
                unknown=skus,
                http_status=response.status_code,
                attempts=attempts,
                detail="unparseable 200 response",
            )

        raw_accepted = payload.get("accepted") or []
        raw_rejected = payload.get("rejected") or []

        # The API returns accepted SKUs as bare strings and rejections as
        # objects; both shapes are normalised here.
        accepted = [str(sku) for sku in raw_accepted if sku is not None]
        rejected: List[tuple] = []
        for entry in raw_rejected:
            if isinstance(entry, dict):
                sku = entry.get("sku")
                reason = str(entry.get("reason") or "rejected by WMS")
            else:  # pragma: no cover - tolerate a bare string form
                sku, reason = entry, "rejected by WMS"
            if sku is None:
                # A rejection with no SKU cannot be attributed to a product.
                # It is counted and logged, not silently dropped.
                self.metrics.incr("WmsUnattributedRejections")
                self.log.warning(
                    "WMS rejected a record without a SKU", extra={"reason": reason}
                )
                continue
            rejected.append((str(sku), reason))

        reported = set(accepted) | {sku for sku, _ in rejected}
        unknown = [sku for sku in skus if sku not in reported]

        self.metrics.incr("WmsBatchesSent")
        self.metrics.incr("ProductsAccepted", len(accepted))
        self.metrics.incr("ProductsRejected", len(rejected))
        if unknown:
            self.metrics.incr("ProductsUnknown", len(unknown))
            self.log.warning(
                "WMS 200 did not report every SKU; unreported SKUs marked UNKNOWN",
                extra={"unreported": len(unknown), "batch_size": len(skus)},
            )

        return BatchResult(
            status=BatchStatus.COMPLETED,
            accepted=accepted,
            rejected=rejected,
            unknown=unknown,
            http_status=response.status_code,
            attempts=attempts,
        )

    def _record_status(self, status_code: int) -> None:
        if status_code == 429:
            self.metrics.incr("Wms429")
        elif 500 <= status_code < 600:
            self.metrics.incr("Wms5xx")
        elif status_code == 413:
            self.metrics.incr("Wms413")
        elif status_code == 401:
            self.metrics.incr("WmsAuthFailures")
