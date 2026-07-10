import asyncio
import base64
import csv
import json
import inspect
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.logging_config import configure_logging
from obs_scan_platform.models import BucketInfo, BucketScanResult, PartialErrorSummary, ScanStatus
from obs_scan_platform.obs_client import OBSClient, OBSRequestError
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

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


class ConcurrentFakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
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

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        await asyncio.sleep(0)
        self.calls.append({"url": url, "params": params, "headers": headers})
        return {"result": {"files": [], "nextOffset": 1}}


class FailingChildFilelistClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        request_body = decode_request_body({"params": params})
        if request_body["path"] == "/":
            return {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "bravo/"},
                    ],
                    "nextOffset": "",
                }
            }
        if request_body["path"] == "/alpha/":
            raise RuntimeError("child filelist failed")
        return {"result": {"files": [{"objectType": "folder", "objectKey": "bravo/child/"}], "nextOffset": ""}}


class FailingRootFilelistClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        request_body = decode_request_body({"params": params})
        assert request_body["path"] == "/"
        raise RuntimeError("root filelist failed")


class PhaseOrderClient:
    def __init__(self):
        self.phase_events: list[str] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.phase_events.append(endpoint)
        if endpoint == "bucket_endpoint":
            return {"result": "http://bucket-endpoint/"}
        if endpoint == "filelist":
            request_body = decode_request_body({"params": params})
            if request_body["path"] == "/":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "alpha/"},
                            {"objectType": "object", "objectKey": "root.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            return {"result": {"files": [], "nextOffset": ""}}
        if endpoint == "metadata":
            assert "objectkeys" not in self.phase_events
            return {"result": {"objectKey": {"objectKey": "root.txt", "size": "12", "lastModifyTime": "1000"}}}
        if endpoint == "objectkeys":
            metadata_index = self.phase_events.index("metadata")
            objectkeys_index = len(self.phase_events) - 1
            assert metadata_index < objectkeys_index
            return {"result": {"objectkeys": [], "truncated": "false"}}
        raise AssertionError(endpoint)


class DummyProgressBar:
    def __init__(self):
        self.total = 0
        self.updates: list[int] = []
        self.refreshes = 0
        self.closed = False

    def update(self, count: int) -> None:
        self.updates.append(count)

    def refresh(self) -> None:
        self.refreshes += 1

    def close(self) -> None:
        self.closed = True


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


def test_should_scan_bucket_includes_scan_capable_shared_buckets_when_enabled():
    owned = BucketInfo("1", "a", "HEC", "cn-east-3", "owner", None)
    owner_shared = BucketInfo("2", "b", "HEC", "cn-east-3", "owner", "other")
    reader_shared = BucketInfo("3", "c", "HEC", "cn-east-3", "reader", "other")
    missing_vendor = BucketInfo("4", "d", "", "cn-east-3", "reader", "other")

    assert should_scan_bucket(owned, include_shared=False)
    assert not should_scan_bucket(owner_shared, include_shared=False)
    assert not should_scan_bucket(reader_shared, include_shared=False)
    assert should_scan_bucket(owned, include_shared=True)
    assert should_scan_bucket(owner_shared, include_shared=True)
    assert should_scan_bucket(reader_shared, include_shared=True)
    assert not should_scan_bucket(missing_vendor, include_shared=True)


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

    assert [bucket.name for bucket in buckets] == ["owned-bucket", "shared-bucket", "reader-bucket"]
    assert client.calls[0]["url"].startswith("http://global-obs.example/")


@pytest.mark.asyncio
async def test_list_buckets_includes_shared_bucket_lists_when_enabled():
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
                        }
                    ],
                    "sharedBuckets": [
                        {
                            "id": "shared-id",
                            "name": "shared-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "reader",
                            "shareFrom": "other-app",
                        }
                    ],
                }
            }
        ]
    )

    buckets = await scanner._list_buckets(application, client)

    assert [bucket.name for bucket in buckets] == ["owned-bucket", "shared-bucket"]


