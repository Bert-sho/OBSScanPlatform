import asyncio
import base64
import csv
import json
import inspect
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
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
            raise detailed_request_error("filelist", "child filelist failed")
        return {"result": {"files": [{"objectType": "folder", "objectKey": "bravo/child/"}], "nextOffset": ""}}


class PaginatedFailingChildFilelistClient:
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
            if request_body["pointer"] == "":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "alpha/child/"},
                            {"objectType": "object", "objectKey": "alpha/page-one.txt"},
                        ],
                        "nextOffset": "page-2",
                    }
                }
            raise detailed_request_error(
                "filelist",
                "child filelist failed for https://obs.example/private/path?token=secret-token&access_token=abc123"
            )
        if request_body["path"] == "/bravo/":
            return {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "bravo/child/"}],
                    "nextOffset": "",
                }
            }
        return {"result": {"files": [], "nextOffset": ""}}


class FailingRootFilelistClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        request_body = decode_request_body({"params": params})
        assert request_body["path"] == "/"
        raise detailed_request_error("filelist", "root filelist failed")


class CancellingFilelistClient:
    def __init__(self) -> None:
        self.blocked_started = asyncio.Event()
        self.cancellation_finished = asyncio.Event()

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        request_body = decode_request_body({"params": params})
        path = request_body["path"]
        if path == "/":
            return {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "bravo/"},
                    ],
                    "nextOffset": "",
                }
            }
        if path == "/alpha/":
            await self.blocked_started.wait()
            raise RuntimeError("filelist parser bug")
        self.blocked_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            self.cancellation_finished.set()
            raise


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


class MetadataLimitRollbackClient:
    def __init__(
        self,
        filelist_results: dict[str | tuple[str, str], dict[str, Any] | Exception],
        filelist_delays: dict[str, float] | None = None,
    ):
        self.filelist_results = filelist_results
        self.filelist_delays = filelist_delays or {}
        self.filelist_completions: list[str] = []
        self.phase_events: list[str] = []
        self.objectkeys_prefixes: list[str] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        del url, headers
        self.phase_events.append(endpoint)
        if endpoint == "bucket_endpoint":
            return {"result": "http://bucket-endpoint/"}
        if endpoint == "filelist":
            request_body = decode_request_body({"params": params})
            path = request_body["path"]
            if delay := self.filelist_delays.get(path):
                await asyncio.sleep(delay)
            self.filelist_completions.append(path)
            page_key = (path, request_body["pointer"])
            result = (
                self.filelist_results[page_key]
                if page_key in self.filelist_results
                else self.filelist_results[path]
            )
            if isinstance(result, Exception):
                raise result
            return {"result": result}
        if endpoint == "metadata":
            raise AssertionError("metadata requests must be skipped after rollback")
        if endpoint == "objectkeys":
            prefix = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8")
            self.objectkeys_prefixes.append(prefix)
            return {"result": {"objectkeys": [], "truncated": "false"}}
        raise AssertionError(endpoint)


class DummyProgressBar:
    def __init__(self):
        self.total = 0
        self.updates: list[int] = []
        self.refreshes = 0
        self.closed = False
        self.postfixes: list[dict[str, int]] = []

    def set_postfix(self, values: dict[str, int], *, refresh: bool = False) -> None:
        self.postfixes.append(dict(values))

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


def second_application() -> ApplicationConfig:
    return ApplicationConfig(
        appid="app.two",
        name="App Two",
        endpoint="http://app-two-obs.example",
        apptoken="token-2",
    )


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
    assert discovery.prefixes == []
    assert discovery.root_files == ["root.txt"]


@pytest.mark.asyncio
async def test_bucket_uses_only_filelist_frontier_as_objectkeys_tasks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    discovery = await scanner._discover_root(
        application,
        bucket,
        FakeClient(
            [
                {
                    "result": {
                        "files": [{"objectType": "folder", "objectKey": "alpha/"}],
                        "nextOffset": "",
                    }
                },
                {
                    "result": {
                        "files": [{"objectType": "folder", "objectKey": "alpha/beta/"}],
                        "nextOffset": "",
                    }
                },
            ]
        ),
    )

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._collect_prefixes(
            application,
            bucket,
            "http://bucket-endpoint",
            discovery.prefixes,
            tmp_path,
            FakeClient(
                [
                    {
                        "result": {
                            "objectkeys": [
                                {"objectKey": "alpha/beta/file.txt", "size": "2", "lastModifyTime": "2000"}
                            ],
                            "truncated": "false",
                        }
                    },
                ]
            ),
        )

    assert not (tmp_path / prefix_temp_filename("alpha/")).exists()
    assert (tmp_path / prefix_temp_filename("alpha/beta/")).exists()
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "objectkeys finish appid=app.one bucket=bucket-name-1 "
        "completed=1 total=1 succeeded=1 failed=0 pages=1 objects=1" in message
        for message in messages
    )


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

    assert discovery.prefixes == []
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

    assert discovery.prefixes == []
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
    assert discovery.prefixes == ["alpha/beta/"]
    assert discovery.metadata_files == ["root.txt", "alpha/ignored.txt"]


@pytest.mark.asyncio
async def test_discover_root_records_child_filelist_failure_and_continues():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 3
    partial_errors = PartialErrorSummary()
    client = FailingChildFilelistClient()

    discovery = await scanner._discover_root(application, bucket, client, partial_errors=partial_errors)

    assert discovery.prefixes == ["alpha/"]
    assert partial_errors.to_manifest()["filelist_failed_dirs"] == 1
    assert partial_errors.to_manifest()["samples"][0]["target"] == "/alpha/"
    requested_paths = [decode_request_body(call)["path"] for call in client.calls]
    assert "/alpha/" in requested_paths
    assert "/bravo/" in requested_paths
    assert "/alpha/child/" not in requested_paths


@pytest.mark.asyncio
async def test_discover_root_preserves_successful_child_filelist_pages_after_later_failure():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 3
    partial_errors = PartialErrorSummary()
    client = PaginatedFailingChildFilelistClient()

    discovery = await scanner._discover_root(application, bucket, client, partial_errors=partial_errors)

    assert discovery.prefixes == ["alpha/"]
    assert "alpha/page-one.txt" not in discovery.metadata_files
    manifest = partial_errors.to_manifest()
    assert manifest["filelist_failed_dirs"] == 1
    assert manifest["samples"][0]["target"] == "/alpha/"
    requested_paths = [decode_request_body(call)["path"] for call in client.calls]
    assert "/alpha/" in requested_paths
    assert "/bravo/" in requested_paths
    assert "/alpha/child/" not in requested_paths
    assert "/bravo/child/" in requested_paths


