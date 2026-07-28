import base64
import csv
import json
from pathlib import Path
from typing import Any

import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
from obs_scan_platform.obs_client import OBSRequestError
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


class FakeOBSClient:
    instances: list["FakeOBSClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.calls: list[dict[str, Any]] = []
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
            )
        ],
    )
    config.scan.results_dir = str(tmp_path / "results")
    config.scan.keep_temp_files = False
    config.scan.bucket_concurrency = 1
    config.scan.objectkeys_concurrency_per_bucket = 1
    config.scan.metadata_concurrency_per_bucket = 1

    manifest = await Scanner(config).run(run_id="run-1")

    fake_client = FakeOBSClient.instances[0]
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
