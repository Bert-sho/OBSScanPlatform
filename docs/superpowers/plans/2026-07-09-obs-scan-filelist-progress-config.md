# OBS Scan Filelist Progress and Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement recursive `filelist` directory discovery with bounded task splitting, CLI `tqdm` progress, concise scan logs, global endpoint config, empty-bucket success handling, and application-level shared bucket scanning.

**Architecture:** Keep the current scanner architecture and add focused behavior in existing modules. Configuration gains global endpoint, shared bucket switch, filelist depth, and task limit; scanner discovery changes from root-only discovery to bounded recursive discovery while preserving `objectkeys` as final object collection. CLI opts into progress display; FastAPI keeps non-interactive logging only.

**Tech Stack:** Python 3.11, asyncio, httpx, Pydantic, Typer, FastAPI, tqdm, pytest, pytest-asyncio.

---

## Scope Check

This plan implements one cohesive scanner behavior update. It touches configuration, scanner orchestration, CLI progress, docs, and tests, but all changes serve the same scan execution path and can be validated through focused tests plus the existing end-to-end mocked OBS test.

## File Structure

- Modify: `pyproject.toml`
  - Add `tqdm` runtime dependency.
- Modify: `config/apps.example.yaml`
  - Move `endpoint` to the top level.
  - Add `scan.filelist_task_limit_per_bucket`.
  - Add `defaults.filelist_depth`.
  - Add `applications[].scan_shared_buckets`.
  - Add sample bucket-level `filelist_depth`.
- Modify: `src/obs_scan_platform/config.py`
  - Add global endpoint and endpoint resolution.
  - Add filelist task limit.
  - Add bucket override model so a bucket can override only `filelist_depth` without repeating thresholds.
  - Add application shared bucket flag.
- Modify: `src/obs_scan_platform/models.py`
  - Add filelist discovery data structures used by scanner and tests.
  - Keep `Thresholds` available for aggregation code.
- Modify: `src/obs_scan_platform/scanner.py`
  - Resolve effective endpoint from config.
  - Include or skip shared buckets based on application config.
  - Replace root-only discovery with bounded recursive `filelist` discovery.
  - Collect metadata for direct files in every expanded directory.
  - Emit filelist progress logs and bucket elapsed time logs.
  - Support optional CLI progress reporter.
  - Treat zero-object buckets as successful header-only CSV outputs.
- Modify: `src/obs_scan_platform/cli.py`
  - Pass `show_progress=True` to `run_scan`.
- Modify: `src/obs_scan_platform/api.py`
  - Keep default scan call non-interactive; no `tqdm`.
- Modify: `README.md`
  - Document new config keys and progress behavior.
- Modify: `docs/scan-start-guide.md`
  - Document new config keys and CLI/FastAPI progress behavior.
- Modify: `tests/test_config.py`
  - Cover config defaults, overrides, endpoint resolution, and shared-bucket default.
- Modify: `tests/test_scanner.py`
  - Cover shared bucket selection, recursive discovery, task limit, metadata collection, empty buckets, and logs.
- Modify: `tests/test_cli.py`
  - Cover CLI passes progress mode.
- Modify: `tests/test_api.py`
  - Cover API keeps progress disabled.
- Modify: `tests/test_scan_end_to_end.py`
  - Update mocked config shape and verify final behavior remains intact.
- Modify: `docs/current-task.md`
  - Record implementation progress and validation.
- Modify: `docs/handoff.md`
  - Record resume instructions and validation.

## Task 1: Configuration Model and Example YAML

**Files:**
- Modify: `src/obs_scan_platform/config.py`
- Modify: `config/apps.example.yaml`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError
from pathlib import Path

from obs_scan_platform.config import load_config


def test_load_config_uses_top_level_endpoint_and_new_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  filelist_task_limit_per_bucket: 77
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
  filelist_depth: 5
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
    enabled: true
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]
    settings = config.thresholds_for(application, "missing-bucket")

    assert config.endpoint_for(application) == "http://obs.global"
    assert config.scan.filelist_task_limit_per_bucket == 77
    assert application.scan_shared_buckets is False
    assert settings.filelist_depth == 5


def test_load_config_falls_back_to_application_endpoint(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    endpoint: http://obs.app
    apptoken: secret-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]

    assert config.endpoint_for(application) == "http://obs.app"
    assert config.thresholds_for(application, "missing-bucket").filelist_depth == 5


