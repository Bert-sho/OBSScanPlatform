import asyncio
import base64
import csv
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from obs_scan_platform import scanner as scanner_module
from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.models import BucketInfo, BucketScanResult, ScanStatus
from obs_scan_platform.obs_client import OBSRequestError
from obs_scan_platform.scan_coordination import ScanPhaseCoordinator
from obs_scan_platform.scanner import Scanner


def _decode_base64_json(value: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(value.encode("utf-8")).decode("utf-8"))


def _decode_base64_text(value: str) -> str:
    return base64.urlsafe_b64decode(value.encode("utf-8")).decode("utf-8")


class DummyAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class BarrierHTTPHarness:
    def __init__(
        self,
        buckets_by_app: dict[str, list[str]],
        *,
        paged_bucket: str,
        ready_buckets: set[str],
        failing_aggregations: set[str] | None = None,
    ) -> None:
        self.buckets_by_app = buckets_by_app
        self.paged_bucket = paged_bucket
        self.ready_buckets = ready_buckets
        self.failing_aggregations = failing_aggregations or set()
        self.active_page_started = asyncio.Event()
        self.release_active_page = asyncio.Event()
        self.next_page_admitted = asyncio.Event()
        self.aggregation_waiters: asyncio.Queue[None] = asyncio.Queue()
        self.events: list[str] = []

    async def send(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/rest/s3/listbuckets"):
            appid = params["appid"]
            return self._response(
                request,
                {
                    "buckets": [
                        {
                            "id": f"{bucket_name}-id",
                            "name": bucket_name,
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "owner",
                            "shareFrom": None,
                        }
                        for bucket_name in self.buckets_by_app[appid]
                    ]
                },
            )
        if path.endswith("/rest/s3/bucket/endpoint"):
            return self._response(request, f"https://{params['bucketid']}.example")
        if path.endswith("/rest/s3/bucket/filelist"):
            request_body = _decode_base64_json(params["requestbody"])
            bucket_name = str(request_body["id"]).removesuffix("-id")
            return self._response(
                request,
                {
                    "files": [{"objectType": "folder", "objectKey": f"{bucket_name}/"}],
                    "nextOffset": "",
                },
            )
        if path.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            bucket_name = params["bucketid"]
            marker = params["nextmarker"]
            if bucket_name == self.paged_bucket and not marker:
                self.active_page_started.set()
                await self.release_active_page.wait()
                return self._response(
                    request,
                    {
                        "objectkeys": [
                            {
                                "objectKey": f"{bucket_name}/current.txt",
                                "size": "5",
                                "lastModifyTime": "2000",
                            }
                        ],
                        "truncated": "true",
                        "nextmarker": "page-2",
                    },
                )
            if bucket_name == self.paged_bucket:
                self.events.append("next-request-admitted")
                self.next_page_admitted.set()
                return self._response(
                    request,
                    {"objectkeys": [], "truncated": "false"},
                )
            if bucket_name in self.ready_buckets:
                await self.active_page_started.wait()
                return self._response(
                    request,
                    {"objectkeys": [], "truncated": "false"},
                )
        raise AssertionError(f"unexpected request: {request.url}")

    @staticmethod
    def _response(request: httpx.Request, result: Any) -> httpx.Response:
        return httpx.Response(200, json={"result": result}, request=request)


def _barrier_config(tmp_path: Path, buckets_by_app: dict[str, list[str]]) -> AppConfigFile:
    config = AppConfigFile(
        endpoint="https://global-obs-api.example",
        defaults=Thresholds(
            large_directory_bytes=10,
            large_file_bytes=10,
            inactive_directory_days=30,
            filelist_depth=1,
        ),
        applications=[
            ApplicationConfig(
                appid=appid,
                name=appid,
                apptoken=f"token-{index}",
            )
            for index, appid in enumerate(buckets_by_app, start=1)
        ],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.overview_format = "csv"
    config.scan.keep_temp_files = True
    config.scan.bucket_concurrency = 10
    config.scan.global_request_concurrency = 10
    config.scan.objectkeys_concurrency_per_bucket = 1
    config.scan.metadata_concurrency_per_bucket = 1
    config.scan.max_retries = 0
    return config


def _install_barrier_harness(
    monkeypatch: pytest.MonkeyPatch,
    harness: BarrierHTTPHarness,
) -> None:
    real_coordinator_type = ScanPhaseCoordinator
    real_async_client_type = httpx.AsyncClient

    def make_coordinator(max_concurrent_requests: int) -> ScanPhaseCoordinator:
        coordinator = real_coordinator_type(max_concurrent_requests)
        real_aggregation = coordinator.aggregation

        @asynccontextmanager
        async def instrumented_aggregation():
            context = real_aggregation()
            entry_started = asyncio.Event()

            async def enter_real_scope() -> None:
                entry_started.set()
                await context.__aenter__()

            entry_task = asyncio.create_task(enter_real_scope())
            await entry_started.wait()
            harness.aggregation_waiters.put_nowait(None)
            await entry_task
            try:
                yield
            finally:
                await context.__aexit__(None, None, None)

        coordinator.aggregation = instrumented_aggregation
        return coordinator

    original_append_object_rows = scanner_module.append_object_rows

    def recording_append_object_rows(path: Path, rows) -> None:
        rows = list(rows)
        original_append_object_rows(path, rows)
        if path.parent.name == harness.paged_bucket and rows:
            harness.events.append("page-appended")

    def recording_aggregate_bucket(**kwargs: Any) -> int:
        bucket_name = kwargs["bucket_name"]
        harness.events.append(f"aggregation:{bucket_name}")
        if bucket_name in harness.failing_aggregations:
            raise RuntimeError(f"aggregation failed for {bucket_name}")
        return 0

    monkeypatch.setattr(scanner_module, "ScanPhaseCoordinator", make_coordinator)
    monkeypatch.setattr(
        scanner_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client_type(transport=httpx.MockTransport(harness.send)),
    )
    monkeypatch.setattr(scanner_module, "append_object_rows", recording_append_object_rows)
    monkeypatch.setattr(scanner_module, "aggregate_bucket", recording_aggregate_bucket)


async def _run_barrier_scan(
    scanner: Scanner,
    harness: BarrierHTTPHarness,
    *,
    run_id: str,
    waiting_aggregations: int,
) -> dict[str, Any]:
    scan_task = asyncio.create_task(scanner.run(run_id=run_id))
    await asyncio.wait_for(harness.active_page_started.wait(), timeout=2)
    for _ in range(waiting_aggregations):
        await asyncio.wait_for(harness.aggregation_waiters.get(), timeout=2)
    assert not harness.next_page_admitted.is_set()
    harness.release_active_page.set()
    return await asyncio.wait_for(scan_task, timeout=5)


class FakeOBSClient:
    instances: list["FakeOBSClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.phase_coordinator = kwargs["phase_coordinator"]
        FakeOBSClient.instances.append(self)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
        endpoint: str = "unknown",
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params, "headers": headers})

        if url.endswith("/rest/s3/listbuckets"):
            return {
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
                    ]
                }
            }

        if url.endswith("/rest/s3/bucket/endpoint"):
            assert params["bucketid"] == "owned-bucket"
            assert params["bucketUid"] == "owned-id"
            return {"result": "https://owned-bucket.example/"}

        if url.endswith("/rest/s3/bucket/filelist"):
            request_body = _decode_base64_json(params["requestbody"])
            assert request_body["id"] == "owned-id"
            if request_body["path"] == "/alpha/beta/":
                return {"result": {"files": [], "nextOffset": ""}}
            if request_body["path"] == "/alpha/":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "alpha/beta/"},
                            {"objectType": "object", "objectKey": "alpha/direct.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            assert request_body["path"] == "/"
            return {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/nested/"},
                        {"objectType": "object", "objectKey": "root.txt"},
                    ],
                    "nextOffset": "",
                }
            }

        if url.endswith("/rest/boto3/s3/object/metadata"):
            assert params["bucketid"] == "owned-bucket"
            assert params["bucketId"] == "owned-id"
            assert "bucketld" not in params
            object_key = _decode_base64_text(params["objectkey"]).lstrip("/")
            assert object_key in {"root.txt", "alpha/direct.txt"}
            return {
                "result": {
                    "objectKey": {
                        "objectKey": object_key,
                        "size": "12" if object_key == "root.txt" else "5",
                        "lastModifyTime": "1000" if object_key == "root.txt" else "2000",
                    }
                }
            }

        if url.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            assert params["bucketid"] == "owned-bucket"
            assert params["bucketId"] == "owned-id"
            assert "bucketld" not in params
            prefix = _decode_base64_text(params["objectkey"])
            if prefix == "/alpha/beta/":
                return {
                    "result": {
                        "objectkeys": [
                            {"objectKey": "alpha/beta/child.txt", "size": "7", "lastModifyTime": "3000"},
                        ],
                        "truncated": "false",
                    }
                }
            assert prefix == "/alpha/"
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "alpha/direct.txt", "size": "5", "lastModifyTime": "2000"},
                        {"objectKey": "alpha/beta/child.txt", "size": "7", "lastModifyTime": "3000"},
                    ],
                    "truncated": "false",
                }
            }

        raise AssertionError(f"unexpected OBS URL: {url}")


