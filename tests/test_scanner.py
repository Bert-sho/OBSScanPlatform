import csv
import inspect
from pathlib import Path

import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.models import BucketInfo
from obs_scan_platform.scanner import Scanner, is_owned_bucket, parse_int_or_none, run_scan


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get_json(self, url, *, params, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


def make_scanner() -> tuple[Scanner, ApplicationConfig, BucketInfo]:
    application = ApplicationConfig(
        appid="app.one",
        name="App One",
        endpoint="http://obs.example",
        apptoken="token-1",
    )
    config = AppConfigFile(
        defaults=Thresholds(
            large_directory_bytes=100,
            large_file_bytes=10,
            inactive_directory_days=180,
        ),
        applications=[application],
    )
    bucket = BucketInfo("bucket-id-1", "bucket-name-1", "HEC", "cn-east-3", "owner", None)
    return Scanner(config), application, bucket


def test_is_owned_bucket_excludes_shared_bucket():
    assert is_owned_bucket(BucketInfo("1", "a", "HEC", "cn-east-3", "owner", None))
    assert not is_owned_bucket(BucketInfo("2", "b", "HEC", "cn-east-3", "owner", "other"))
    assert not is_owned_bucket(BucketInfo("3", "c", "HEC", "cn-east-3", "reader", None))


def test_parse_int_or_none_handles_dirty_values():
    assert parse_int_or_none("123") == 123
    assert parse_int_or_none(456) == 456
    assert parse_int_or_none("17676892757s82") is None
    assert parse_int_or_none(None) is None


def test_run_scan_is_async_public_api():
    assert inspect.iscoroutinefunction(run_scan)


@pytest.mark.asyncio
async def test_get_bucket_endpoint_uses_bucket_name_as_bucketid_and_id_as_bucket_uid():
    scanner, application, bucket = make_scanner()
    client = FakeClient([{"result": "http://bucket-endpoint/"}])

    endpoint = await scanner._get_bucket_endpoint(application, bucket, client)

    assert endpoint == "http://bucket-endpoint"
    call = client.calls[0]
    assert call["url"].endswith("/rest/s3/bucket/endpoint")
    assert call["params"]["bucketid"] == bucket.name
    assert call["params"]["bucketUid"] == bucket.bucket_id


@pytest.mark.asyncio
async def test_discover_root_uses_bucket_filelist_and_parses_first_level_items():
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/nested/"},
                        {"objectType": "object", "objectKey": "root.txt"},
                    ]
                }
            }
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    call = client.calls[0]
    assert call["url"].endswith("/rest/s3/bucket/filelist")
    assert discovery.prefixes == ["alpha/"]
    assert discovery.root_files == ["root.txt"]


@pytest.mark.asyncio
async def test_collect_root_files_uses_bucket_name_as_bucketid_and_writes_csv(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    client = FakeClient([{"result": {"objectKey": {"objectKey": "root.txt", "size": "12", "lastModifyTime": "1000"}}}])

    await scanner._collect_root_files(
        application,
        bucket,
        "http://bucket-endpoint",
        ["root.txt"],
        tmp_path,
        client,
    )

    call = client.calls[0]
    assert call["params"]["bucketid"] == bucket.name
    assert call["params"]["bucketld"] == bucket.bucket_id
    rows = list(csv.DictReader((tmp_path / "root_files.csv").open(newline="", encoding="utf-8")))
    assert rows == [{"object_key": "root.txt", "size_bytes": "12", "last_modified_ms": "1000"}]


@pytest.mark.asyncio
async def test_collect_prefix_stops_when_truncated_string_false(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {
                "result": {
                    "objectkeys": [{"objectKey": "alpha/file.txt", "size": "5", "lastModifyTime": "2000"}],
                    "truncated": "false",
                    "nextmarker": "should-not-continue",
                }
            }
        ]
    )

    await scanner._collect_prefix(application, bucket, "http://bucket-endpoint", "alpha/", tmp_path, client)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["params"]["bucketid"] == bucket.name
    assert call["params"]["bucketld"] == bucket.bucket_id
