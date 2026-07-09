import asyncio
import base64
import csv
import json
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.models import BucketInfo, BucketScanResult, ScanStatus
from obs_scan_platform.paths import prefix_temp_filename
from obs_scan_platform.scanner import (
    Scanner,
    _rollup_status,
    is_owned_bucket,
    parse_int_or_none,
    run_scan,
    should_scan_bucket,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get_json(self, url, *, params, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


class ConcurrentFakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def get_json(self, url, *, params, headers=None):
        async with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        async with self.lock:
            self.active -= 1
            self.calls.append({"url": url, "params": params, "headers": headers})
            return self.responses.pop(0)


class RepeatingRootOffsetClient:
    def __init__(self):
        self.calls = []

    async def get_json(self, url, *, params, headers=None):
        await asyncio.sleep(0)
        self.calls.append({"url": url, "params": params, "headers": headers})
        return {"result": {"files": [], "nextOffset": 1}}


def decode_request_body(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(call["params"]["requestbody"].encode("utf-8")).decode("utf-8"))


def make_scanner() -> tuple[Scanner, ApplicationConfig, BucketInfo]:
    application = ApplicationConfig(
        appid="app.one",
        name="App One",
        endpoint="http://app-obs.example",
        apptoken="token-1",
    )
    config = AppConfigFile(
        endpoint="http://global-obs.example",
        defaults=Thresholds(
            large_directory_bytes=100,
            large_file_bytes=10,
            inactive_directory_days=180,
        ),
        applications=[application],
    )
    bucket = BucketInfo("bucket-id-1", "bucket-name-1", "HEC", "cn-east-3", "owner", None)
    return Scanner(config), application, bucket


def test_rollup_status_handles_empty_success_failed_and_partial():
    assert _rollup_status([]) == ScanStatus.SUCCESS.value
    assert _rollup_status([ScanStatus.SUCCESS.value]) == ScanStatus.SUCCESS.value
    assert _rollup_status([ScanStatus.FAILED.value]) == ScanStatus.FAILED.value
    assert _rollup_status([ScanStatus.SUCCESS.value, ScanStatus.FAILED.value]) == ScanStatus.PARTIAL_FAILED.value
    assert _rollup_status([ScanStatus.PARTIAL_FAILED.value, ScanStatus.SUCCESS.value]) == ScanStatus.PARTIAL_FAILED.value


def test_is_owned_bucket_excludes_shared_bucket():
    assert is_owned_bucket(BucketInfo("1", "a", "HEC", "cn-east-3", "owner", None))
    assert not is_owned_bucket(BucketInfo("2", "b", "HEC", "cn-east-3", "owner", "other"))
    assert not is_owned_bucket(BucketInfo("3", "c", "HEC", "cn-east-3", "reader", None))


def test_should_scan_bucket_includes_owned_and_optional_shared_buckets():
    owned = BucketInfo("1", "a", "HEC", "cn-east-3", "owner", None)
    shared = BucketInfo("2", "b", "HEC", "cn-east-3", "owner", "other")
    reader = BucketInfo("3", "c", "HEC", "cn-east-3", "reader", None)

    assert should_scan_bucket(owned, include_shared=False)
    assert should_scan_bucket(owned, include_shared=True)
    assert not should_scan_bucket(shared, include_shared=False)
    assert should_scan_bucket(shared, include_shared=True)
    assert not should_scan_bucket(reader, include_shared=True)


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
    assert call["url"].startswith("http://global-obs.example/")
    assert call["url"].endswith("/rest/s3/bucket/endpoint")
    assert call["params"]["bucketid"] == bucket.name
    assert call["params"]["bucketUid"] == bucket.bucket_id


@pytest.mark.asyncio
async def test_list_buckets_uses_global_endpoint_and_includes_shared_when_enabled():
    scanner, application, _ = make_scanner()
    application.scan_shared_buckets = True
    client = FakeClient(
        [
            {
                "result": {
                    "buckets": [
                        {
                            "id": "owned-id",
                            "name": "owned-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "owner",
                            "shareFrom": None,
                        },
                        {
                            "id": "shared-id",
                            "name": "shared-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "owner",
                            "shareFrom": "other-app",
                        },
                        {
                            "id": "reader-id",
                            "name": "reader-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "reader",
                            "shareFrom": None,
                        },
                    ]
                }
            }
        ]
    )

    buckets = await scanner._list_buckets(application, client)

    assert [bucket.name for bucket in buckets] == ["owned-bucket", "shared-bucket"]
    assert client.calls[0]["url"].startswith("http://global-obs.example/")


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
            },
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    call = client.calls[0]
    assert call["url"].startswith("http://global-obs.example/")
    assert call["url"].endswith("/rest/s3/bucket/filelist")
    assert discovery.prefixes == ["alpha/"]
    assert discovery.root_files == ["root.txt"]


@pytest.mark.asyncio
async def test_discover_root_treats_capitalized_folder_as_prefix():
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "Folder", "objectKey": "alpha/nested/"},
                    ]
                }
            },
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/"]
    assert discovery.root_files == []


@pytest.mark.asyncio
async def test_discover_root_recurses_to_filelist_depth_and_finds_nested_prefixes():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "object", "objectKey": "root.txt"},
                    ],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/beta/"},
                        {"objectType": "object", "objectKey": "alpha/ignored.txt"},
                    ],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert [decode_request_body(call)["path"] for call in client.calls] == ["/", "/alpha/"]
    assert discovery.prefixes == ["alpha/"]
    assert discovery.root_files == ["root.txt"]