@pytest.mark.asyncio
async def test_discover_root_records_root_request_failure_and_returns_empty_discovery():
    scanner, application, bucket = make_scanner()
    partial_errors = PartialErrorSummary()

    discovery = await scanner._discover_root(
        application,
        bucket,
        FailingRootFilelistClient(),
        partial_errors=partial_errors,
    )

    assert discovery.prefixes == []
    assert discovery.metadata_files == []
    assert partial_errors.filelist_failed_dirs == 1
    assert partial_errors.errors[0].scope == "directory"
    assert partial_errors.errors[0].scope_value == "/"


@pytest.mark.asyncio
async def test_discover_root_cancels_and_awaits_sibling_tasks_after_unexpected_exception():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    client = CancellingFilelistClient()

    with pytest.raises(RuntimeError, match="filelist parser bug"):
        await scanner._discover_root(application, bucket, client)

    assert client.cancellation_finished.is_set()


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
    assert discovery.prefixes == [f"dir-{index}/child/" for index in range(5)]


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
    assert discovery.prefixes == []


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

    assert discovery.prefixes == []
    assert discovery.metadata_files == ["root.txt", "alpha/direct.txt"]


@pytest.mark.asyncio
async def test_discover_root_allows_metadata_count_equal_to_limit():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 2
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

    assert discovery.prefixes == []
    assert discovery.metadata_files == ["root.txt", "alpha/direct.txt"]


@pytest.mark.asyncio
async def test_discover_root_overflow_uses_root_prefix():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "object", "objectKey": "root-one.txt"},
                        {"objectType": "object", "objectKey": "root-two.txt"},
                        {"objectType": "folder", "objectKey": "alpha/"},
                    ],
                    "nextOffset": "",
                }
            },
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["/"]
    assert discovery.metadata_files == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_discover_root_overflow_restores_previous_whole_level():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 3
    scanner.config.scan.metadata_task_limit_per_bucket = 2
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "object", "objectKey": "root.txt"},
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "bravo/"},
                    ],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [
                        {"objectType": "object", "objectKey": "alpha/one.txt"},
                        {"objectType": "object", "objectKey": "alpha/two.txt"},
                        {"objectType": "folder", "objectKey": "alpha/child/"},
                    ],
                    "nextOffset": "",
                }
            },
            {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "bravo/child/"}],
                    "nextOffset": "",
                }
            },
            {"result": {"files": [], "nextOffset": ""}},
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/", "bravo/"]
    assert discovery.metadata_files == ["root.txt"]
    assert [decode_request_body(call)["path"] for call in client.calls] == ["/", "/alpha/", "/bravo/"]


@pytest.mark.asyncio
async def test_discover_root_rollback_excludes_empty_prefix_and_logs(caplog: pytest.LogCaptureFixture):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "folder", "objectKey": "alpha/"},
                    {"objectType": "folder", "objectKey": "empty/"},
                ],
                "nextOffset": "",
            },
            "/alpha/": {
                "files": [
                    {"objectType": "object", "objectKey": "alpha/one.txt"},
                    {"objectType": "object", "objectKey": "alpha/two.txt"},
                ],
                "nextOffset": "",
            },
            "/empty/": {"files": [], "nextOffset": ""},
        }
    )

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/"]
    assert discovery.metadata_files == []
    assert any(
        record.getMessage()
        == "filelist metadata limit rollback appid=app.one bucket=bucket-name-1 "
        "depth=2 metadata_tasks=2 limit=1 prefixes=1"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_discover_root_empty_prefix_rollback_does_not_expose_covered_direct_files():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "folder", "objectKey": "alpha/"},
                    {"objectType": "folder", "objectKey": "noisy/"},
                    {"objectType": "object", "objectKey": "alpha/a.txt"},
                    {"objectType": "object", "objectKey": "alpha/b.txt"},
                ],
                "nextOffset": "",
            },
            "/alpha/": {"files": [], "nextOffset": ""},
            "/noisy/": {
                "files": [
                    {"objectType": "object", "objectKey": "noisy/one.txt"},
                    {"objectType": "object", "objectKey": "noisy/two.txt"},
                ],
                "nextOffset": "",
            },
        }
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["noisy/"]
    assert discovery.metadata_files == []
    assert len(discovery.metadata_files) <= scanner.config.scan.metadata_task_limit_per_bucket


@pytest.mark.asyncio
async def test_discover_root_metadata_overflow_rollback_is_deterministic_when_level_completes_out_of_order():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "object", "objectKey": "root.txt"},
                    {"objectType": "folder", "objectKey": "alpha/"},
                    {"objectType": "folder", "objectKey": "bravo/"},
                ],
                "nextOffset": "",
            },
            "/alpha/": {
                "files": [{"objectType": "object", "objectKey": "alpha/file.txt"}],
                "nextOffset": "",
            },
            "/bravo/": {
                "files": [{"objectType": "object", "objectKey": "bravo/file.txt"}],
                "nextOffset": "",
            },
        },
        filelist_delays={"/alpha/": 0.02},
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert client.filelist_completions == ["/", "/bravo/", "/alpha/"]
    assert discovery.prefixes == ["alpha/", "bravo/"]
    assert discovery.metadata_files == ["root.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_page",
    [
        {"files": [], "nextOffset": ""},
        {"objects": {}, "nextOffset": ""},
    ],
    ids=["empty-files", "empty-objects-map"],
)
async def test_discover_root_paginated_populated_directory_is_not_empty_on_empty_terminal_page(
    terminal_page: dict[str, Any],
):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "folder", "objectKey": "alpha/"},
                    {"objectType": "folder", "objectKey": "noisy/"},
                ],
                "nextOffset": "",
            },
            ("/alpha/", ""): {
                "files": [{"objectType": "folder", "objectKey": "alpha/child/"}],
                "nextOffset": "page-2",
            },
            ("/alpha/", "page-2"): terminal_page,
            "/noisy/": {
                "files": [
                    {"objectType": "object", "objectKey": "noisy/one.txt"},
                    {"objectType": "object", "objectKey": "noisy/two.txt"},
                ],
                "nextOffset": "",
            },
        }
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/", "noisy/"]
    assert discovery.metadata_files == []