class EmptyFolderOBSClient(FakeOBSClient):
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
        endpoint: str = "unknown",
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params, "headers": headers})

        if url.endswith("/rest/s3/listbuckets"):
            return {
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
                    ]
                }
            }

        if url.endswith("/rest/s3/bucket/endpoint"):
            return {"result": "https://owned-bucket.example/"}

        if url.endswith("/rest/s3/bucket/filelist"):
            request_body = _decode_base64_json(params["requestbody"])
            if request_body["path"] == "/empty/":
                return {"result": {"files": [], "nextOffset": ""}}
            assert request_body["path"] == "/"
            return {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": "empty/"}],
                    "nextOffset": "",
                }
            }

        if url.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            assert _decode_base64_text(params["objectkey"]) == "/empty/"
            return {"result": {"objectkeys": [], "truncated": "false"}}

        raise AssertionError(f"unexpected OBS URL: {url}")


class SharedBucketOBSClient(FakeOBSClient):
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
        endpoint: str = "unknown",
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params, "headers": headers})

        if url.endswith("/rest/s3/listbuckets"):
            return {
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
                            "id": "reader-shared-id",
                            "name": "reader-shared-bucket",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "reader",
                            "shareFrom": "other-app",
                        },
                    ]
                }
            }

        if url.endswith("/rest/s3/bucket/endpoint"):
            if params["bucketid"] == "owned-bucket":
                assert params["bucketUid"] == "owned-id"
                return {"result": "https://owned-bucket.example/"}
            assert params["bucketid"] == "reader-shared-bucket"
            assert params["bucketUid"] == "reader-shared-id"
            return {"result": "https://reader-shared-bucket.example/"}

        if url.endswith("/rest/s3/bucket/filelist"):
            request_body = _decode_base64_json(params["requestbody"])
            if request_body["id"] == "owned-id":
                return {"result": {"files": [], "nextOffset": ""}}
            assert request_body["id"] == "reader-shared-id"
            if request_body["path"] == "/":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "reader-shared-prefix/"},
                        ],
                        "nextOffset": "",
                    }
                }
            assert request_body["path"] == "/reader-shared-prefix/"
            return {"result": {"files": [], "nextOffset": ""}}

        if url.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            objectkey = _decode_base64_text(params["objectkey"])
            if params["bucketid"] == "owned-bucket":
                assert params["bucketId"] == "owned-id"
                assert "bucketld" not in params
                assert objectkey == "/owned-prefix/"
                return {
                    "result": {
                        "objectkeys": [
                            {"objectKey": "owned-prefix/child.txt", "size": "5", "lastModifyTime": "2000"},
                        ],
                        "truncated": "false",
                    }
                }
            assert params["bucketid"] == "reader-shared-bucket"
            assert params["bucketId"] == "reader-shared-id"
            assert "bucketld" not in params
            assert objectkey == "/reader-shared-prefix/"
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "reader-shared-prefix/child.txt", "size": "5", "lastModifyTime": "2000"},
                    ],
                    "truncated": "false",
                }
            }

        raise AssertionError(f"unexpected OBS URL: {url}")


