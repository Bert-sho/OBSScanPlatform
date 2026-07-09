import base64
import csv
import json
from pathlib import Path
from typing import Any

import pytest

from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds
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
            assert params["bucketld"] == "owned-id"
            assert _decode_base64_text(params["objectkey"]) == "/root.txt"
            return {
                "result": {
                    "objectKey": {
                        "objectKey": "root.txt",
                        "size": "12",
                        "lastModifyTime": "1000",
                    }
                }
            }

        if url.endswith("/rest/boto3/s3/list/bucket/objectkeys"):
            assert params["bucketid"] == "owned-bucket"
            assert params["bucketld"] == "owned-id"
            assert _decode_base64_text(params["objectkey"]) == "/alpha/"
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
    config.scan.app_concurrency = 1
    config.scan.bucket_concurrency = 1
    config.scan.per_bucket_prefix_concurrency = 1
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
    assert [_decode_base64_text(call["params"]["objectkey"]) for call in objectkey_calls] == ["/alpha/"]

    csv_path = tmp_path / "results" / "run-1" / "app.one" / "owned-bucket.csv"
    assert manifest["status"] == "success"
    assert manifest["applications"][0]["buckets"] == [
        {
            "bucket_name": "owned-bucket",
            "bucket_id": "owned-id",
            "status": "success",
            "csv_path": str(csv_path),
            "thresholds": {
                "large_directory_bytes": 10,
                "large_file_bytes": 10,
                "inactive_directory_days": 30,
                "filelist_depth": 5,
            },
            "error": None,
        }
    ]
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
