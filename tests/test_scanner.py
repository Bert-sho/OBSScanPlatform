import asyncio
import csv
import inspect
from pathlib import Path

import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.models import BucketInfo, BucketScanResult, ScanStatus
from obs_scan_platform.paths import prefix_temp_filename
from obs_scan_platform.scanner import Scanner, _rollup_status, is_owned_bucket, parse_int_or_none, run_scan


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
            }
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