@pytest.mark.asyncio
async def test_scan_bucket_root_overflow_skips_metadata_and_scans_root_prefix(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "object", "objectKey": "root-one.txt"},
                    {"objectType": "object", "objectKey": "root-two.txt"},
                ],
                "nextOffset": "",
            }
        }
    )

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.SUCCESS
    assert client.phase_events == ["bucket_endpoint", "filelist", "objectkeys"]
    assert client.objectkeys_prefixes == ["/"]


@pytest.mark.asyncio
async def test_metadata_limit_rollback_preserves_filelist_failure():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    partial_errors = PartialErrorSummary()
    client = MetadataLimitRollbackClient(
        {
            "/": {
                "files": [
                    {"objectType": "folder", "objectKey": "bad/"},
                    {"objectType": "folder", "objectKey": "noisy/"},
                ],
                "nextOffset": "",
            },
            "/bad/": detailed_request_error("filelist", "bad directory unavailable"),
            "/noisy/": {
                "files": [
                    {"objectType": "object", "objectKey": "noisy/one.txt"},
                    {"objectType": "object", "objectKey": "noisy/two.txt"},
                ],
                "nextOffset": "",
            },
        }
    )

    discovery = await scanner._discover_root(
        application,
        bucket,
        client,
        partial_errors=partial_errors,
    )

    assert discovery.prefixes == ["bad/", "noisy/"]
    assert discovery.metadata_files == []
    assert partial_errors.to_manifest()["filelist_failed_dirs"] == 1


@pytest.mark.asyncio
async def test_discover_root_joins_child_file_names_to_bucket_path():
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
                    "files": [{"objectType": "object", "objectKey": "direct.txt"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == []
    assert discovery.metadata_files == ["alpha/direct.txt"]


@pytest.mark.asyncio
async def test_discover_root_preserves_child_absolute_object_keys():
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
                    "files": [{"objectType": "object", "objectKey": "alpha/direct.txt"}],
                    "nextOffset": "",
                }
            },
        ]
    )

    discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == []
    assert discovery.metadata_files == ["alpha/direct.txt"]


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
async def test_discover_root_returns_non_root_objects_as_metadata_files():
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

    assert discovery.prefixes == []
    assert discovery.metadata_files == ["alpha/not-root.txt"]


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
async def test_discover_root_logs_raw_request_failure_reason(caplog: pytest.LogCaptureFixture):
    scanner, application, bucket = make_scanner()
    partial_errors = PartialErrorSummary()
    client = PaginatedFailingChildFilelistClient()

    with caplog.at_level(logging.WARNING, logger="obs_scan_platform.scanner"):
        await scanner._discover_root(application, bucket, client, partial_errors=partial_errors)

    messages = [record.getMessage() for record in caplog.records]
    assert any("filelist directory failure appid=app.one bucket=bucket-name-1 path=/alpha/" in message for message in messages)
    assert any("https://obs.example/private/path" in message for message in messages)
    assert any("token=secret-token" in message for message in messages)
    assert any("access_token=abc123" in message for message in messages)


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
    monotonic_times = iter([10.0, 12.0, 20.0])
    monkeypatch.setattr("obs_scan_platform.scanner._now_ms", lambda: 1_000)
    monkeypatch.setattr("obs_scan_platform.scanner.time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))
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
        del client, run_id, scan_started_ms
        if bucket.name == "bad-bucket":
            temp_dir = results_dir / scanner.config.scan.temp_subdir / app.appid / bucket.name
            temp_dir.mkdir(parents=True)
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

    result = await scanner._scan_application(
        application,
        "run-1",
        tmp_path,
        scan_started_ms=1000,
        bucket_semaphore=asyncio.Semaphore(scanner.config.scan.bucket_concurrency),
    )

    assert result["status"] == ScanStatus.PARTIAL_FAILED.value
    assert [bucket["bucket_name"] for bucket in result["buckets"]] == ["bad-bucket", "good-bucket"]
    assert result["buckets"][0]["status"] == ScanStatus.FAILED.value
    assert result["buckets"][0]["error"] == "unexpected bucket boom"
    assert result["buckets"][0]["elapsed_seconds"] == 2.0
    assert result["buckets"][0]["request_elapsed_seconds"] == result["buckets"][0]["elapsed_seconds"]
    assert result["buckets"][0]["processing_elapsed_seconds"] == 0.0
    assert result["buckets"][1]["status"] == ScanStatus.SUCCESS.value
    assert not (tmp_path / scanner.config.scan.temp_subdir / application.appid / "bad-bucket").exists()


@pytest.mark.asyncio
async def test_run_keeps_other_applications_after_listbuckets_request_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, bad_application, _ = make_scanner()
    good_application = ApplicationConfig(
        appid="app.good",
        name="Good App",
        endpoint="http://good-obs.example",
        apptoken="token-2",
    )
    scanner.config.applications = [bad_application, good_application]
    scanner.config.scan.results_dir = str(tmp_path)

    async def fake_list_buckets(application: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del client
        if application.appid == "app.one":
            raise detailed_request_error("listbuckets", "list buckets unavailable")
        return []

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)

    manifest = await scanner.run(run_id="run-1")

    assert manifest["status"] == "partial_failed"
    assert manifest["applications"][0]["status"] == "failed"
    assert manifest["applications"][0]["buckets"] == []
    assert manifest["applications"][1]["status"] == "success"


@pytest.mark.asyncio
async def test_run_applies_bucket_concurrency_globally_across_applications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, first_application, _ = make_scanner()
    scanner.config.applications = [first_application, second_application()]
    scanner.config.scan.results_dir = str(tmp_path)
    scanner.config.scan.bucket_concurrency = 1
    active = 0
    peak = 0

    async def fake_list_buckets(application: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del client
        return [
            BucketInfo(
                f"{application.appid}-id",
                f"{application.appid}-bucket",
                "HEC",
                "cn-east-3",
                "owner",
                None,
            )
        ]

    async def fake_scan_bucket(
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: Any,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> BucketScanResult:
        nonlocal active, peak
        del client, run_id, results_dir, scan_started_ms
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=None,
            thresholds=scanner.config.defaults,
        )

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", fake_scan_bucket)

    manifest = await scanner.run(run_id="global-bucket-limit")

    assert manifest["status"] == ScanStatus.SUCCESS.value
    assert peak == 1


@pytest.mark.asyncio
async def test_run_starts_all_application_enumerations_without_app_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, first_application, _ = make_scanner()
    scanner.config = AppConfigFile.model_validate(
        {
            "endpoint": "http://global-obs.example",
            "scan": {"app_concurrency": 1, "results_dir": str(tmp_path)},
            "defaults": scanner.config.defaults.model_dump(),
            "applications": [first_application.model_dump(), second_application().model_dump()],
        }
    )
    active = 0
    peak = 0

    async def fake_list_buckets(application: ApplicationConfig, client: Any) -> list[BucketInfo]:
        nonlocal active, peak
        del application, client
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)

    await scanner.run(run_id="unlimited-apps")

    assert peak == 2


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


class MetadataPartialFailureClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        object_key = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if object_key == "bad.txt":
            raise detailed_request_error(
                "metadata",
                "metadata unavailable for https://obs.example/private/path?token=secret-token&access_token=abc123"
            )
        return {
            "result": {
                "objectKey": {
                    "objectKey": object_key,
                    "size": "12",
                    "lastModifyTime": "1000",
                }
            }
        }


class MetadataProgressClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        del url, headers, endpoint
        object_key = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if object_key == "request-failed.txt":
            raise detailed_request_error("metadata", "metadata unavailable")
        if object_key == "invalid.txt":
            return {"result": {"objectKey": {"objectKey": object_key}}}
        return {
            "result": {
                "objectKey": {
                    "objectKey": object_key,
                    "size": "12",
                    "lastModifyTime": "1000",
                }
            }
        }


class ObjectkeysPartialFailureClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        prefix = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if prefix == "bad/":
            raise detailed_request_error("objectkeys", "objectkeys unavailable")
        return {
            "result": {
                "objectkeys": [
                    {"objectKey": f"{prefix}file.txt", "size": "5", "lastModifyTime": "2000"},
                ],
                "truncated": "false",
            }
        }


class ObjectkeysPaginatedPartialFailureClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        prefix = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        marker = params["nextmarker"]
        if prefix == "partial/" and marker == "page-2":
            raise detailed_request_error("objectkeys", "objectkeys page unavailable")
        if prefix == "partial/":
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "partial/one.txt", "size": "1", "lastModifyTime": "2000"},
                        {"objectKey": "partial/two.txt", "size": "2", "lastModifyTime": "2000"},
                    ],
                    "truncated": "true",
                    "nextmarker": "page-2",
                }
            }
        return {
            "result": {
                "objectkeys": [
                    {"objectKey": "good/file.txt", "size": "3", "lastModifyTime": "2000"},
                ],
                "truncated": "false",
            }
        }


class UnexpectedMetadataClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        object_key = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if object_key == "bad.txt":
            raise RuntimeError("metadata parser bug")
        return {
            "result": {
                "objectKey": {"objectKey": object_key, "size": "3", "lastModifyTime": "2000"}
            }
        }


class UnexpectedObjectkeysClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        raise RuntimeError("objectkeys parser bug")


@pytest.mark.asyncio
async def test_metadata_logs_progress_for_success_failure_and_invalid_response(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_concurrency_per_bucket = 1
    partial_errors = PartialErrorSummary()

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._collect_metadata_files(
            application,
            bucket,
            "http://bucket-endpoint",
            ["good.txt", "request-failed.txt", "invalid.txt"],
            tmp_path,
            MetadataProgressClient(),
            partial_errors=partial_errors,
        )

    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith("metadata ")]
    assert messages == [
        "metadata start appid=app.one bucket=bucket-name-1 total=3",
        "metadata progress appid=app.one bucket=bucket-name-1 completed=1 total=3 succeeded=1 failed=0",
        "metadata object failure appid=app.one bucket=bucket-name-1 object_key=request-failed.txt error=OBS request failed endpoint=metadata status=503 reason=metadata unavailable",
        "metadata progress appid=app.one bucket=bucket-name-1 completed=2 total=3 succeeded=1 failed=1",
        "metadata object failure appid=app.one bucket=bucket-name-1 object_key=invalid.txt error=invalid metadata response",
        "metadata progress appid=app.one bucket=bucket-name-1 completed=3 total=3 succeeded=1 failed=2",
        "metadata finish appid=app.one bucket=bucket-name-1 completed=3 total=3 succeeded=1 failed=2",
    ]
    assert partial_errors.metadata_failed_files == 2
    assert partial_errors.samples[1].reason == "invalid metadata response"


@pytest.mark.asyncio
async def test_metadata_logs_skipped_for_empty_task_list(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._collect_metadata_files(
            application,
            bucket,
            "http://bucket-endpoint",
            [],
            tmp_path,
            FakeClient([]),
        )

    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith("metadata ")]
    assert messages == ["metadata skipped appid=app.one bucket=bucket-name-1 total=0"]


class CancellingWorkerClient:
    def __init__(self, *, failure: str) -> None:
        self.failure = failure
        self.blocked_started = asyncio.Event()
        self.cancellation_finished = asyncio.Event()

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        target = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if target.startswith("bad"):
            await self.blocked_started.wait()
            raise RuntimeError(self.failure)
        self.blocked_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            self.cancellation_finished.set()
            raise