def test_load_config_requires_some_endpoint(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="endpoint"):
        load_config(config_file)


def test_bucket_override_can_set_only_filelist_depth(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
  filelist_depth: 5
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
    scan_shared_buckets: true
    buckets:
      bucket-a:
        filelist_depth: 8
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]
    settings = config.thresholds_for(application, "bucket-a")

    assert application.scan_shared_buckets is True
    assert settings.large_directory_bytes == 100
    assert settings.large_file_bytes == 10
    assert settings.inactive_directory_days == 180
    assert settings.filelist_depth == 8
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: FAIL because `endpoint`, `filelist_task_limit_per_bucket`, `scan_shared_buckets`, and bucket-only `filelist_depth` overrides are not implemented.

- [ ] **Step 3: Implement config models**

Update `src/obs_scan_platform/config.py` with these structures:

```python
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class ScanSettings(BaseModel):
    results_dir: str = "results"
    temp_subdir: str = "_tmp"
    keep_temp_files: bool = False
    page_size: int = 1000
    app_concurrency: int = 2
    bucket_concurrency: int = 4
    global_request_concurrency: int = 50
    per_bucket_prefix_concurrency: int = 8
    metadata_concurrency_per_bucket: int = 8
    filelist_task_limit_per_bucket: int = 100
    request_timeout_seconds: int = 30
    max_retries: int = 5
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 60


class Thresholds(BaseModel):
    large_directory_bytes: int
    large_file_bytes: int
    inactive_directory_days: int
    filelist_depth: int = 5


class BucketOverrides(BaseModel):
    large_directory_bytes: int | None = None
    large_file_bytes: int | None = None
    inactive_directory_days: int | None = None
    filelist_depth: int | None = None


class ApplicationConfig(BaseModel):
    appid: str
    name: str
    endpoint: str | None = None
    apptoken: str
    enabled: bool = True
    scan_shared_buckets: bool = False
    buckets: dict[str, BucketOverrides] = Field(default_factory=dict)


class AppConfigFile(BaseModel):
    endpoint: str | None = None
    scan: ScanSettings = Field(default_factory=ScanSettings)
    defaults: Thresholds
    applications: list[ApplicationConfig]
    source_path: Path | None = None

    @model_validator(mode="after")
    def require_endpoint(self) -> "AppConfigFile":
        missing = [application.appid for application in self.applications if self.endpoint is None and application.endpoint is None]
        if missing:
            raise ValueError(f"endpoint is required for applications without application endpoint: {', '.join(missing)}")
        return self

    def endpoint_for(self, application: ApplicationConfig) -> str:
        endpoint = self.endpoint or application.endpoint
        if endpoint is None:
            raise ValueError(f"endpoint is required for application {application.appid}")
        return endpoint

    def enabled_applications(self) -> list[ApplicationConfig]:
        return [application for application in self.applications if application.enabled]

    def thresholds_for(self, application: ApplicationConfig, bucket_name: str) -> Thresholds:
        override = application.buckets.get(bucket_name)
        if override is None:
            return self.defaults
        data = self.defaults.model_dump()
        data.update(override.model_dump(exclude_none=True))
        return Thresholds.model_validate(data)

    def masked_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"source_path"})
        for application in data["applications"]:
            application["apptoken"] = "******"
        return data


def load_config(path: str | Path) -> AppConfigFile:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfigFile.model_validate(raw)
    config.source_path = config_path
    return config
```

- [ ] **Step 4: Update example YAML**

Update `config/apps.example.yaml` to this shape:

```yaml
endpoint: http://example.com

scan:
  results_dir: results
  temp_subdir: _tmp
  keep_temp_files: false
  page_size: 1000
  app_concurrency: 2
  bucket_concurrency: 4
  global_request_concurrency: 50
  per_bucket_prefix_concurrency: 8
  metadata_concurrency_per_bucket: 8
  filelist_task_limit_per_bucket: 100
  request_timeout_seconds: 30
  max_retries: 5
  retry_base_delay_seconds: 2
  retry_max_delay_seconds: 60

defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180
  filelist_depth: 5

applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    enabled: true
    scan_shared_buckets: false
    buckets:
      bucket-1191:
        large_directory_bytes: 214748364800
        large_file_bytes: 21474836480
        inactive_directory_days: 365
        filelist_depth: 8
```