@pytest.mark.asyncio
async def test_list_buckets_logs_skip_for_missing_required_shared_bucket(caplog: pytest.LogCaptureFixture):
    scanner, application, _ = make_scanner()
    application.scan_shared_buckets = True
    client = FakeClient(
        [
            {
                "result": {
                    "buckets": [
                        {
                            "id": "reader-id",
                            "name": "reader-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "reader",
                            "shareFrom": "other",
                        },
                        {
                            "id": "bad-id",
                            "name": "missing-region",
                            "vendor": "HEC",
                            "region": "",
                            "auth": "reader",
                            "shareFrom": "other",
                        },
                    ]
                }
            }
        ]
    )

    with caplog.at_level(logging.WARNING, logger="obs_scan_platform.scanner"):
        buckets = await scanner._list_buckets(application, client)

    assert [bucket.name for bucket in buckets] == ["reader-bucket"]
    messages = [record.getMessage() for record in caplog.records]
    assert any("bucket skipped appid=app.one bucket=missing-region reason=missing_required_fields" in msg for msg in messages)
    assert all("token" not in msg.lower() for msg in messages)


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
async def test_discover_root_parses_documented_objects_key():
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {
                "success": True,
                "files": [],
                "objects": [
                    {"objectType": "folder", "objectKey": "alpha/nested/"},
                    {"objectType": "object", "objectKey": "root.txt"},
                ],
                "nextOffset": "",
            },
            {"success": True, "files": [], "objects": [], "nextOffset": ""},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

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
async def test_discover_root_records_child_filelist_failure_and_continues():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 3
    partial_errors = PartialErrorSummary()
    client = FailingChildFilelistClient()

    discovery = await scanner._discover_root(application, bucket, client, partial_errors=partial_errors)

    assert discovery.prefixes == ["bravo/"]
    assert partial_errors.to_manifest()["filelist_failed_dirs"] == 1
    assert partial_errors.to_manifest()["samples"][0]["target"] == "/alpha/"
    requested_paths = [decode_request_body(call)["path"] for call in client.calls]
    assert "/alpha/" in requested_paths
    assert "/bravo/" in requested_paths
    assert "/alpha/child/" not in requested_paths


@pytest.mark.asyncio
async def test_discover_root_propagates_root_filelist_failure():
    scanner, application, bucket = make_scanner()
    partial_errors = PartialErrorSummary()

    with pytest.raises(RuntimeError, match="root filelist failed"):
        await scanner._discover_root(
            application,
            bucket,
            FailingRootFilelistClient(),
            partial_errors=partial_errors,
        )

    assert not partial_errors.has_errors()


@pytest.mark.asyncio
async def test_discover_root_logs_filelist_progress(caplog: pytest.LogCaptureFixture):
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
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._discover_root(application, bucket, client)

    progress_messages = [
        record.getMessage()
        for record in caplog.records
        if "filelist progress appid=app.one bucket=bucket-name-1" in record.getMessage()
    ]
    assert progress_messages == [
        "filelist progress appid=app.one bucket=bucket-name-1 completed=1 total=2",
        "filelist progress appid=app.one bucket=bucket-name-1 completed=2 total=2",
    ]


