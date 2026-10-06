"""Object storage interface and the local filesystem implementation.

The export is written through this interface so the run is identical locally and
in AWS. Only whole objects are read or written: raw pages are small (<=500
products) and the CSV is streamed via a local scratch file, which keeps memory
flat as the catalogue grows towards a million products.

Deliberately no ``delete``: retention is an S3 lifecycle rule (90 days), and the
application roles get no delete permission at all, so a bug cannot destroy the
audit trail.
"""

from __future__ import annotations

import abc
import os
import shutil
from pathlib import Path
from typing import List, Optional


class ObjectNotFound(KeyError):
    pass


class ObjectStore(abc.ABC):
    @abc.abstractmethod
    def put_bytes(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> str:
        """Store ``data`` under ``key`` and return its URI."""

    @abc.abstractmethod
    def put_file(self, local_path: str, key: str, *, content_type: Optional[str] = None) -> str:
        """Upload a local file under ``key`` and return its URI."""

    @abc.abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Return the object body, raising :class:`ObjectNotFound` if missing."""

    @abc.abstractmethod
    def download_to(self, key: str, local_path: str) -> str:
        """Copy the object to ``local_path`` and return that path."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abc.abstractmethod
    def list_keys(self, prefix: str) -> List[str]:
        ...

    @abc.abstractmethod
    def uri(self, key: str) -> str:
        ...


class LocalObjectStore(ObjectStore):
    """Filesystem stand-in for S3, used by local runs and tests.

    Keeps the same semantics the original ``S3Store`` had (a root directory with
    keys as relative paths) so local output stays inspectable.
    """

    def __init__(self, root: str = "./runtime/s3") -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        normalised = key.lstrip("/")
        path = (self.root / normalised).resolve()
        root = self.root.resolve()
        if root != path and root not in path.parents:
            raise ValueError(f"key escapes the storage root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name then rename, so a crash never leaves a
        # half-written page that a resume would treat as complete.
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return self.uri(key)

    def put_file(self, local_path: str, key: str, *, content_type: Optional[str] = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        shutil.copyfile(local_path, tmp)
        os.replace(tmp, path)
        return self.uri(key)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFound(key) from exc

    def download_to(self, key: str, local_path: str) -> str:
        path = self._path(key)
        if not path.exists():
            raise ObjectNotFound(key)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        return str(destination)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str) -> List[str]:
        root = self.root.resolve()
        base = self._path(prefix)
        search_root = base if base.is_dir() else base.parent
        if not search_root.exists():
            return []
        keys = []
        for path in sorted(search_root.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                key = str(path.resolve().relative_to(root))
                if key.startswith(prefix.lstrip("/")):
                    keys.append(key)
        return keys

    def uri(self, key: str) -> str:
        return f"file://{self._path(key)}"