- [ ] **Step 5: Run config tests and commit**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: PASS.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/config.py config/apps.example.yaml tests/test_config.py
/opt/homebrew/bin/git commit -m "feat: add scan configuration controls"
```

## Task 2: Endpoint Resolution and Shared Bucket Selection in Scanner

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`

- [ ] **Step 1: Write failing scanner tests for shared buckets**

Add this test to `tests/test_scanner.py`:

```python
def test_should_scan_bucket_respects_shared_bucket_switch():
    owned = BucketInfo("1", "owned", "HEC", "cn-east-3", "owner", None)
    shared = BucketInfo("2", "shared", "HEC", "cn-east-3", "owner", "other-app")
    reader = BucketInfo("3", "reader", "HEC", "cn-east-3", "reader", None)

    assert is_owned_bucket(owned)
    assert not is_owned_bucket(shared)
    assert not is_owned_bucket(reader)
    assert should_scan_bucket(owned, include_shared=False)
    assert not should_scan_bucket(shared, include_shared=False)
    assert should_scan_bucket(shared, include_shared=True)
    assert not should_scan_bucket(reader, include_shared=True)
```

Update the import in `tests/test_scanner.py`:

```python
from obs_scan_platform.scanner import Scanner, _rollup_status, is_owned_bucket, parse_int_or_none, run_scan, should_scan_bucket
```

- [ ] **Step 2: Write failing endpoint resolution usage test**

Add this async test to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_list_buckets_uses_top_level_endpoint_and_includes_shared_when_enabled():
    scanner, application, _ = make_scanner()
    scanner.config.endpoint = "http://global-obs.example"
    application.scan_shared_buckets = True
    client = FakeClient(
        [
            {
                "result": {
                    "buckets": [
                        {"id": "owned-id", "name": "owned", "vendor": "HEC", "region": "cn-east-3", "auth": "owner"},
                        {
                            "id": "shared-id",
                            "name": "shared",
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "owner",
                            "shareFrom": "other-app",
                        },
                    ]
                }
            }
        ]
    )

    buckets = await scanner._list_buckets(application, client)

    assert [bucket.name for bucket in buckets] == ["owned", "shared"]
    assert client.calls[0]["url"].startswith("http://global-obs.example/")
```

- [ ] **Step 3: Run targeted tests and verify they fail**

Run:

```bash
pytest tests/test_scanner.py::test_should_scan_bucket_respects_shared_bucket_switch tests/test_scanner.py::test_list_buckets_uses_top_level_endpoint_and_includes_shared_when_enabled -v
```

Expected: FAIL because `should_scan_bucket` and `_list_buckets` do not exist yet.

- [ ] **Step 4: Implement endpoint and shared bucket selection**

In `src/obs_scan_platform/scanner.py`, add:

```python
def should_scan_bucket(bucket: BucketInfo, *, include_shared: bool) -> bool:
    if bucket.auth != "owner":
        return False
    return include_shared or bucket.share_from is None
```

Replace `_list_owned_buckets` with:

```python
    async def _list_buckets(self, application: ApplicationConfig, client: OBSClient) -> list[BucketInfo]:
        data = await client.get_json(
            _endpoint(self.config.endpoint_for(application), "/rest/s3/listbuckets"),
            params={"appid": application.appid},
            headers={**JSON_HEADERS, "csb-token": application.apptoken},
        )
        buckets: list[BucketInfo] = []
        for item in _items_from_payload(_result_payload(data), "buckets", "bucketList", "list"):
            bucket = BucketInfo(
                bucket_id=_string_or_empty(item.get("id")),
                name=_string_or_empty(item.get("name")),
                vendor=_string_or_empty(item.get("vendor")),
                region=_string_or_empty(item.get("region")),
                auth=item.get("auth"),
                share_from=item.get("shareFrom"),
            )
            if should_scan_bucket(bucket, include_shared=application.scan_shared_buckets):
                buckets.append(bucket)
        return buckets
```

In `_scan_application`, replace:

```python
buckets = await self._list_owned_buckets(application, client)
```

with:

```python
buckets = await self._list_buckets(application, client)
```

In `_get_bucket_endpoint` and discovery code, use:

```python
self.config.endpoint_for(application)
```

instead of `application.endpoint` for global OBS API calls.

- [ ] **Step 5: Update existing tests that call `_list_owned_buckets`**

If any tests reference `_list_owned_buckets`, rename them to `_list_buckets` and keep the assertions for the default skip-shared behavior.

Update `tests/test_scan_end_to_end.py` config construction to set either:

```python
endpoint="https://obs-api.example"
```

on `AppConfigFile`, or keep application-level endpoint for compatibility. Prefer top-level endpoint in this test.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: PASS.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
/opt/homebrew/bin/git commit -m "feat: support global endpoint and shared bucket switch"
```