@pytest.mark.asyncio
async def test_discover_root_progress_total_excludes_discarded_next_level(caplog: pytest.LogCaptureFixture):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.filelist_task_limit_per_bucket = 1
    client = FakeClient(
        [
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "alpha/"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._discover_root(application, bucket, client)

    progress_messages = [
        record.getMessage()
        for record in caplog.records
        if "filelist progress appid=app.one bucket=bucket-name-1" in record.getMessage()
    ]
    assert progress_messages == [
        "filelist progress appid=app.one bucket=bucket-name-1 completed=1 total=1",
    ]


@pytest.mark.asyncio
async def test_discover_root_updates_progress_bar_when_enabled(monkeypatch: pytest.MonkeyPatch):
    scanner, application, bucket = make_scanner()
    scanner.show_progress = True
    progress_bar = DummyProgressBar()
    monkeypatch.setattr(scanner, "_filelist_progress_bar", lambda app, bucket_info, total: progress_bar)
    client = FakeClient([{"result": {"files": [], "nextOffset": ""}}])

    await scanner._discover_root(application, bucket, client)

    assert progress_bar.total == 1
    assert progress_bar.updates == [1]
    assert progress_bar.closed is True


@pytest.mark.asyncio
async def test_discover_root_progress_bar_total_excludes_discarded_next_level(monkeypatch: pytest.MonkeyPatch):
    scanner, application, bucket = make_scanner()
    scanner.show_progress = True
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.filelist_task_limit_per_bucket = 1
    progress_bar = DummyProgressBar()
    monkeypatch.setattr(scanner, "_filelist_progress_bar", lambda app, bucket_info, total: progress_bar)
    client = FakeClient(
        [
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "alpha/"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    await scanner._discover_root(application, bucket, client)

    assert progress_bar.total == 1
    assert progress_bar.updates == [1]
    assert progress_bar.closed is True


@pytest.mark.asyncio
async def test_discover_root_does_not_create_progress_bar_by_default(monkeypatch: pytest.MonkeyPatch):
    scanner, application, bucket = make_scanner()

    def fail_progress_bar(app, bucket_info, total):
        raise AssertionError("progress bar should not be created")

    monkeypatch.setattr(scanner, "_filelist_progress_bar", fail_progress_bar)
    client = FakeClient([{"result": {"files": [], "nextOffset": ""}}])

    await scanner._discover_root(application, bucket, client)


@pytest.mark.asyncio
async def test_discover_root_processes_whole_level_even_when_it_exceeds_task_limit():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 5
    scanner.config.scan.filelist_task_limit_per_bucket = 3
    level_two_folders = [f"dir-{index}/" for index in range(5)]
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": folder}
                        for folder in level_two_folders
                    ],
                    "nextOffset": "",
                }
            },
            *[
                {
                    "result": {
                        "files": [{"objectType": "folder", "objectKey": f"{folder}child/"}],
                        "nextOffset": "",
                    }
                }
                for folder in level_two_folders
            ],
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert [decode_request_body(call)["path"] for call in client.calls] == [
        "/",
        "/dir-0/",
        "/dir-1/",
        "/dir-2/",
        "/dir-3/",
        "/dir-4/",
    ]
    assert discovery.prefixes == [f"dir-{index}/" for index in range(5)]


@pytest.mark.asyncio
async def test_discover_root_schedules_deeper_level_when_current_level_keeps_total_below_limit():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 3
    scanner.config.scan.filelist_task_limit_per_bucket = 10
    client = FakeClient(
        [
            {"result": {"files": [{"objectType": "folder", "objectKey": "alpha/"}], "nextOffset": ""}},
            {"result": {"files": [{"objectType": "folder", "objectKey": "alpha/beta/"}], "nextOffset": ""}},
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert [decode_request_body(call)["path"] for call in client.calls] == ["/", "/alpha/", "/alpha/beta/"]
    assert discovery.prefixes == ["alpha/"]


@pytest.mark.asyncio
async def test_discover_root_returns_metadata_files_not_covered_by_objectkeys_prefixes():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "object", "objectKey": "root.txt"},
                        {"objectType": "folder", "objectKey": "alpha/"},
                    ],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [{"objectType": "object", "objectKey": "alpha/direct.txt"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/"]
    assert discovery.metadata_files == ["root.txt"]


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
async def test_discover_root_processes_same_filelist_level_concurrently():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    client = ConcurrentFakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "bravo/"},
                        {"objectType": "folder", "objectKey": "charlie/"},
                    ],
                    "nextOffset": "",
                }
            },
            {"result": {"files": [], "nextOffset": ""}},
            {"result": {"files": [], "nextOffset": ""}},
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    await scanner._discover_root(application, bucket, client)

    assert client.max_active > 1
    assert sorted(decode_request_body(call)["path"] for call in client.calls) == [
        "/",
        "/alpha/",
        "/bravo/",
        "/charlie/",
    ]


@pytest.mark.asyncio
async def test_discover_root_stops_on_repeated_numeric_next_offset():
    scanner, application, bucket = make_scanner()
    client = RepeatingRootOffsetClient()

    await asyncio.wait_for(scanner._discover_root(application, bucket, client), timeout=1)

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_scan_bucket_treats_empty_filelist_objects_as_empty_bucket(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {"result": "http://bucket-endpoint/"},
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "empty/"}],
                    "nextOffset": "",
                }
            },
            {"result": {"objects": {}, "nextOffset": ""}},
            {"result": {"objectkeys": [], "truncated": "false"}},
        ]
    )

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.SUCCESS
    assert [call["url"].rsplit("/", 1)[-1] for call in client.calls] == ["endpoint", "filelist", "filelist"]


