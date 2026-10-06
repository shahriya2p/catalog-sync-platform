"""Object store contract.

The same test body runs against the local filesystem store and, when moto is
installed, against the real S3 API. Keeping one contract test for both is what
makes it credible that a local run exercises the production code path.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.storage.object_store import LocalObjectStore, ObjectNotFound
from app.storage.s3 import build_object_store


@pytest.fixture(params=["local", "s3"])
def object_store(request, tmp_path):
    if request.param == "local":
        yield LocalObjectStore(str(tmp_path / "s3"))
        return

    moto_s3 = pytest.importorskip("moto", reason="moto is only in requirements.txt")
    import boto3

    with moto_s3.mock_aws():
        boto3.client("s3", region_name="eu-west-1").create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )
        from app.storage.s3 import S3ObjectStore

        yield S3ObjectStore("test-bucket", prefix="catalogue", region_name="eu-west-1")


def test_round_trip_bytes(object_store):
    object_store.put_bytes("raw/run-1/pages/000001.json", b'{"products": []}')
    assert object_store.get_bytes("raw/run-1/pages/000001.json") == b'{"products": []}'


def test_exists_distinguishes_present_from_absent(object_store):
    assert object_store.exists("exports/run-1/catalogue.csv") is False
    object_store.put_bytes("exports/run-1/catalogue.csv", b"id,name\n")
    assert object_store.exists("exports/run-1/catalogue.csv") is True


def test_missing_objects_raise_a_typed_error(object_store):
    with pytest.raises(ObjectNotFound):
        object_store.get_bytes("exports/nope/catalogue.csv")


def test_upload_and_download_a_file(object_store, tmp_path):
    source = tmp_path / "catalogue.csv"
    source.write_text("id,name\nP1,Widget\n", encoding="utf-8")

    object_store.put_file(str(source), "exports/run-1/catalogue.csv", content_type="text/csv")
    destination = tmp_path / "downloaded" / "catalogue.csv"
    object_store.download_to("exports/run-1/catalogue.csv", str(destination))

    assert destination.read_text(encoding="utf-8") == "id,name\nP1,Widget\n"


def test_keys_are_listed_under_a_prefix(object_store):
    for page in range(1, 4):
        object_store.put_bytes(f"raw/run-1/pages/{page:06d}.json", b"{}")
    object_store.put_bytes("raw/run-2/pages/000001.json", b"{}")

    keys = object_store.list_keys("raw/run-1/")

    assert sorted(keys) == [
        "raw/run-1/pages/000001.json",
        "raw/run-1/pages/000002.json",
        "raw/run-1/pages/000003.json",
    ]


def test_overwriting_is_atomic_from_a_readers_point_of_view(object_store):
    object_store.put_bytes("exports/run-1/manifest.json", b'{"v": 1}')
    object_store.put_bytes("exports/run-1/manifest.json", b'{"v": 2}')
    assert object_store.get_bytes("exports/run-1/manifest.json") == b'{"v": 2}'


def test_uri_is_reported_for_logs(object_store):
    uri = object_store.uri("exports/run-1/catalogue.csv")
    assert uri.startswith(("file://", "s3://"))
    assert uri.endswith("exports/run-1/catalogue.csv")


# --- the factory ------------------------------------------------------------


def test_factory_returns_the_local_store_without_touching_aws(tmp_path):
    settings = Settings(
        storage_backend="local", local_storage_root=str(tmp_path)
    ).validate()
    assert isinstance(build_object_store(settings), LocalObjectStore)


def test_keys_cannot_escape_the_local_root(tmp_path):
    store = LocalObjectStore(str(tmp_path / "s3"))
    with pytest.raises(ValueError):
        store.put_bytes("../../etc/passwd", b"nope")