## Task 3: Recursive Filelist Discovery

**Files:**
- Modify: `src/obs_scan_platform/models.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`

- [ ] **Step 1: Add failing model expectations**

Modify `src/obs_scan_platform/models.py` only after writing scanner tests. The tests should first expect a richer discovery object.

Add this test to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_discover_filelist_recurses_to_depth_and_uses_final_prefixes():
    scanner, application, bucket = make_scanner()
    scanner.config.scan.filelist_task_limit_per_bucket = 10
    scanner.config.defaults.filelist_depth = 1
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "object", "objectKey": "root.txt"},
                    ]
                }
            },
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/nested/"},
                        {"objectType": "object", "objectKey": "alpha/direct.txt"},
                    ]
                }
            },
        ]
    )

    discovery = await scanner._discover_filelist(application, bucket, client)

    assert discovery.metadata_files == ["root.txt", "alpha/direct.txt"]
    assert discovery.prefixes == ["alpha/nested/"]
    assert discovery.filelist_completed == 2
    assert discovery.filelist_total == 2
    paths = [call["params"]["requestbody"] for call in client.calls]
    assert len(paths) == 2
```

- [ ] **Step 2: Add failing task-limit test**

Add:

```python
@pytest.mark.asyncio
async def test_discover_filelist_task_limit_keeps_unexpanded_directories_as_prefixes():
    scanner, application, bucket = make_scanner()
    scanner.config.scan.filelist_task_limit_per_bucket = 2
    scanner.config.defaults.filelist_depth = 5
    client = FakeClient(
        [
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/"},
                        {"objectType": "folder", "objectKey": "beta/"},
                        {"objectType": "folder", "objectKey": "gamma/"},
                    ]
                }
            },
            {
                "result": {
                    "files": [
                        {"objectType": "folder", "objectKey": "alpha/nested/"},
                    ]
                }
            },
        ]
    )

    discovery = await scanner._discover_filelist(application, bucket, client)

    assert discovery.filelist_completed == 2
    assert discovery.filelist_total == 2
    assert discovery.prefixes == ["alpha/nested/", "beta/", "gamma/"]
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
pytest tests/test_scanner.py::test_discover_filelist_recurses_to_depth_and_uses_final_prefixes tests/test_scanner.py::test_discover_filelist_task_limit_keeps_unexpanded_directories_as_prefixes -v
```

Expected: FAIL because `_discover_filelist` and discovery fields are not implemented.

- [ ] **Step 4: Add discovery model**

In `src/obs_scan_platform/models.py`, replace or extend `RootDiscovery`:

```python
@dataclass(frozen=True)
class FilelistDiscovery:
    prefixes: list[str]
    metadata_files: list[str]
    filelist_completed: int = 0
    filelist_total: int = 0


RootDiscovery = FilelistDiscovery
```

The alias keeps older tests that import `RootDiscovery` working while the scanner migrates.

- [ ] **Step 5: Implement recursive discovery**

In `src/obs_scan_platform/scanner.py`, add a small path helper:

```python
def _directory_prefix(value: Any) -> str:
    prefix = str(value or "").strip("/")
    if not prefix:
        return ""
    return f"{prefix}/"
```

Add `_filelist_path_for_prefix`:

```python
def _filelist_path_for_prefix(prefix: str) -> str:
    if not prefix:
        return "/"
    return "/" + prefix.strip("/") + "/"