@pytest.mark.asyncio
async def test_metadata_unexpected_exception_is_recorded_and_later_files_continue(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_concurrency_per_bucket = 1
    partial_errors = PartialErrorSummary()

    await scanner._collect_metadata_files(
        application,
        bucket,
        "http://bucket-endpoint",
        ["bad.txt", "good.txt"],
        tmp_path,
        UnexpectedMetadataClient(),
        partial_errors=partial_errors,
    )

    rows = list(csv.DictReader((tmp_path / "metadata_files.csv").open(newline="", encoding="utf-8")))
    assert [row["object_key"] for row in rows] == ["good.txt"]
    assert partial_errors.to_manifest()["metadata_failed_files"] == 1
    assert partial_errors.to_manifest()["samples"][0]["reason"] == "metadata parser bug"


@pytest.mark.asyncio
async def test_objectkeys_unexpected_exception_propagates(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    with pytest.raises(RuntimeError, match="objectkeys parser bug"):
        await scanner._collect_prefixes(
            application,
            bucket,
            "http://bucket-endpoint",
            ["bad/"],
            tmp_path,
            UnexpectedObjectkeysClient(),
            partial_errors=PartialErrorSummary(),
        )


@pytest.mark.asyncio
async def test_objectkeys_cancels_and_awaits_sibling_workers_after_unexpected_exception(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.objectkeys_concurrency_per_bucket = 2
    client = CancellingWorkerClient(failure="objectkeys parser bug")

    with pytest.raises(RuntimeError, match="objectkeys parser bug"):
        await scanner._collect_prefixes(
            application,
            bucket,
            "http://bucket-endpoint",
            ["bad/", "blocked/"],
            tmp_path,
            client,
        )

    assert client.cancellation_finished.is_set()


@pytest.mark.asyncio
async def test_collect_metadata_files_records_failure_and_keeps_other_files(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    partial_errors = PartialErrorSummary()
    client = MetadataPartialFailureClient()

    await scanner._collect_metadata_files(
        application,
        bucket,
        "http://bucket-endpoint",
        ["good.txt", "bad.txt", "also-good.txt"],
        tmp_path,
        client,
        partial_errors=partial_errors,
    )

    rows = list(csv.DictReader((tmp_path / "metadata_files.csv").open(newline="", encoding="utf-8")))
    assert sorted(row["object_key"] for row in rows) == ["also-good.txt", "good.txt"]
    assert partial_errors.to_manifest()["metadata_failed_files"] == 1
    assert partial_errors.to_manifest()["samples"][0]["target"] == "bad.txt"


@pytest.mark.asyncio
async def test_collect_metadata_files_sanitizes_failure_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    scanner, application, bucket = make_scanner()
    partial_errors = PartialErrorSummary()
    client = MetadataPartialFailureClient()

    with caplog.at_level(logging.WARNING, logger="obs_scan_platform.scanner"):
        await scanner._collect_metadata_files(
            application,
            bucket,
            "http://bucket-endpoint",
            ["bad.txt"],
            tmp_path,
            client,
            partial_errors=partial_errors,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("metadata object failure appid=app.one bucket=bucket-name-1 object_key=bad.txt" in message for message in messages)
    assert all("https://obs.example/private/path" not in message for message in messages)
    assert all("secret-token" not in message for message in messages)
    assert all("abc123" not in message for message in messages)
    assert all("token=" not in message for message in messages)
    assert all("access_token=" not in message for message in messages)


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
async def test_collect_prefixes_records_prefix_failure_and_keeps_other_prefixes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1
    partial_errors = PartialErrorSummary()

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._collect_prefixes(
            application,
            bucket,
            "http://bucket-endpoint",
            ["good/", "bad/", "also-good/"],
            tmp_path,
            ObjectkeysPartialFailureClient(),
            partial_errors=partial_errors,
        )

    assert (tmp_path / prefix_temp_filename("good/")).exists()
    assert not (tmp_path / prefix_temp_filename("bad/")).exists()
    assert (tmp_path / prefix_temp_filename("also-good/")).exists()
    assert partial_errors.to_manifest()["objectkeys_failed_prefixes"] == 1
    assert partial_errors.to_manifest()["samples"][0]["target"] == "bad/"
    messages = [record.getMessage() for record in caplog.records]
    assert any("objectkeys start appid=app.one bucket=bucket-name-1 total=3" in message for message in messages)
    assert any("objectkeys prefix failure appid=app.one bucket=bucket-name-1 prefix=bad/" in message for message in messages)
    assert any(
        "objectkeys finish appid=app.one bucket=bucket-name-1 "
        "completed=3 total=3 succeeded=2 failed=1 pages=2 objects=2" in message
        for message in messages
    )
    assert all("http://bucket-endpoint" not in message for message in messages)


@pytest.mark.asyncio
async def test_collect_prefixes_updates_objectkeys_progress_for_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, bucket = make_scanner()
    scanner.show_progress = True
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1
    progress_bar = DummyProgressBar()
    partial_errors = PartialErrorSummary()

    monkeypatch.setattr(scanner, "_objectkeys_progress_bar", lambda app, bucket_info, total: progress_bar)

    await scanner._collect_prefixes(
        application,
        bucket,
        "http://bucket-endpoint",
        ["good/", "bad/"],
        tmp_path,
        ObjectkeysPartialFailureClient(),
        partial_errors=partial_errors,
    )

    assert progress_bar.total == 2
    assert progress_bar.updates == [1, 1]
    assert progress_bar.postfixes[-1] == {
        "succeeded": 1,
        "failed": 1,
        "pages": 1,
        "objects": 1,
    }
    assert progress_bar.closed


@pytest.mark.asyncio
async def test_objectkeys_progress_keeps_successful_pages_from_failed_prefix(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        await scanner._collect_prefixes(
            application,
            bucket,
            "http://bucket-endpoint",
            ["partial/", "good/"],
            tmp_path,
            ObjectkeysPaginatedPartialFailureClient(),
            partial_errors=PartialErrorSummary(),
        )

    rows = []
    for prefix in ("partial/", "good/"):
        rows.extend(csv.DictReader((tmp_path / prefix_temp_filename(prefix)).open(newline="", encoding="utf-8")))
    assert sorted(row["object_key"] for row in rows) == ["good/file.txt", "partial/one.txt", "partial/two.txt"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "objectkeys finish appid=app.one bucket=bucket-name-1 "
        "completed=2 total=2 succeeded=1 failed=1 pages=2 objects=3" in message
        for message in messages
    )


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
    scanner.config.defaults.filelist_depth = 1
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1
    client = PhaseOrderClient()

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.SUCCESS
    assert client.phase_events == ["bucket_endpoint", "filelist", "metadata", "objectkeys"]


class PartialBucketScanClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers, "endpoint": endpoint})
        if endpoint == "bucket_endpoint":
            return {"result": "http://bucket-endpoint/"}
        if endpoint == "filelist":
            request_body = decode_request_body({"params": params})
            if request_body["path"] == "/":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "good/"},
                            {"objectType": "folder", "objectKey": "bad/"},
                            {"objectType": "object", "objectKey": "root.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            return {"result": {"files": [], "nextOffset": ""}}
        if endpoint == "metadata":
            return {"result": {"objectKey": {"objectKey": "root.txt", "size": "12", "lastModifyTime": "1000"}}}
        if endpoint == "objectkeys":
            prefix = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
            if prefix == "bad/":
                raise detailed_request_error("objectkeys", "prefix boom")
            return {
                "result": {
                    "objectkeys": [{"objectKey": "good/file.txt", "size": "5", "lastModifyTime": "2000"}],
                    "truncated": "false",
                }
            }
        raise AssertionError(endpoint)


class UnexpectedMetadataBucketScanClient:
    def __init__(self) -> None:
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
                            {"objectType": "object", "objectKey": "bad.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            return {"result": {"files": [], "nextOffset": ""}}
        if endpoint == "metadata":
            raise RuntimeError("metadata parser bug")
        if endpoint == "objectkeys":
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "alpha/file.txt", "size": "5", "lastModifyTime": "2000"}
                    ],
                    "truncated": "false",
                }
            }
        raise AssertionError(endpoint)


class InvalidMetadataBucketScanClient:
    def __init__(self) -> None:
        self.phase_events: list[str] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        del url, headers
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
                            {"objectType": "object", "objectKey": "invalid.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            return {"result": {"files": [], "nextOffset": ""}}
        if endpoint == "metadata":
            return {"result": {"objectKey": {"objectKey": "invalid.txt"}}}
        if endpoint == "objectkeys":
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "alpha/file.txt", "size": "5", "lastModifyTime": "2000"}
                    ],
                    "truncated": "false",
                }
            }
        raise AssertionError(endpoint)