class PartialFailureOBSClient(FakeOBSClient):
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
        endpoint: str = "unknown",
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params, "headers": headers})

        if url.endswith("/rest/s3/listbuckets"):
            return {
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
                    ]
                }
            }

        if url.endswith("/rest/s3/bucket/endpoint"):
            return {"result": "https://owned-bucket.example/"}

        if url.endswith("/rest/s3/bucket/filelist"):
            request_body = _decode_base64_json(params["requestbody"])
            if request_body["path"] == "/":
                return {
                    "result": {
                        "files": [
                            {"objectType": "folder", "objectKey": "good/"},
                            {"objectType": "folder", "objectKey": "bad/"},
                            {"objectType": "folder", "objectKey": "paged/"},
                            {"objectType": "object", "objectKey": "good-metadata.txt"},
                            {"objectType": "object", "objectKey": "failed-only.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            if request_body["path"] == "/bad/":
                raise OBSRequestError(
                    endpoint="filelist",
                    status_code=503,
                    reason="directory unavailable",
                    url="https://global-obs-api.example/rest/s3/bucket/filelist?token=test-token",
                    response_body='{"success":false,"msg":"directory unavailable"}',
                    response_body_truncated=False,
                    response_body_original_chars=47,
                    exception_type="OBSBusinessError",
                    attempts=4,
                )
            prefix = request_body["path"].strip("/")
            return {
                "result": {
                    "files": [{"objectType": "folder", "objectKey": f"{prefix}/leaf/"}],
                    "nextOffset": "",
                }
            }

        if url.endswith("/rest/boto3/s3/object/metadata"):
            object_key = _decode_base64_text(params["objectkey"]).lstrip("/")
            if object_key == "failed-only.txt":
                raise OBSRequestError(
                    endpoint="metadata",
                    status_code=404,
                    reason="object missing",
                    url="https://owned-bucket.example/rest/boto3/s3/object/metadata?token=test-token",
                    response_body='{"success":false,"msg":"object missing"}',
                    response_body_truncated=False,
                    response_body_original_chars=40,
                    exception_type="HTTPStatusError",
                    attempts=1,
                )
            assert object_key == "good-metadata.txt"
            return {
                "result": {
                    "objectKey": {
                        "objectKey": "good-metadata.txt",
                        "size": "12",
                        "lastModifyTime": "1000",
                    }
                }
            }

        if url.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            prefix = _decode_base64_text(params["objectkey"]).lstrip("/")
            if prefix == "paged/leaf/" and params["nextmarker"] == "page-2":
                raise OBSRequestError(
                    endpoint="objectkeys",
                    status_code=503,
                    reason="next page unavailable",
                    url="https://owned-bucket.example/rest/boto3/s3/list/bucket/objectkeys?token=test-token",
                    response_body='{"success":false,"msg":"next page unavailable"}',
                    response_body_truncated=False,
                    response_body_original_chars=49,
                    exception_type="OBSBusinessError",
                    attempts=4,
                )
            if prefix == "paged/leaf/":
                return {
                    "result": {
                        "objectkeys": [
                            {"objectKey": "paged/kept-first-page.txt", "size": "7", "lastModifyTime": "3000"}
                        ],
                        "truncated": "true",
                        "nextmarker": "page-2",
                    }
                }
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": f"{prefix}success.txt", "size": "5", "lastModifyTime": "2000"},
                    ],
                    "truncated": "false",
                }
            }

        raise AssertionError(f"unexpected OBS URL: {url}")


@pytest.mark.asyncio
async def test_scanner_run_completes_with_mocked_obs_and_directory_csv(tmp_path: Path, monkeypatch):
    FakeOBSClient.instances.clear()
    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("obs_scan_platform.scanner.OBSClient", FakeOBSClient)

    config = AppConfigFile(
        endpoint="https://global-obs-api.example",
        defaults=Thresholds(
            large_directory_bytes=10,
            large_file_bytes=10,
            inactive_directory_days=30,
            filelist_depth=2,
        ),
        applications=[
            ApplicationConfig(
                appid="app.one",
                name="App One",
                apptoken="token-1",
            ),
            ApplicationConfig(
                appid="app.two",
                name="App Two",
                apptoken="token-2",
            ),
        ],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.overview_format = "csv"
    config.scan.keep_temp_files = False
    config.scan.bucket_concurrency = 1
    config.scan.objectkeys_concurrency_per_bucket = 1
    config.scan.metadata_concurrency_per_bucket = 1

    scanner = Scanner(config)
    manifest = await scanner.run(run_id="run-1")

    fake_client = FakeOBSClient.instances[0]
    assert len(FakeOBSClient.instances) == len(config.applications)
    assert len({id(client.phase_coordinator) for client in FakeOBSClient.instances}) == 1
    called_urls = [call["url"] for call in fake_client.calls]
    assert any(url.endswith("/rest/s3/listbuckets") for url in called_urls)
    assert any(url.endswith("/rest/s3/bucket/endpoint") for url in called_urls)
    assert any(url.endswith("/rest/s3/bucket/filelist") for url in called_urls)
    assert any(url.endswith("/rest/boto3/s3/object/metadata") for url in called_urls)
    assert any(url.endswith("/rest/boto3/s3/list/bucket/objectkeys") for url in called_urls)
    assert any(url.startswith("https://global-obs-api.example/") for url in called_urls)
    assert all(call["params"].get("bucketid") != "shared-bucket" for call in fake_client.calls)
    objectkey_calls = [call for call in fake_client.calls if call["url"].endswith("/rest/boto3/s3/list/bucket/objectkeys")]
    assert [_decode_base64_text(call["params"]["objectkey"]) for call in objectkey_calls] == [
        "/alpha/beta/",
    ]

    csv_path = tmp_path / "results" / "run-1" / "app.one" / "owned-bucket.csv"
    assert manifest["status"] == "success"
    bucket_manifest = manifest["applications"][0]["buckets"][0]
    assert bucket_manifest["bucket_name"] == "owned-bucket"
    assert bucket_manifest["bucket_id"] == "owned-id"
    assert bucket_manifest["status"] == "success"
    assert bucket_manifest["csv_path"] == str(csv_path)
    assert bucket_manifest["thresholds"] == {
        "large_directory_bytes": 10,
        "large_file_bytes": 10,
        "inactive_directory_days": 30,
        "filelist_depth": 2,
    }
    assert bucket_manifest["error"] is None
    assert bucket_manifest["errors"] == []
    assert bucket_manifest["started_ms"] <= bucket_manifest["ended_ms"]
    assert bucket_manifest["started_at"].endswith("Z")
    assert bucket_manifest["ended_at"].endswith("Z")
    assert bucket_manifest["elapsed_seconds"] >= 0
    assert bucket_manifest["request_elapsed_seconds"] >= 0
    assert bucket_manifest["processing_elapsed_seconds"] >= 0
    assert bucket_manifest["elapsed_seconds"] == pytest.approx(
        bucket_manifest["request_elapsed_seconds"] + bucket_manifest["processing_elapsed_seconds"]
    )
    assert csv_path.exists()

    rows = {row["directory_path"]: row for row in csv.DictReader(csv_path.open(newline="", encoding="utf-8"))}
    assert sorted(rows) == ["/", "/alpha/", "/alpha/beta/"]
    assert rows["/"]["object_count"] == "3"
    assert rows["/"]["total_size_bytes"] == "24"
    assert rows["/"]["max_file_size_bytes"] == "12"
    assert rows["/alpha/"]["object_count"] == "2"
    assert rows["/alpha/"]["total_size_bytes"] == "12"
    assert rows["/alpha/beta/"]["object_count"] == "1"
    assert rows["/alpha/beta/"]["total_size_bytes"] == "7"

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "object_key" not in csv_text
    assert "root.txt" not in csv_text
    assert "alpha/direct.txt" not in csv_text
    assert not (tmp_path / "results" / "run-1" / "_tmp" / "app.one" / "owned-bucket").exists()

    manifest_path = tmp_path / "results" / "run-1" / "manifest.json"
    persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted_manifest["status"] == "success"
    assert persisted_manifest["applications"][0]["buckets"][0]["csv_path"] == str(csv_path)


@pytest.mark.asyncio
async def test_concurrent_runs_on_one_scanner_use_distinct_run_coordinators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    FakeOBSClient.instances.clear()
    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("obs_scan_platform.scanner.OBSClient", FakeOBSClient)
    config = _barrier_config(tmp_path, {"app.one": ["one"], "app.two": ["two"]})
    scanner = Scanner(config)
    both_runs_started = asyncio.Event()
    entered_run_ids: set[str] = set()
    bucket_coordinators: list[tuple[str, FakeOBSClient, object | None]] = []

    async def fake_list_buckets(application, client):
        del client
        return [BucketInfo(f"{application.appid}-id", application.appid, "HEC", "cn-east-3", "owner", None)]

    async def recording_scan_bucket(
        application,
        bucket,
        client,
        run_id,
        results_dir,
        scan_started_ms,
        *,
        phase_coordinator=None,
    ):
        del results_dir, scan_started_ms
        bucket_coordinators.append((run_id, client, phase_coordinator))
        entered_run_ids.add(run_id)
        if len(entered_run_ids) == 2:
            both_runs_started.set()
        await both_runs_started.wait()
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=None,
            thresholds=config.defaults,
        )

    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)
    monkeypatch.setattr(scanner, "_scan_bucket", recording_scan_bucket)

    await asyncio.wait_for(
        asyncio.gather(
            scanner.run(run_id="run-a"),
            scanner.run(run_id="run-b"),
        ),
        timeout=5,
    )

    coordinator_ids_by_run: dict[str, set[int]] = {}
    for run_id in ("run-a", "run-b"):
        run_records = [record for record in bucket_coordinators if record[0] == run_id]
        assert len({id(client) for _, client, _ in run_records}) == len(config.applications)
        assert all(coordinator is client.phase_coordinator for _, client, coordinator in run_records)
        coordinator_ids_by_run[run_id] = {
            id(client.phase_coordinator)
            for _, client, _ in run_records
        }
        assert len(coordinator_ids_by_run[run_id]) == 1

    assert coordinator_ids_by_run["run-a"].isdisjoint(coordinator_ids_by_run["run-b"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("buckets_by_app", "ready_buckets", "failing_aggregations"),
    [
        (
            {"app.one": ["ready-bucket", "paged-bucket"]},
            {"ready-bucket"},
            set(),
        ),
        (
            {"app.one": ["ready-one"], "app.two": ["ready-two", "paged-bucket"]},
            {"ready-one", "ready-two"},
            set(),
        ),
        (
            {"app.one": ["failing-bucket", "paged-bucket"]},
            {"failing-bucket"},
            {"failing-bucket"},
        ),
    ],
    ids=["page-before-aggregation", "cross-application-writers", "failure-reopens-admission"],
)
async def test_scanner_composed_barrier_orders_processing_aggregations_and_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    buckets_by_app: dict[str, list[str]],
    ready_buckets: set[str],
    failing_aggregations: set[str],
):
    harness = BarrierHTTPHarness(
        buckets_by_app,
        paged_bucket="paged-bucket",
        ready_buckets=ready_buckets,
        failing_aggregations=failing_aggregations,
    )
    _install_barrier_harness(monkeypatch, harness)
    scanner = Scanner(_barrier_config(tmp_path, buckets_by_app))

    manifest = await _run_barrier_scan(
        scanner,
        harness,
        run_id="composed-barrier",
        waiting_aggregations=len(ready_buckets),
    )

    ready_event_count = len(ready_buckets)
    assert harness.events[0] == "page-appended"
    assert set(harness.events[1 : ready_event_count + 1]) == {
        f"aggregation:{bucket_name}" for bucket_name in ready_buckets
    }
    assert harness.events[ready_event_count + 1 :] == [
        "next-request-admitted",
        "aggregation:paged-bucket",
    ]
    assert manifest["status"] == ("partial_failed" if failing_aggregations else "success")
    if failing_aggregations:
        buckets = {
            bucket["bucket_name"]: bucket
            for application in manifest["applications"]
            for bucket in application["buckets"]
        }
        assert buckets["failing-bucket"]["error"] == "aggregation failed for failing-bucket"
        assert buckets["paged-bucket"]["status"] == "success"