```

Replace `_discover_root` with `_discover_filelist`:

```python
    async def _discover_filelist(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
    ) -> RootDiscovery:
        settings = self.config.thresholds_for(application, bucket.name)
        max_depth = max(0, settings.filelist_depth)
        limit = max(1, self.config.scan.filelist_task_limit_per_bucket)
        metadata_files: list[str] = []
        final_prefixes: set[str] = set()
        queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        queue.put_nowait(("", 0))
        filelist_total = 1
        filelist_completed = 0
        url = _endpoint(self.config.endpoint_for(application), "/rest/s3/bucket/filelist")

        while True:
            try:
                prefix, depth = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                pointer = ""
                while True:
                    request_body = encode_request_body(
                        {
                            "id": bucket.bucket_id,
                            "path": _filelist_path_for_prefix(prefix),
                            "pointer": pointer,
                            "size": self.config.scan.page_size,
                        }
                    )
                    data = await client.get_json(
                        url,
                        params={"appid": application.appid, "requestbody": request_body},
                        headers={**JSON_HEADERS, "csb-token": application.apptoken},
                    )
                    payload = _result_payload(data)
                    for item in _items_from_payload(payload, "files", "list", "items"):
                        object_type = str(item.get("objectType") or "").lower()
                        object_key = item.get("objectKey") or item.get("name")
                        if object_type == "folder":
                            child_prefix = _directory_prefix(object_key)
                            if not child_prefix:
                                continue
                            if depth < max_depth and filelist_total < limit:
                                queue.put_nowait((child_prefix, depth + 1))
                                filelist_total += 1
                            else:
                                final_prefixes.add(child_prefix)
                        elif object_key:
                            metadata_files.append(str(object_key).lstrip("/"))

                    next_pointer = ""
                    if isinstance(payload, dict):
                        next_pointer = str(payload.get("nextOffset") or "")
                    if not next_pointer or next_pointer == pointer:
                        break
                    pointer = next_pointer
            finally:
                filelist_completed += 1
                queue.task_done()

        return RootDiscovery(
            prefixes=sorted(final_prefixes),
            metadata_files=metadata_files,
            filelist_completed=filelist_completed,
            filelist_total=filelist_total,
        )
```

- [ ] **Step 6: Wire scanner to new discovery**

In `_scan_bucket`, replace:

```python
discovery = await self._discover_root(application, bucket, client)
await self._collect_root_files(application, bucket, endpoint, discovery.root_files, temp_dir, client)
await self._collect_prefixes(application, bucket, endpoint, discovery.prefixes, temp_dir, client)
```

with:

```python
discovery = await self._discover_filelist(application, bucket, client)
await self._collect_metadata_files(application, bucket, endpoint, discovery.metadata_files, temp_dir, client)
await self._collect_prefixes(application, bucket, endpoint, discovery.prefixes, temp_dir, client)
```

Rename `_collect_root_files` to `_collect_metadata_files`. Keep a wrapper if existing tests still call `_collect_root_files`:

```python
    async def _collect_root_files(self, application, bucket, endpoint, root_files, temp_dir, client) -> None:
        await self._collect_metadata_files(application, bucket, endpoint, root_files, temp_dir, client)
```

- [ ] **Step 7: Run scanner tests and commit**

Run:

```bash
pytest tests/test_scanner.py -v
```

Expected: PASS.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/models.py src/obs_scan_platform/scanner.py tests/test_scanner.py
/opt/homebrew/bin/git commit -m "feat: add bounded recursive filelist discovery"
```

## Task 4: Empty Bucket Success and Bucket Duration Logs

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`

- [ ] **Step 1: Write failing empty root bucket test**

Add to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_scan_bucket_empty_root_writes_header_only_csv(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {"result": "http://bucket-endpoint/"},
            {"result": {"files": []}},
        ]
    )

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, 1000)

    assert result.status == ScanStatus.SUCCESS
    assert result.csv_path is not None
    assert result.csv_path.exists()
    assert result.csv_path.read_text(encoding="utf-8").startswith("run_id,appid,bucket_name")
    assert len(list(csv.DictReader(result.csv_path.open(newline="", encoding="utf-8")))) == 0
```

- [ ] **Step 2: Write failing empty nested folder test**

Add:

```python
@pytest.mark.asyncio
async def test_scan_bucket_only_empty_folders_writes_header_only_csv(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 5
    client = FakeClient(
        [
            {"result": "http://bucket-endpoint/"},
            {"result": {"files": [{"objectType": "folder", "objectKey": "empty/"}]}},
            {"result": {"files": []}},
        ]
    )

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, 1000)

    assert result.status == ScanStatus.SUCCESS
    assert result.csv_path is not None
    assert result.csv_path.exists()
    rows = list(csv.DictReader(result.csv_path.open(newline="", encoding="utf-8")))
    assert rows == []
```

- [ ] **Step 3: Write failing bucket elapsed log test**

Add:

```python
@pytest.mark.asyncio
async def test_scan_bucket_logs_elapsed_seconds(tmp_path: Path, caplog):
    scanner, application, bucket = make_scanner()
    client = FakeClient(
        [
            {"result": "http://bucket-endpoint/"},
            {"result": {"files": []}},
        ]
    )

    with caplog.at_level("INFO", logger="obs_scan_platform.scanner"):
        await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, 1000)

    messages = [record.getMessage() for record in caplog.records]
    assert any("bucket empty appid=app.one bucket=bucket-name-1 objects=0" in message for message in messages)
    assert any("elapsed_seconds=" in message and "bucket finish appid=app.one bucket=bucket-name-1 status=success" in message for message in messages)
```

- [ ] **Step 4: Run tests and verify failures**

Run:

```bash
pytest tests/test_scanner.py::test_scan_bucket_empty_root_writes_header_only_csv tests/test_scanner.py::test_scan_bucket_only_empty_folders_writes_header_only_csv tests/test_scanner.py::test_scan_bucket_logs_elapsed_seconds -v
```

Expected: At least the log assertions fail before elapsed/empty log implementation. Empty CSV may already pass; if it passes, keep the regression test.

- [ ] **Step 5: Implement duration and empty logs**

In `_scan_bucket`, record a monotonic start time:

```python
bucket_started = time.monotonic()
discovery = None
object_count = 0
```

After aggregation, count final CSV rows:

```python
row_count = aggregate_bucket(...)
object_count = row_count
if row_count == 0:
    LOGGER.info("bucket empty appid=%s bucket=%s objects=0", application.appid, bucket.name)
```

Use this finish log on success:

```python
elapsed_seconds = time.monotonic() - bucket_started
LOGGER.info(
    "bucket finish appid=%s bucket=%s status=%s elapsed_seconds=%.2f filelist_completed=%s filelist_total=%s",
    application.appid,
    bucket.name,
    ScanStatus.SUCCESS.value,
    elapsed_seconds,
    discovery.filelist_completed if discovery is not None else 0,
    discovery.filelist_total if discovery is not None else 0,
)
```

Use this finish log in the exception path:

```python
elapsed_seconds = time.monotonic() - bucket_started
LOGGER.info(
    "bucket finish appid=%s bucket=%s status=%s elapsed_seconds=%.2f filelist_completed=%s filelist_total=%s error=%s",
    application.appid,
    bucket.name,
    ScanStatus.FAILED.value,
    elapsed_seconds,
    discovery.filelist_completed if discovery is not None else 0,
    discovery.filelist_total if discovery is not None else 0,
    exc,
)
```

Keep the existing `LOGGER.exception("bucket failure ...")` so stack traces remain available for real failures.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: PASS.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
/opt/homebrew/bin/git commit -m "fix: handle empty buckets and log bucket duration"
```

## Task 5: Filelist Progress Logging and CLI tqdm

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `src/obs_scan_platform/cli.py`
- Modify: `src/obs_scan_platform/api.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add `tqdm` dependency**

Add to `pyproject.toml` dependencies:

```toml
  "tqdm>=4.66",
```

- [ ] **Step 2: Write failing scanner progress log test**

Add to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_discover_filelist_logs_progress(tmp_path: Path, caplog):
    scanner, application, bucket = make_scanner()
    client = FakeClient([{"result": {"files": []}}])

    with caplog.at_level("INFO", logger="obs_scan_platform.scanner"):
        discovery = await scanner._discover_filelist(application, bucket, client)

    assert discovery.filelist_completed == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "filelist progress appid=app.one bucket=bucket-name-1 completed=1 total=1 limit=100 depth=0/5" in message
        for message in messages
    )