@pytest.mark.asyncio
async def test_bucket_continues_to_objectkeys_after_metadata_task_failure(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 1
    client = UnexpectedMetadataBucketScanClient()

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.PARTIAL_FAILED
    assert result.error == "1 failures; metadata=1; first: metadata target=bad.txt reason=metadata parser bug"
    assert "objectkeys" in client.phase_events
    assert result.csv_path is not None
    assert result.csv_path.exists()
    assert result.partial_errors is not None
    assert result.partial_errors.to_manifest()["metadata_failed_files"] == 1


@pytest.mark.asyncio
async def test_invalid_metadata_marks_bucket_partial_failed_and_objectkeys_still_runs(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 1
    client = InvalidMetadataBucketScanClient()

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.PARTIAL_FAILED
    assert result.error == (
        "1 failures; metadata=1; first: metadata target=invalid.txt reason=invalid metadata response"
    )
    assert "objectkeys" in client.phase_events
    assert result.csv_path is not None
    assert result.csv_path.exists()
    assert result.partial_errors is not None
    assert result.partial_errors.to_manifest()["metadata_failed_files"] == 1


@pytest.mark.asyncio
async def test_scan_bucket_returns_partial_failed_with_csv_for_objectkeys_failure(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 1
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1

    result = await scanner._scan_bucket(
        application,
        bucket,
        PartialBucketScanClient(),
        "run-1",
        tmp_path,
        scan_started_ms=1000,
    )

    assert result.status == ScanStatus.PARTIAL_FAILED
    assert result.error == "1 request failures; objectkeys=1; first: objectkeys prefix=bad/ status=503 reason=prefix boom"
    assert result.csv_path == tmp_path / application.appid / f"{bucket.name}.csv"
    assert result.csv_path.exists()
    assert result.partial_errors is not None
    assert result.partial_errors.to_manifest()["objectkeys_failed_prefixes"] == 1


class FailingBucketEndpointClient:
    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        raise detailed_request_error("bucket_endpoint", "endpoint unavailable")


@pytest.mark.asyncio
async def test_scan_bucket_endpoint_request_failure_has_detail_and_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, bucket = make_scanner()
    wall_times = iter([2_000, 5_000])
    monotonic_times = iter([20.0, 21.5])
    monkeypatch.setattr("obs_scan_platform.scanner._now_ms", lambda: next(wall_times))
    monkeypatch.setattr("obs_scan_platform.scanner.time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))

    result = await scanner._scan_bucket(
        application,
        bucket,
        FailingBucketEndpointClient(),
        "run-1",
        tmp_path,
        scan_started_ms=500,
    )

    assert result.status == ScanStatus.FAILED
    assert result.csv_path is None
    assert "endpoint unavailable" in result.error
    assert len(result.errors) == 1
    assert result.errors[0].scope == "bucket"
    assert result.errors[0].scope_value == bucket.name
    assert result.started_ms == 2_000
    assert result.ended_ms == 5_000
    assert result.started_at == "1970-01-01T00:00:02.000Z"
    assert result.ended_at == "1970-01-01T00:00:05.000Z"
    assert result.elapsed_seconds == 1.5
    assert result.request_elapsed_seconds == 1.5
    assert result.processing_elapsed_seconds == 0.0


@pytest.mark.asyncio
async def test_bucket_result_records_start_end_and_elapsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scanner, application, bucket = make_scanner()
    wall_times = iter([1_000, 4_000])
    monotonic_times = iter([10.0, 12.0, 13.25])
    monkeypatch.setattr("obs_scan_platform.scanner._now_ms", lambda: next(wall_times))
    monkeypatch.setattr("obs_scan_platform.scanner.time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))

    result = await scanner._scan_bucket(
        application,
        bucket,
        FakeClient([{"result": "http://bucket-endpoint/"}, {"result": {"files": [], "nextOffset": ""}}]),
        "run-1",
        tmp_path,
        scan_started_ms=500,
    )

    assert result.started_ms == 1_000
    assert result.ended_ms == 4_000
    assert result.started_at == "1970-01-01T00:00:01.000Z"
    assert result.ended_at == "1970-01-01T00:00:04.000Z"
    assert result.elapsed_seconds == 3.25
    assert result.request_elapsed_seconds == 2.0
    assert result.processing_elapsed_seconds == 1.25


@pytest.mark.asyncio
async def test_bucket_result_records_request_and_processing_timing_when_processing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, bucket = make_scanner()
    wall_times = iter([1_000, 4_000])
    monotonic_times = iter([30.0, 32.0, 33.0])
    monkeypatch.setattr("obs_scan_platform.scanner._now_ms", lambda: next(wall_times))
    monkeypatch.setattr("obs_scan_platform.scanner.time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))

    def fail_aggregation(**kwargs: Any) -> int:
        del kwargs
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr("obs_scan_platform.scanner.aggregate_bucket", fail_aggregation)

    result = await scanner._scan_bucket(
        application,
        bucket,
        FakeClient([{"result": "http://bucket-endpoint/"}, {"result": {"files": [], "nextOffset": ""}}]),
        "run-1",
        tmp_path,
        scan_started_ms=500,
    )

    assert result.status == ScanStatus.FAILED
    assert result.error == "aggregation failed"
    assert result.elapsed_seconds == 3.0
    assert result.request_elapsed_seconds == 2.0
    assert result.processing_elapsed_seconds == 1.0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ScanStatus.SUCCESS, ScanStatus.PARTIAL_FAILED, ScanStatus.FAILED])
async def test_scan_application_deletes_each_bucket_temp_dir_after_final_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ScanStatus,
):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.keep_temp_files = False
    temp_dir = tmp_path / scanner.config.scan.temp_subdir / application.appid / bucket.name

    async def fake_list_buckets(app: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del app, client
        return [bucket]

    async def fake_scan_bucket(*args: Any, **kwargs: Any) -> BucketScanResult:
        del args, kwargs
        temp_dir.mkdir(parents=True)
        (temp_dir / "objects.csv").write_text("data", encoding="utf-8")
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=status,
            csv_path=None,
            thresholds=scanner.config.defaults,
        )

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", fake_scan_bucket)

    await scanner._scan_application(
        application,
        "run-1",
        tmp_path,
        scan_started_ms=1000,
        bucket_semaphore=asyncio.Semaphore(1),
    )

    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_finished_bucket_temp_dir_is_deleted_before_sibling_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, _ = make_scanner()
    scanner.config.scan.keep_temp_files = False
    first = BucketInfo("first-id", "first", "HEC", "cn-east-3", "owner", None)
    second = BucketInfo("second-id", "second", "HEC", "cn-east-3", "owner", None)
    first_returned = asyncio.Event()
    release_second = asyncio.Event()

    async def fake_list_buckets(app: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del app, client
        return [first, second]

    async def fake_scan_bucket(
        app: ApplicationConfig,
        bucket: BucketInfo,
        client: Any,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> BucketScanResult:
        del client, run_id, scan_started_ms
        temp_dir = results_dir / scanner.config.scan.temp_subdir / app.appid / bucket.name
        temp_dir.mkdir(parents=True)
        if bucket.name == "first":
            first_returned.set()
        else:
            await release_second.wait()
        return BucketScanResult(
            appid=app.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=None,
            thresholds=scanner.config.defaults,
        )

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", fake_scan_bucket)

    application_task = asyncio.create_task(
        scanner._scan_application(
            application,
            "run-1",
            tmp_path,
            scan_started_ms=1000,
            bucket_semaphore=asyncio.Semaphore(2),
        )
    )
    await first_returned.wait()
    first_temp_dir = tmp_path / scanner.config.scan.temp_subdir / application.appid / "first"
    for _ in range(10):
        if not first_temp_dir.exists():
            break
        await asyncio.sleep(0)

    assert not first_temp_dir.exists()
    assert not application_task.done()

    release_second.set()
    await application_task


@pytest.mark.asyncio
async def test_cleanup_failure_returns_failed_bucket_and_waits_for_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, _ = make_scanner()
    scanner.config.scan.keep_temp_files = False
    cleanup_bucket = BucketInfo("cleanup-id", "cleanup", "HEC", "cn-east-3", "owner", None)
    sibling_bucket = BucketInfo("sibling-id", "sibling", "HEC", "cn-east-3", "owner", None)
    sibling_started = asyncio.Event()
    release_sibling = asyncio.Event()

    async def fake_list_buckets(app: ApplicationConfig, client: Any) -> list[BucketInfo]:
        del app, client
        return [cleanup_bucket, sibling_bucket]

    async def fake_scan_bucket(
        app: ApplicationConfig,
        bucket: BucketInfo,
        client: Any,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> BucketScanResult:
        del client, run_id, scan_started_ms
        temp_dir = results_dir / scanner.config.scan.temp_subdir / app.appid / bucket.name
        temp_dir.mkdir(parents=True)
        if bucket.name == "sibling":
            sibling_started.set()
            await release_sibling.wait()
        return BucketScanResult(
            appid=app.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=tmp_path / app.appid / f"{bucket.name}.csv",
            thresholds=scanner.config.defaults,
            error="existing warning" if bucket.name == "cleanup" else None,
            elapsed_seconds=1.25,
            request_elapsed_seconds=0.75,
            processing_elapsed_seconds=0.5,
        )

    real_rmtree = shutil.rmtree

    def fail_cleanup(path: Path) -> None:
        if Path(path).name == "cleanup":
            raise OSError("cleanup denied at https://obs.example/path?token=secret-token")
        real_rmtree(path)

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", fake_scan_bucket)
    monkeypatch.setattr("obs_scan_platform.scanner.shutil.rmtree", fail_cleanup)

    application_task = asyncio.create_task(
        scanner._scan_application(
            application,
            "run-1",
            tmp_path,
            scan_started_ms=1000,
            bucket_semaphore=asyncio.Semaphore(2),
        )
    )
    await sibling_started.wait()
    for _ in range(10):
        if application_task.done():
            break
        await asyncio.sleep(0)

    assert not application_task.done()

    release_sibling.set()
    result = await application_task

    assert result["status"] == ScanStatus.PARTIAL_FAILED.value
    assert [bucket["bucket_name"] for bucket in result["buckets"]] == ["cleanup", "sibling"]
    failed_bucket = result["buckets"][0]
    assert failed_bucket["status"] == ScanStatus.FAILED.value
    assert failed_bucket["csv_path"] == str(tmp_path / application.appid / "cleanup.csv")
    assert failed_bucket["error"] == "existing warning; cleanup failed: cleanup denied at <redacted-url>"
    assert failed_bucket["elapsed_seconds"] == 1.25
    assert failed_bucket["request_elapsed_seconds"] == 0.75
    assert failed_bucket["processing_elapsed_seconds"] == 0.5
    assert "secret-token" not in failed_bucket["error"]
    assert result["buckets"][1]["status"] == ScanStatus.SUCCESS.value


@pytest.mark.parametrize("status", [ScanStatus.SUCCESS, ScanStatus.PARTIAL_FAILED, ScanStatus.FAILED])
def test_bucket_manifest_keeps_temp_dir_for_every_status_when_retention_enabled(
    tmp_path: Path,
    status: ScanStatus,
):
    scanner, _, bucket = make_scanner()
    scanner.config.scan.keep_temp_files = True
    temp_dir = tmp_path / status.value
    temp_dir.mkdir()
    result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=status,
        csv_path=None,
        thresholds=scanner.config.defaults,
    )

    manifest = scanner._bucket_result_to_manifest(result, temp_dir)

    assert temp_dir.exists()
    assert manifest["temp_dir"] == str(temp_dir)


def test_bucket_manifest_does_not_delete_temp_dir_when_retention_disabled(tmp_path: Path):
    scanner, _, bucket = make_scanner()
    scanner.config.scan.keep_temp_files = False
    temp_dir = tmp_path / "existing"
    temp_dir.mkdir()
    result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=ScanStatus.SUCCESS,
        csv_path=None,
        thresholds=scanner.config.defaults,
    )

    manifest = scanner._bucket_result_to_manifest(result, temp_dir)

    assert temp_dir.exists()
    assert "temp_dir" not in manifest


def detailed_request_error(endpoint: str, reason: str) -> OBSRequestError:
    return OBSRequestError(
        endpoint=endpoint,
        status_code=503,
        reason=reason,
        url="https://obs.example/test?token=raw-token",
        response_body='{"success":false,"msg":"busy"}',
        response_body_truncated=False,
        response_body_original_chars=30,
        exception_type="OBSBusinessError",
        attempts=4,
    )


def test_partial_error_summary_counts_and_caps_samples():
    summary = PartialErrorSummary(sample_limit=2)

    summary.record("filelist", "/alpha/", detailed_request_error("filelist", "first failure"))
    summary.record("metadata", "root.txt", detailed_request_error("metadata", "second failure"))
    summary.record("objectkeys", "logs/", detailed_request_error("objectkeys", "third failure"))

    assert summary.has_errors()
    assert summary.to_manifest() == {
        "filelist_failed_dirs": 1,
        "metadata_failed_files": 1,
        "objectkeys_failed_prefixes": 1,
        "samples": [
            {
                "endpoint": "filelist",
                "target": "/alpha/",
                "status": 503,
                "reason": "first failure",
            },
            {
                "endpoint": "metadata",
                "target": "root.txt",
                "status": 503,
                "reason": "second failure",
            },
        ],
    }
    assert len(summary.errors) == 3
    assert summary.errors[2].url == "https://obs.example/test?token=raw-token"
    assert summary.errors[2].response_body == '{"success":false,"msg":"busy"}'


def test_partial_error_summary_redacts_urls_and_credential_query_text():
    summary = PartialErrorSummary(sample_limit=2)

    summary.record(
        "metadata",
        "root.txt",
        detailed_request_error(
            "metadata",
            "upstream failed for https://obs.example/path?token=secret-token&user=alice",
        ),
    )
    summary.record(
        "objectkeys",
        "logs/",
        detailed_request_error(
            "objectkeys",
            "download failed for http://obs.example/archive?access_token=abc123&bucket=demo",
        ),
    )

    samples = summary.to_manifest()["samples"]
    assert len(samples) == 2
    assert all("http://obs.example" not in sample["reason"] for sample in samples)
    assert all("https://obs.example" not in sample["reason"] for sample in samples)
    assert all("secret-token" not in sample["reason"] for sample in samples)
    assert all("abc123" not in sample["reason"] for sample in samples)
    assert all("token=" not in sample["reason"] for sample in samples)
    assert all("access_token=" not in sample["reason"] for sample in samples)
    assert summary.errors[0].url == "https://obs.example/test?token=raw-token"
    assert summary.errors[0].response_body == '{"success":false,"msg":"busy"}'


def test_bucket_manifest_includes_partial_errors_and_keeps_error_empty(tmp_path: Path):
    scanner, _, bucket = make_scanner()
    partial_errors = PartialErrorSummary()
    partial_errors.record("objectkeys", "alpha/", detailed_request_error("objectkeys", "busy"))
    result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=ScanStatus.PARTIAL_FAILED,
        csv_path=tmp_path / "bucket.csv",
        thresholds=scanner.config.defaults,
        error=partial_errors.summary_text(),
        partial_errors=partial_errors,
        errors=list(partial_errors.errors),
        started_ms=1_000,
        ended_ms=4_000,
        started_at="1970-01-01T00:00:01.000Z",
        ended_at="1970-01-01T00:00:04.000Z",
        elapsed_seconds=3.0,
        request_elapsed_seconds=2.5,
        processing_elapsed_seconds=0.5,
    )

    manifest = scanner._bucket_result_to_manifest(result, tmp_path / "missing-temp")

    assert manifest["status"] == "partial_failed"
    assert manifest["error"] == "1 request failures; objectkeys=1; first: objectkeys target=alpha/ status=503 reason=busy"
    assert manifest["errors"][0]["scope"] == "target"
    assert manifest["errors"][0]["scope_value"] == "alpha/"
    assert manifest["started_ms"] == 1_000
    assert manifest["ended_ms"] == 4_000
    assert manifest["started_at"] == "1970-01-01T00:00:01.000Z"
    assert manifest["ended_at"] == "1970-01-01T00:00:04.000Z"
    assert manifest["elapsed_seconds"] == 3.0
    assert manifest["request_elapsed_seconds"] == 2.5
    assert manifest["processing_elapsed_seconds"] == 0.5
    assert manifest["partial_errors"] == {
        "filelist_failed_dirs": 0,
        "metadata_failed_files": 0,
        "objectkeys_failed_prefixes": 1,
        "samples": [
            {
                "endpoint": "objectkeys",
                "target": "alpha/",
                "status": 503,
                "reason": "busy",
            }
        ],
    }