def test_configure_logging_suppresses_httpx_request_url_logs(tmp_path: Path):
    configure_logging(tmp_path / "scan.log")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_configure_logging_filters_httpx_request_urls_even_after_level_reset(tmp_path: Path):
    log_path = tmp_path / "scan.log"
    configure_logging(log_path)
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)

    httpx_logger.info('HTTP Request: GET http://obs.example/secret?token=secret-token "HTTP/1.1 200 OK"')
    logging.getLogger("obs_scan_platform.scanner").info("bucket finish appid=app.one bucket=bucket-a")

    log_text = log_path.read_text(encoding="utf-8")
    assert "bucket finish appid=app.one bucket=bucket-a" in log_text
    assert "HTTP Request" not in log_text
    assert "obs.example" not in log_text
    assert "secret-token" not in log_text


@pytest.mark.asyncio
async def test_discover_root_progress_logs_do_not_include_request_urls(caplog: pytest.LogCaptureFixture):
    scanner, application, bucket = make_scanner()
    client = FakeClient([{"result": {"files": [], "nextOffset": ""}}])

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._discover_root(application, bucket, client)

    messages = [record.getMessage() for record in caplog.records]
    assert any("filelist progress appid=app.one bucket=bucket-name-1" in message for message in messages)
    assert all("http://global-obs.example" not in message for message in messages)
    assert all("requestbody" not in message for message in messages)
    assert all(application.apptoken not in message for message in messages)