```

- [ ] **Step 3: Write failing CLI progress flag test**

In `tests/test_cli.py`, add:

```python
def test_scan_cli_enables_progress(monkeypatch, tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("endpoint: http://obs.example\n", encoding="utf-8")
    called = {}

    async def fake_run_scan(config, *, run_id=None, appid=None, show_progress=False):
        called["config"] = config
        called["show_progress"] = show_progress
        return {"status": "success", "run_id": "run-1"}

    monkeypatch.setattr("obs_scan_platform.cli.run_scan", fake_run_scan)
    result = CliRunner().invoke(app, ["scan", "--config", str(config_file)])

    assert result.exit_code == 0
    assert called["show_progress"] is True
```

- [ ] **Step 4: Write failing API progress flag test**

In `tests/test_api.py`, update the existing accepted scan test or add:

```python
def test_post_runs_disables_cli_progress(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)
    called = {}

    async def fake_run_scan(path, *, show_progress=False):
        called["path"] = path
        called["show_progress"] = show_progress

    monkeypatch.setattr(api, "run_scan", fake_run_scan)
    client = TestClient(api.create_app(config_path=config_path, results_dir=tmp_path))

    response = client.post("/runs")

    assert response.status_code == 202
    assert called == {"path": config_path, "show_progress": False}
```

- [ ] **Step 5: Run tests and verify failures**

Run:

```bash
pytest tests/test_scanner.py::test_discover_filelist_logs_progress tests/test_cli.py::test_scan_cli_enables_progress tests/test_api.py::test_post_runs_disables_cli_progress -v
```

Expected: FAIL because `show_progress` and progress logs are not implemented.

- [ ] **Step 6: Implement progress flag**

Change `Scanner.__init__`:

```python
class Scanner:
    def __init__(self, config: AppConfigFile, *, show_progress: bool = False) -> None:
        self.config = config
        self.show_progress = show_progress
        self.request_semaphore = asyncio.Semaphore(config.scan.global_request_concurrency)
```

Change `run_scan`:

```python
async def run_scan(
    config_path: str | Path,
    *,
    run_id: str | None = None,
    appid: str | None = None,
    show_progress: bool = False,
) -> dict[str, Any]:
    scanner = Scanner(load_config(config_path), show_progress=show_progress)
    return await scanner.run(run_id=run_id, appid=appid)
```

Change `src/obs_scan_platform/cli.py`:

```python
manifest = asyncio.run(run_scan(config, run_id=run_id, appid=appid, show_progress=True))
```

FastAPI does not pass `show_progress`, so it remains false. If the API test monkeypatch expects a keyword, change `_run_scan_background` to:

```python
await run_scan(config_path, show_progress=False)
```

- [ ] **Step 7: Implement progress logging and optional tqdm**

Inside `_discover_filelist`, after each directory task completes:

```python
LOGGER.info(
    "filelist progress appid=%s bucket=%s completed=%s total=%s limit=%s depth=%s/%s",
    application.appid,
    bucket.name,
    filelist_completed,
    filelist_total,
    limit,
    depth,
    max_depth,
)
```

For `tqdm`, import lazily:

```python
from contextlib import nullcontext
```

Inside `_discover_filelist`, before the loop:

```python
progress_bar = None
if self.show_progress:
    from tqdm import tqdm

    progress_bar = tqdm(total=filelist_total, desc=f"{application.appid}/{bucket.name} filelist", unit="dir")
```

When `filelist_total` increases:

```python
if progress_bar is not None:
    progress_bar.total = filelist_total
    progress_bar.refresh()
```

When a directory completes:

```python
if progress_bar is not None:
    progress_bar.update(1)
```

Ensure the bar closes:

```python
try:
    ...
finally:
    if progress_bar is not None:
        progress_bar.close()
```

- [ ] **Step 8: Run progress tests and commit**

Run:

```bash
pytest tests/test_scanner.py::test_discover_filelist_logs_progress tests/test_cli.py tests/test_api.py -v
```

Expected: PASS.

Commit:

```bash
/opt/homebrew/bin/git add pyproject.toml src/obs_scan_platform/scanner.py src/obs_scan_platform/cli.py src/obs_scan_platform/api.py tests/test_scanner.py tests/test_cli.py tests/test_api.py
/opt/homebrew/bin/git commit -m "feat: add filelist progress reporting"
```

## Task 6: Documentation and End-to-End Compatibility

**Files:**
- Modify: `README.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `tests/test_scan_end_to_end.py`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

- [ ] **Step 1: Update end-to-end config shape**

In `tests/test_scan_end_to_end.py`, construct `AppConfigFile` with top-level endpoint:

```python
config = AppConfigFile(
    endpoint="https://obs-api.example",
    defaults=Thresholds(
        large_directory_bytes=20,
        large_file_bytes=10,
        inactive_directory_days=180,
        filelist_depth=5,
    ),
    applications=[
        ApplicationConfig(
            appid="app.one",
            name="App One",
            apptoken="token-1",
            scan_shared_buckets=False,
        )
    ],
)
```

Adjust fake OBS responses if recursive filelist now calls additional paths. Keep assertions that the shared bucket is skipped by default.

- [ ] **Step 2: Update README**

In `README.md`, update the config section to mention:

Add this prose and YAML example:

    The recommended config shape uses one top-level OBS API endpoint:

    ```yaml
    endpoint: http://obs.example
    ```

    Each application can opt into shared bucket scanning:

    ```yaml
    scan_shared_buckets: false
    ```

    Directory discovery uses recursive `filelist` splitting. The default depth is `5`, and each bucket is capped around `100` filelist tasks by `scan.filelist_task_limit_per_bucket`.

- [ ] **Step 3: Update scan start guide**

In `docs/scan-start-guide.md`, update the prep section to mention:

```markdown
`endpoint` is configured once at the top level. CLI scans show `tqdm` progress for each bucket's filelist discovery. FastAPI scans do not show progress bars; use `results/<run_id>/scan.log` to see filelist progress and bucket elapsed time.
```

- [ ] **Step 4: Update handoff docs**

Update `docs/current-task.md` with:

```markdown
Task status: `completed`
Completed work:
- Implemented global endpoint config.
- Implemented shared bucket switch.
- Implemented bounded recursive filelist discovery.
- Implemented filelist progress logging and CLI tqdm.
- Implemented empty bucket success behavior.
Validation result:
- Record the exact `pytest -q` result from Step 5.
```

Update `docs/handoff.md` with:

```markdown
Latest commit before this session:
- Record the output of `/opt/homebrew/bin/git rev-parse HEAD` before implementation starts.
Latest commit after this session:
- State that the commit containing the handoff cannot include its own SHA and that `/opt/homebrew/bin/git rev-parse HEAD` gives the final SHA after commit.
Current test/build status:
- Record the exact `pytest -q` result from Step 5.
Resume instructions:
- Run `pytest -q`.
- Check `config/apps.example.yaml`.
- Run a CLI scan in a controlled environment.
```

Do not hard-code the final commit SHA inside the commit that creates it; write a note that `git rev-parse HEAD` gives the final SHA.

- [ ] **Step 5: Run full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass. A FastAPI/Starlette `httpx` deprecation warning may remain.

- [ ] **Step 6: Run CLI and API import checks**

Run:

```bash
obs-scan --help
obs-scan scan --help
python3 -c "from obs_scan_platform.api import app; print(app.title)"
```

Expected:

- Both CLI commands exit 0 and show usage.
- Python command prints `OBS Scan Platform`.

- [ ] **Step 7: Clean caches, inspect diff, and commit**

Run:

```bash
rm -rf .pytest_cache src/obs_scan_platform/__pycache__ tests/__pycache__
/opt/homebrew/bin/git status --short --branch
/opt/homebrew/bin/git diff --stat
/opt/homebrew/bin/git diff
```

Inspect the diff for secrets and unrelated edits.

Commit:

```bash
/opt/homebrew/bin/git add README.md docs/scan-start-guide.md tests/test_scan_end_to_end.py docs/current-task.md docs/handoff.md
/opt/homebrew/bin/git commit -m "docs: update scan progress configuration docs"
```

## Final Verification

- [ ] **Step 1: Run full verification**

Run:

```bash
pytest -q
obs-scan --help
obs-scan scan --help
python3 -c "from obs_scan_platform.api import app; print(app.title)"
```

Expected:

- `pytest -q` passes.
- `obs-scan --help` exits 0.
- `obs-scan scan --help` exits 0.
- Python command prints `OBS Scan Platform`.

- [ ] **Step 2: Review git status**

Run:

```bash
/opt/homebrew/bin/git status --short --branch
```

Expected: clean working tree on `codex/obs-scan-platform`.

- [ ] **Step 3: Push branch**

Run:

```bash
/opt/homebrew/bin/git push origin codex/obs-scan-platform
```

Expected: push succeeds.

## Plan Self-Review

- Spec coverage:
  - Global endpoint config: Task 1 and Task 2.
  - Shared bucket switch: Task 1 and Task 2.
  - Recursive filelist depth and task limit: Task 1 and Task 3.
  - No duplicate parent/child objectkeys prefixes: Task 3 tests and algorithm.
  - Metadata for direct files in expanded directories: Task 3.
  - Empty root and only-empty-folder buckets: Task 4.
  - Per-bucket elapsed time logging: Task 4.
  - CLI tqdm and FastAPI no tqdm: Task 5.
  - Dependency and docs: Task 5 and Task 6.
- Placeholder scan: no open-ended implementation markers are intentional.
- Type consistency:
  - `Thresholds.filelist_depth` is the effective bucket setting used by scanner.
  - `BucketOverrides` is config-only and merged through `thresholds_for()`.
  - `RootDiscovery` is kept as an alias for `FilelistDiscovery` to reduce churn.
