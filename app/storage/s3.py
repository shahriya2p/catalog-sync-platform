"""S3 implementation of the object store, plus the backend factory.

``boto3`` is imported lazily so a local run (or a test run) needs no AWS
session, credentials or region. Uploads go through ``upload_file``/``put_object``
from the managed transfer API, which switches to multipart automatically — the
catalogue CSV is expected to pass 100 MB at a million products.
"""

from __future__ import annotations

from typing import Any, List, Optional

from app.config import Settings
from app.storage.object_store import LocalObjectStore, ObjectNotFound, ObjectStore


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        client: Any = None,
        kms_key_id: Optional[str] = None,
        region_name: Optional[str] = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.kms_key_id = kms_key_id
        if client is not None:
            self._client = client
        else:
            import boto3  # lazy: keeps local runs free of AWS imports

            self._client = boto3.client("s3", region_name=region_name)

    # -- helpers -----------------------------------------------------------

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _encryption_args(self, content_type: Optional[str]) -> dict:
        args: dict = {}
        if content_type:
            args["ContentType"] = content_type
        if self.kms_key_id:
            args["ServerSideEncryption"] = "aws:kms"
            args["SSEKMSKeyId"] = self.kms_key_id
        else:
            # The bucket enforces encryption too; being explicit means a
            # misconfigured bucket policy fails loudly instead of silently
            # storing plaintext.
            args["ServerSideEncryption"] = "AES256"
        return args

    # -- ObjectStore -------------------------------------------------------

    def put_bytes(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> str:
        self._client.put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data, **self._encryption_args(content_type)
        )
        return self.uri(key)

    def put_file(self, local_path: str, key: str, *, content_type: Optional[str] = None) -> str:
        self._client.upload_file(
            local_path,
            self.bucket,
            self._key(key),
            ExtraArgs=self._encryption_args(content_type),
        )
        return self.uri(key)

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as error:  # botocore exceptions are created dynamically
            if _is_missing(error):
                raise ObjectNotFound(key) from error
            raise
        return response["Body"].read()

    def download_to(self, key: str, local_path: str) -> str:
        import os

        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        try:
            self._client.download_file(self.bucket, self._key(key), local_path)
        except Exception as error:
            if _is_missing(error):
                raise ObjectNotFound(key) from error
            raise
        return local_path

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as error:
            if _is_missing(error):
                return False
            raise

    def list_keys(self, prefix: str) -> List[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        offset = len(self.prefix) + 1 if self.prefix else 0
        keys: List[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for item in page.get("Contents", []):
                keys.append(item["Key"][offset:])
        return keys

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


def _is_missing(error: Exception) -> bool:
    code = getattr(error, "response", {}).get("Error", {}).get("Code") if hasattr(
        error, "response"
    ) else None
    return code in {"404", "NoSuchKey", "NotFound"} or type(error).__name__ in {
        "NoSuchKey",
        "404",
    }


def build_object_store(settings: Settings) -> ObjectStore:
    """Return the configured object store (``local`` or ``s3``)."""
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:  # pragma: no cover - validated in config
            raise ValueError("s3_bucket must be set for the s3 storage backend")
        return S3ObjectStore(
            settings.s3_bucket,
            prefix=settings.s3_prefix,
            kms_key_id=settings.s3_kms_key_id,
            region_name=settings.aws_region,
        )
    return LocalObjectStore(settings.local_storage_root)