@pytest.mark.asyncio
async def test_discover_root_limits_recursive_filelist_tasks_but_keeps_discovered_prefixes():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 5
    scanner.config.scan.filelist_task_limit_per_bucket = 2
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "bravo/"},
                    ],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/beta/"},
                    ],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert [decode_request_body(call)["path"] for call in client.calls] == ["/", "/alpha/"]
    assert discovery.prefixes == ["alpha/", "bravo/"]
    assert discovery.root_files == []


@pytest.mark.asyncio
async def test_discover_root_reads_all_filelist_pages_for_each_directory():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 1
    client = FakeClient(
        [
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "alpha/"}],
                    "nextOffset": "page-2",
                }
            },
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "bravo/"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert [decode_request_body(call)["pointer"] for call in client.calls] == ["", "page-2"]
    assert discovery.prefixes == ["alpha/", "bravo/"]
    assert discovery.root_files == []


@pytest.mark.asyncio
async def test_discover_root_does_not_return_non_root_objects_as_root_files():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    client = FakeClient(
        [
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "alpha/"}],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [{"objectType": "object", "objectKey": "alpha/not-root.txt"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/"]
    assert discovery.root_files == []


@pytest.mark.asyncio
async def test_discover_root_stops_on_repeated_numeric_next_offset():
    scanner, application, bucket = make_scanner()
    client = RepeatingRootOffsetClient()

    await asyncio.wait_for(scanner._discover_root(application, bucket, client), timeout=1)

    assert len(client.calls) == 2


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
async def test_collect_root_files_processes_all_files_with_bounded_workers(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_concurrency_per_bucket = 2
    client = ConcurrentFakeClient(
        [
            {"result": {"objectKey": {"objectKey": f"root-{index}.txt", "size": str(index), "lastModifyTime": "1000"}}}
            for index in range(5)
        ]
    )

    await scanner._collect_root_files(
        application,
        bucket,
        "http://bucket-endpoint",
        [f"root-{index}.txt" for index in range(5)],
        tmp_path,
        client,
    )

    rows = list(csv.DictReader((tmp_path / "root_files.csv").open(newline="", encoding="utf-8")))
    assert sorted(row["object_key"] for row in rows) == [f"root-{index}.txt" for index in range(5)]
    assert client.max_active <= 2


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


@pytest.mark.asyncio
async def test_collect_prefixes_processes_all_prefixes_with_bounded_workers(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.per_bucket_prefix_concurrency = 2
    prefixes = [f"prefix-{index}/" for index in range(5)]
    client = ConcurrentFakeClient(
        [
            {
                "result": {
                    "objectkeys": [
                        {"objectKey": f"{prefix}file.txt", "size": "5", "lastModifyTime": "2000"},
                    ],
                    "truncated": "False",
                }
            }
            for prefix in prefixes
        ]
    )

    await scanner._collect_prefixes(application, bucket, "http://bucket-endpoint", prefixes, tmp_path, client)

    for prefix in prefixes:
        rows = list(csv.DictReader((tmp_path / prefix_temp_filename(prefix)).open(newline="", encoding="utf-8")))
        assert rows == [{"object_key": f"{prefix}file.txt", "size_bytes": "5", "last_modified_ms": "2000"}]
    assert client.max_active <= 2


@pytest.mark.asyncio
async def test_scan_bucket_writes_header_only_csv_for_empty_bucket_and_logs_elapsed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {"result": "http://bucket-endpoint/"},
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        result = await scanner._scan_bucket(
            application,
            bucket,
            client,
            "run-1",
            tmp_path,
            scan_started_ms=1000,
        )

    assert result.status == ScanStatus.SUCCESS
    assert result.csv_path == tmp_path / application.appid / f"{bucket.name}.csv"
    with result.csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows[0][0:3] == ["run_id", "appid", "bucket_name"]
    assert len(rows) == 1
    finish_messages = [
        record.getMessage()
        for record in caplog.records
        if f"bucket finish appid={application.appid} bucket={bucket.name}" in record.getMessage()
    ]
    assert len(finish_messages) == 1
    assert "status=success" in finish_messages[0]
    assert "elapsed_seconds=" in finish_messages[0]


def test_bucket_manifest_temp_dir_cleanup_and_retention(tmp_path: Path):
    scanner, _, bucket = make_scanner()
    thresholds = scanner.config.defaults

    success_delete_dir = tmp_path / "success-delete"
    success_delete_dir.mkdir()
    success_delete_result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=ScanStatus.SUCCESS,
        csv_path=tmp_path / "bucket.csv",
        thresholds=thresholds,
    )
    manifest = scanner._bucket_result_to_manifest(success_delete_result, success_delete_dir)
    assert not success_delete_dir.exists()
    assert "temp_dir" not in manifest

    scanner.config.scan.keep_temp_files = True
    success_keep_dir = tmp_path / "success-keep"
    success_keep_dir.mkdir()
    manifest = scanner._bucket_result_to_manifest(success_delete_result, success_keep_dir)
    assert success_keep_dir.exists()
    assert manifest["temp_dir"] == str(success_keep_dir)

    scanner.config.scan.keep_temp_files = False
    failed_keep_dir = tmp_path / "failed-keep"
    failed_keep_dir.mkdir()
    failed_result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=ScanStatus.FAILED,
        csv_path=None,
        thresholds=thresholds,
        error="boom",
    )
    manifest = scanner._bucket_result_to_manifest(failed_result, failed_keep_dir)
    assert failed_keep_dir.exists()
    assert manifest["temp_dir"] == str(failed_keep_dir)