@pytest.mark.asyncio
async def test_scanner_run_marks_bucket_partial_failed_and_keeps_csv(tmp_path: Path, monkeypatch):
    FakeOBSClient.instances.clear()
    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("obs_scan_platform.scanner.OBSClient", PartialFailureOBSClient)

    config = AppConfigFile(
        endpoint="https://global-obs-api.example",
        defaults=Thresholds(
            large_directory_bytes=10,
            large_file_bytes=10,
            inactive_directory_days=30,
            filelist_depth=2,
        ),
        applications=[ApplicationConfig(appid="app.one", name="App One", apptoken="token-1")],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.overview_format = "csv"
    config.scan.keep_temp_files = True
    config.scan.bucket_concurrency = 1
    config.scan.objectkeys_concurrency_per_bucket = 1

    manifest = await Scanner(config).run(run_id="run-1")

    csv_path = tmp_path / "results" / "run-1" / "app.one" / "owned-bucket.csv"
    bucket = manifest["applications"][0]["buckets"][0]
    assert manifest["status"] == "partial_failed"
    assert manifest["applications"][0]["status"] == "partial_failed"
    assert bucket["status"] == "partial_failed"
    assert bucket["csv_path"] == str(csv_path)
    assert bucket["error"].startswith("3 request failures;")
    assert len(bucket["errors"]) == 3
    errors_by_endpoint = {error["endpoint"]: error for error in bucket["errors"]}
    assert errors_by_endpoint == {
        "filelist": {
            "endpoint": "filelist",
            "scope": "directory",
            "scope_value": "/bad/",
            "url": "https://global-obs-api.example/rest/s3/bucket/filelist?token=test-token",
            "status_code": 503,
            "reason": "directory unavailable",
            "response_body": '{"success":false,"msg":"directory unavailable"}',
            "response_body_truncated": False,
            "response_body_original_chars": 47,
            "exception_type": "OBSBusinessError",
            "attempts": 4,
        },
        "metadata": {
            "endpoint": "metadata",
            "scope": "object_key",
            "scope_value": "failed-only.txt",
            "url": "https://owned-bucket.example/rest/boto3/s3/object/metadata?token=test-token",
            "status_code": 404,
            "reason": "object missing",
            "response_body": '{"success":false,"msg":"object missing"}',
            "response_body_truncated": False,
            "response_body_original_chars": 40,
            "exception_type": "HTTPStatusError",
            "attempts": 1,
        },
        "objectkeys": {
            "endpoint": "objectkeys",
            "scope": "prefix",
            "scope_value": "paged/leaf/",
            "url": "https://owned-bucket.example/rest/boto3/s3/list/bucket/objectkeys?token=test-token",
            "status_code": 503,
            "reason": "next page unavailable",
            "response_body": '{"success":false,"msg":"next page unavailable"}',
            "response_body_truncated": False,
            "response_body_original_chars": 49,
            "exception_type": "OBSBusinessError",
            "attempts": 4,
        },
    }
    assert bucket["partial_errors"]["filelist_failed_dirs"] == 1
    assert bucket["partial_errors"]["metadata_failed_files"] == 1
    assert bucket["partial_errors"]["objectkeys_failed_prefixes"] == 1
    assert bucket["started_ms"] <= bucket["ended_ms"]
    assert bucket["started_at"].endswith("Z")
    assert bucket["ended_at"].endswith("Z")
    assert bucket["elapsed_seconds"] >= 0
    assert csv_path.exists()

    persisted = json.loads((tmp_path / "results" / "run-1" / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["applications"][0]["buckets"][0] == bucket

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "good-metadata.txt" not in csv_text  # CSV schema remains directory-level only.
    assert "failed-only.txt" not in csv_text
    rows = {row["directory_path"]: row for row in csv.DictReader(csv_text.splitlines())}
    assert rows["/"]["object_count"] == "4"
    assert rows["/good/"]["object_count"] == "1"
    assert rows["/paged/"]["object_count"] == "1"


@pytest.mark.asyncio
async def test_scanner_run_succeeds_with_empty_folder_and_header_only_csv(tmp_path: Path, monkeypatch):
    FakeOBSClient.instances.clear()
    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("obs_scan_platform.scanner.OBSClient", EmptyFolderOBSClient)

    config = AppConfigFile(
        endpoint="https://global-obs-api.example",
        defaults=Thresholds(
            large_directory_bytes=10,
            large_file_bytes=10,
            inactive_directory_days=30,
        ),
        applications=[
            ApplicationConfig(
                appid="app.one",
                name="App One",
                apptoken="token-1",
            )
        ],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.overview_format = "csv"
    config.scan.keep_temp_files = False
    config.scan.bucket_concurrency = 1
    config.scan.objectkeys_concurrency_per_bucket = 1
    config.scan.metadata_concurrency_per_bucket = 1

    manifest = await Scanner(config).run(run_id="run-1")

    csv_path = tmp_path / "results" / "run-1" / "app.one" / "owned-bucket.csv"
    assert manifest["status"] == "success"
    assert manifest["applications"][0]["buckets"][0]["status"] == "success"
    assert manifest["applications"][0]["buckets"][0]["csv_path"] == str(csv_path)
    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows[0][0:3] == ["run_id", "appid", "bucket_name"]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_scanner_run_includes_non_owner_shared_bucket_when_enabled(tmp_path: Path, monkeypatch):
    FakeOBSClient.instances.clear()
    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("obs_scan_platform.scanner.OBSClient", SharedBucketOBSClient)

    config = AppConfigFile(
        endpoint="https://global-obs-api.example",
        defaults=Thresholds(
            large_directory_bytes=10,
            large_file_bytes=10,
            inactive_directory_days=30,
            filelist_depth=1,
        ),
        applications=[
            ApplicationConfig(
                appid="app.one",
                name="App One",
                apptoken="token-1",
                scan_shared_buckets=True,
            )
        ],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.keep_temp_files = True
    config.scan.bucket_concurrency = 1

    manifest = await Scanner(config).run(run_id="run-1")

    assert manifest["status"] == "success"
    assert [bucket["bucket_name"] for bucket in manifest["applications"][0]["buckets"]] == [
        "owned-bucket",
        "reader-shared-bucket",
    ]

    fake_client = FakeOBSClient.instances[0]
    endpoint_calls = [call for call in fake_client.calls if call["url"].endswith("/rest/s3/bucket/endpoint")]
    filelist_calls = [call for call in fake_client.calls if call["url"].endswith("/rest/s3/bucket/filelist")]
    objectkeys_calls = [
        call for call in fake_client.calls if call["url"].endswith("/rest/boto3/s3/list/bucket/objectkeys")
    ]
    assert any(call["params"]["bucketid"] == "reader-shared-bucket" for call in endpoint_calls)
    assert any(
        _decode_base64_json(call["params"]["requestbody"])["id"] == "reader-shared-id"
        for call in filelist_calls
    )
    shared_objectkeys_call = next(
        call for call in objectkeys_calls if call["params"]["bucketid"] == "reader-shared-bucket"
    )
    assert shared_objectkeys_call["params"]["bucketId"] == "reader-shared-id"
    assert "bucketld" not in shared_objectkeys_call["params"]