@pytest.mark.asyncio
async def test_scan_shared_bucket_treats_empty_objectkeys_success_false_as_empty(
    tmp_path: Path,
):
    scanner, application, _ = make_scanner()
    application.scan_shared_buckets = True
    scanner.config.defaults.filelist_depth = 1
    bucket = BucketInfo("shared-id", "shared-bucket", "HEC", "cn-east-3", "reader", "other-app")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/s3/bucket/endpoint"):
            return httpx.Response(200, json={"success": True, "result": "http://bucket-endpoint/"})
        if request.url.path.endswith("/rest/s3/bucket/filelist"):
            request_body = decode_request_body({"params": dict(request.url.params)})
            assert request_body["id"] == "shared-id"
            assert request_body["path"] == "/"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "files": [{"objectType": "folder", "objectKey": "shared-prefix/"}],
                        "nextOffset": "",
                    },
                },
            )
        if request.url.path.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            assert request.url.params["bucketid"] == "shared-bucket"
            assert request.url.params["bucketld"] == "shared-id"
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "objectKeys": [],
                    "truncated": "false",
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        request_semaphore=asyncio.Semaphore(10),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )
    try:
        result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)
    finally:
        await client.close()

    assert result.status == ScanStatus.SUCCESS
    assert result.csv_path == tmp_path / application.appid / "shared-bucket.csv"
    with result.csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows[0][0:3] == ["run_id", "appid", "bucket_name"]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_scan_application_keeps_other_buckets_after_unexpected_bucket_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, _ = make_scanner()
    bad_bucket = BucketInfo("bad-id", "bad-bucket", "HEC", "cn-east-3", "owner", None)
    good_bucket = BucketInfo("good-id", "good-bucket", "HEC", "cn-east-3", "owner", None)

    async def fake_list_buckets(app: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del app, client
        return [bad_bucket, good_bucket]

    async def fake_scan_bucket(
        app: ApplicationConfig,
        bucket: BucketInfo,
        client: Any,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> BucketScanResult:
        del app, client, run_id, results_dir, scan_started_ms
        if bucket.name == "bad-bucket":
            raise RuntimeError("unexpected bucket boom")
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=tmp_path / application.appid / f"{bucket.name}.csv",
            thresholds=scanner.config.defaults,
        )

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", fake_scan_bucket)

    result = await scanner._scan_application(application, "run-1", tmp_path, scan_started_ms=1000)

    assert result["status"] == ScanStatus.PARTIAL_FAILED.value
    assert [bucket["bucket_name"] for bucket in result["buckets"]] == ["bad-bucket", "good-bucket"]
    assert result["buckets"][0]["status"] == ScanStatus.FAILED.value
    assert result["buckets"][0]["error"] == "unexpected bucket boom"
    assert result["buckets"][1]["status"] == ScanStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_collect_metadata_files_uses_bucket_name_as_bucketid_and_writes_csv(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    client = FakeClient([{"result": {"objectKey": {"objectKey": "root.txt", "size": "12", "lastModifyTime": "1000"}}}])

    await scanner._collect_metadata_files(
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
    rows = list(csv.DictReader((tmp_path / "metadata_files.csv").open(newline="", encoding="utf-8")))
    assert rows == [{"object_key": "root.txt", "size_bytes": "12", "last_modified_ms": "1000"}]


@pytest.mark.asyncio
async def test_collect_metadata_files_processes_all_files_with_bounded_workers(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_concurrency_per_bucket = 2
    client = ConcurrentFakeClient(
        [
            {"result": {"objectKey": {"objectKey": f"root-{index}.txt", "size": str(index), "lastModifyTime": "1000"}}}
            for index in range(5)
        ]
    )

    await scanner._collect_metadata_files(
        application,
        bucket,
        "http://bucket-endpoint",
        [f"root-{index}.txt" for index in range(5)],
        tmp_path,
        client,
    )

    rows = list(csv.DictReader((tmp_path / "metadata_files.csv").open(newline="", encoding="utf-8")))
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
    scanner.config.scan.objectkeys_concurrency_per_bucket = 2
    scanner.config.scan.per_bucket_prefix_concurrency = 5
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
async def test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1
    client = PhaseOrderClient()

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.SUCCESS
    assert client.phase_events == ["bucket_endpoint", "filelist", "filelist", "metadata", "objectkeys"]


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


def test_partial_error_summary_counts_and_caps_samples():
    summary = PartialErrorSummary(sample_limit=2)

    summary.record("filelist", "/alpha/", "first failure")
    summary.record("metadata", "root.txt", "second failure")
    summary.record("objectkeys", "logs/", "third failure")

    assert summary.has_errors()
    assert summary.to_manifest() == {
        "filelist_failed_dirs": 1,
        "metadata_failed_files": 1,
        "objectkeys_failed_prefixes": 1,
        "samples": [
            {
                "endpoint": "filelist",
                "target": "/alpha/",
                "status": None,
                "reason": "first failure",
            },
            {
                "endpoint": "metadata",
                "target": "root.txt",
                "status": None,
                "reason": "second failure",
            },
        ],
    }


def test_partial_error_summary_redacts_urls_and_credential_query_text():
    summary = PartialErrorSummary(sample_limit=2)

    summary.record(
        "metadata",
        "root.txt",
        OBSRequestError(
            endpoint="metadata",
            status_code=503,
            reason="upstream failed for https://obs.example/path?token=secret-token&user=alice",
        ),
    )
    summary.record(
        "objectkeys",
        "logs/",
        "download failed for http://obs.example/archive?access_token=abc123&bucket=demo",
    )

    samples = summary.to_manifest()["samples"]
    assert len(samples) == 2
    assert all("http://obs.example" not in sample["reason"] for sample in samples)
    assert all("https://obs.example" not in sample["reason"] for sample in samples)
    assert all("secret-token" not in sample["reason"] for sample in samples)
    assert all("abc123" not in sample["reason"] for sample in samples)
    assert all("token=" not in sample["reason"] for sample in samples)
    assert all("access_token=" not in sample["reason"] for sample in samples)


def test_bucket_manifest_includes_partial_errors_and_keeps_error_empty(tmp_path: Path):
    scanner, _, bucket = make_scanner()
    partial_errors = PartialErrorSummary()
    partial_errors.record("objectkeys", "alpha/", "busy")
    result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=ScanStatus.PARTIAL_FAILED,
        csv_path=tmp_path / "bucket.csv",
        thresholds=scanner.config.defaults,
        partial_errors=partial_errors,
    )

    manifest = scanner._bucket_result_to_manifest(result, tmp_path / "missing-temp")

    assert manifest["status"] == "partial_failed"
    assert manifest["error"] is None
    assert manifest["partial_errors"] == {
        "filelist_failed_dirs": 0,
        "metadata_failed_files": 0,
        "objectkeys_failed_prefixes": 1,
        "samples": [
            {
                "endpoint": "objectkeys",
                "target": "alpha/",
                "status": None,
                "reason": "busy",
            }
        ],
    }
