# OBS Scan Behavior Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix OBS scanner logging, shared bucket selection, filelist scheduling, phase ordering, and request concurrency defaults according to `docs/superpowers/specs/2026-07-09-obs-scan-behavior-corrections-design.md`.

**Architecture:** Keep the scanner's public CLI/API behavior stable. Add one focused filelist planning unit, keep `OBSClient` as the single HTTP wrapper, and make concurrency settings explicit while preserving legacy YAML compatibility. The scanner must complete a bucket's filelist phase and metadata phase before objectkeys workers begin.

**Tech Stack:** Python 3.11, asyncio, httpx, pydantic v2, pytest, pytest-asyncio, tqdm.

## Global Constraints

- Do not change FastAPI request parameters or add progress streaming.
- Do not redesign the final bucket CSV schema.
- Do not convert erroneous OBS empty-bucket HTTP/OBS failures into successful empty scans.
- Default `scan.global_request_concurrency` must be `150`.
- Default per-bucket objectkeys concurrency must be `30`.
- `scan.per_bucket_prefix_concurrency` must remain accepted for existing YAML configs.
- Default terminal logs and `scan.log` must not contain full request URLs, query strings, encoded request bodies, or tokens.
- Failed requests such as `404` and `503` must still expose safe endpoint/status/reason diagnostics.
- `scan_shared_buckets: true` must include scan-capable non-owner shared buckets.
- Filelist task limits are level-recursion thresholds, not hard caps on the current level.
- A bucket must complete all filelist discovery and metadata collection before objectkeys collection starts.
- Update `docs/current-task.md` and `docs/handoff.md` before the final commit.

---

## File Structure

- Modify `AGENTS.md`
  - Already restored from `origin/master` in the current session. Keep this synchronized change in the next commit.
- Modify `src/obs_scan_platform/config.py`
  - Update scan concurrency defaults.
  - Add a compatibility resolver for objectkeys concurrency.
- Modify `src/obs_scan_platform/obs_client.py`
  - Add sanitized `OBSRequestError` details.
  - Add an `endpoint` label parameter to `get_json`.
  - Prevent raised request errors from embedding full URLs.
- Create `src/obs_scan_platform/filelist_discovery.py`
  - Own level-based filelist scheduling and final-prefix/metadata-candidate calculation.
- Modify `src/obs_scan_platform/models.py`
  - Expand `RootDiscovery` to carry `metadata_files`.
- Modify `src/obs_scan_platform/scanner.py`
  - Pass endpoint labels into `OBSClient`.
  - Use scan-capable shared bucket selection.
  - Use the filelist scheduler.
  - Rename root metadata collection to metadata-file collection.
  - Apply objectkeys per-bucket concurrency only to objectkeys workers.
- Modify `config/apps.example.yaml`
  - Recommend `objectkeys_concurrency_per_bucket: 30`.
  - Update `global_request_concurrency: 150`.
- Modify `docs/scan-start-guide.md`
  - Align operator guidance with sanitized logs, new concurrency names, and phase ordering.
- Modify `tests/test_config.py`
  - Cover new defaults and legacy concurrency compatibility.
- Modify `tests/test_obs_client.py`
  - Cover sanitized request errors for `404`, `503`, and OBS `success=false`.
- Modify `tests/test_scanner.py`
  - Cover shared bucket selection, level-based filelist scheduling, metadata candidates, phase ordering, and objectkeys concurrency.
- Modify `tests/test_scan_end_to_end.py`
  - Cover full-run phase order and updated concurrency field names.
- Modify `docs/current-task.md` and `docs/handoff.md`
  - Record final implementation status, commands, commit, push status, and resume instructions.

---

### Task 1: Config Defaults and Objectkeys Concurrency Resolver

**Files:**
- Modify: `src/obs_scan_platform/config.py`
- Modify: `config/apps.example.yaml`
- Modify: `docs/scan-start-guide.md`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ScanSettings.objectkeys_concurrency_limit() -> int`
- Consumes: Existing pydantic config loading and scanner `self.config.scan`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py`:

```python
def test_scan_settings_new_concurrency_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan: {}
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.global_request_concurrency == 150
    assert config.scan.objectkeys_concurrency_limit() == 30


def test_legacy_per_bucket_prefix_concurrency_still_sets_objectkeys_limit(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  per_bucket_prefix_concurrency: 7
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.objectkeys_concurrency_limit() == 7


def test_new_objectkeys_concurrency_field_wins_over_legacy_field(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  per_bucket_prefix_concurrency: 7
  objectkeys_concurrency_per_bucket: 13
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.objectkeys_concurrency_limit() == 13
```

- [ ] **Step 2: Run config tests and verify the new tests fail**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: the new default/resolver tests fail because the fields and resolver are not implemented yet.

- [ ] **Step 3: Implement config compatibility**

In `src/obs_scan_platform/config.py`, update `ScanSettings`:

```python
class ScanSettings(BaseModel):
    results_dir: str = "results"
    temp_subdir: str = "_tmp"
    keep_temp_files: bool = False
    page_size: int = 1000
    app_concurrency: int = 2
    bucket_concurrency: int = 4
    global_request_concurrency: int = 150
    per_bucket_prefix_concurrency: int | None = None
    objectkeys_concurrency_per_bucket: int | None = None
    metadata_concurrency_per_bucket: int = 8
    request_timeout_seconds: int = 30
    max_retries: int = 5
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 60
    filelist_task_limit_per_bucket: int = 100

    def objectkeys_concurrency_limit(self) -> int:
        if self.objectkeys_concurrency_per_bucket is not None:
            return self.objectkeys_concurrency_per_bucket
        if self.per_bucket_prefix_concurrency is not None:
            return self.per_bucket_prefix_concurrency
        return 30
```

- [ ] **Step 4: Update example YAML and operator docs**

In `config/apps.example.yaml`, replace the scan concurrency entries with:

```yaml
  global_request_concurrency: 150
  objectkeys_concurrency_per_bucket: 30
  metadata_concurrency_per_bucket: 8
```

Remove the example `per_bucket_prefix_concurrency` line from the recommended sample.

In `docs/scan-start-guide.md`, update the configuration guidance to state:

推荐并发默认值：

```yaml
scan:
  global_request_concurrency: 150
  objectkeys_concurrency_per_bucket: 30
```

`objectkeys_concurrency_per_bucket` 只限制单个桶内 objectkeys 前缀 worker 的并发；`filelist` 和 metadata 请求仍受全局请求并发限制。旧配置项 `per_bucket_prefix_concurrency` 仍兼容，但新配置建议使用 `objectkeys_concurrency_per_bucket`。

- [ ] **Step 5: Run config tests and commit**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: all config tests pass.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/config.py config/apps.example.yaml docs/scan-start-guide.md tests/test_config.py
/opt/homebrew/bin/git commit -m "fix: update scan concurrency config defaults"
```

---

### Task 2: Sanitized OBS Request Errors

**Files:**
- Modify: `src/obs_scan_platform/obs_client.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_obs_client.py`

**Interfaces:**
- Produces: `OBSClient.get_json(..., endpoint: str = "unknown")`
- Produces: `OBSRequestError(endpoint: str, status_code: int | None, reason: str)`
- Consumes: Scanner call sites pass endpoint labels such as `listbuckets`, `filelist`, `metadata`, and `objectkeys`.

- [ ] **Step 1: Write failing sanitized error tests**

Add these tests to `tests/test_obs_client.py`:

```python
@pytest.mark.asyncio
async def test_get_json_404_error_is_sanitized_and_has_endpoint_label():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "msg": "missing"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=3,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json(
                "http://obs.example/test?token=secret-token",
                params={"requestbody": "encoded-secret-body"},
                endpoint="filelist",
            )
    finally:
        await client.close()

    message = str(exc_info.value)
    assert "endpoint=filelist" in message
    assert "status=404" in message
    assert "missing" in message
    assert "http://obs.example" not in message
    assert "secret-token" not in message
    assert "encoded-secret-body" not in message


@pytest.mark.asyncio
async def test_get_json_503_error_after_retries_is_sanitized():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"success": False, "msg": "busy"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("http://obs.example/test", params={}, endpoint="objectkeys")
    finally:
        await client.close()

    assert calls == 2
    assert str(exc_info.value) == "OBS request failed endpoint=objectkeys status=503 reason=busy"


@pytest.mark.asyncio
async def test_get_json_success_false_error_is_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "msg": "permission denied"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("http://obs.example/test", params={"token": "secret"}, endpoint="metadata")
    finally:
        await client.close()

    assert str(exc_info.value) == "OBS request failed endpoint=metadata status=200 reason=permission denied"
```

Update the old `test_get_json_does_not_retry_404` expectation from `httpx.HTTPStatusError` to `OBSRequestError`.

- [ ] **Step 2: Run OBS client tests and verify failure**

Run:

```bash
pytest tests/test_obs_client.py -v
```

Expected: sanitized error tests fail because `get_json` has no `endpoint` parameter and `404` still raises `httpx.HTTPStatusError`.

- [ ] **Step 3: Implement sanitized request errors**

In `src/obs_scan_platform/obs_client.py`, replace `OBSRequestError` with:

```python
class OBSRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        reason: str,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.reason = reason
        status = "unknown" if status_code is None else str(status_code)
        super().__init__(f"OBS request failed endpoint={endpoint} status={status} reason={reason}")
```

Add helpers:

```python
def _response_reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:200] if text else response.reason_phrase
    if isinstance(data, dict):
        value = data.get("msg") or data.get("message") or data.get("error")
        if value:
            return str(value)[:200]
    return response.reason_phrase


def _json_failure_reason(data: dict[str, Any]) -> str:
    return str(data.get("msg") or data.get("message") or data.get("error") or "OBS returned success=false")[:200]
```

Update `get_json` signature and error branches:

```python
async def get_json(
    self,
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    endpoint: str = "unknown",
) -> dict[str, Any]:
    last_error: OBSRequestError | httpx.TimeoutException | httpx.ConnectError | None = None
    for attempt in range(self.max_retries + 1):
        try:
            async with self.request_semaphore:
                response = await self.http.get(url, params=params, headers=headers)
            if response.status_code >= 400:
                error = OBSRequestError(
                    endpoint=endpoint,
                    status_code=response.status_code,
                    reason=_response_reason(response),
                )
                if response.status_code < 500:
                    raise error
                raise error
            data = response.json()
            success = data.get("success")
            if success in (False, "false"):
                raise OBSRequestError(
                    endpoint=endpoint,
                    status_code=response.status_code,
                    reason=_json_failure_reason(data),
                )
            return data
        except OBSRequestError as exc:
            last_error = exc
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise
            if attempt >= self.max_retries:
                break
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            if attempt >= self.max_retries:
                break
        delay = min(
            self.retry_max_delay_seconds,
            self.retry_base_delay_seconds * (2**attempt),
        )
        if delay > 0:
            await asyncio.sleep(delay)
    if isinstance(last_error, OBSRequestError):
        raise last_error
    reason = last_error.__class__.__name__ if last_error is not None else "unknown"
    raise OBSRequestError(endpoint=endpoint, status_code=None, reason=reason)
```

- [ ] **Step 4: Pass endpoint labels at scanner call sites**

In `src/obs_scan_platform/scanner.py`, add `endpoint=` to every `client.get_json` call:

```python
endpoint="listbuckets"
endpoint="bucket_endpoint"
endpoint="filelist"
endpoint="metadata"
endpoint="objectkeys"
```

In `tests/test_scanner.py` and `tests/test_scan_end_to_end.py`, update fake client signatures so endpoint labels are accepted:

```python
async def get_json(
    self,
    url,
    *,
    params,
    headers=None,
    endpoint="unknown",
):
    ...
```

- [ ] **Step 5: Run OBS client tests and commit**

Run:

```bash
pytest tests/test_obs_client.py -v
```

Expected: all OBS client tests pass.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/obs_client.py src/obs_scan_platform/scanner.py tests/test_obs_client.py
/opt/homebrew/bin/git commit -m "fix: sanitize OBS request errors"
```

---

### Task 3: Shared Bucket Selection

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`
- Test: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Produces: `is_scan_capable_bucket(bucket: BucketInfo) -> bool`
- Changes: `should_scan_bucket(bucket, include_shared)` includes all scan-capable buckets when `include_shared=True`.

- [ ] **Step 1: Write failing shared bucket unit tests**

Replace `test_should_scan_bucket_includes_owned_and_optional_shared_buckets` in `tests/test_scanner.py` with:

```python
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
```

Add:

```python
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
```

- [ ] **Step 2: Run shared bucket tests and verify failure**

Run:

```bash
pytest tests/test_scanner.py::test_should_scan_bucket_includes_scan_capable_shared_buckets_when_enabled tests/test_scanner.py::test_list_buckets_logs_skip_for_missing_required_shared_bucket -v
```

Expected: the first test fails because non-owner shared buckets are excluded.

- [ ] **Step 3: Implement scan-capable bucket selection**

In `src/obs_scan_platform/scanner.py`, add:

```python
def is_scan_capable_bucket(bucket: BucketInfo) -> bool:
    return bool(bucket.bucket_id and bucket.name and bucket.vendor and bucket.region)
```

Replace `should_scan_bucket` with:

```python
def should_scan_bucket(bucket: BucketInfo, include_shared: bool) -> bool:
    if not is_scan_capable_bucket(bucket):
        return False
    if include_shared:
        return True
    return is_owned_bucket(bucket)
```

In `_list_buckets`, log skipped incomplete buckets:

```python
if should_scan_bucket(bucket, application.scan_shared_buckets):
    buckets.append(bucket)
elif not is_scan_capable_bucket(bucket):
    LOGGER.warning(
        "bucket skipped appid=%s bucket=%s reason=missing_required_fields",
        application.appid,
        bucket.name or "<missing>",
    )
```

- [ ] **Step 4: Update end-to-end shared bucket fixture**

In `tests/test_scan_end_to_end.py`, add a small `SharedBucketOBSClient` or extend `FakeOBSClient` only for a new test. The new test must set `scan_shared_buckets=True` and assert endpoint/filelist/objectkeys calls include both owned and non-owner shared bucket names:

```python
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
```

Implement `SharedBucketOBSClient` so each bucket returns an empty filelist and objectkeys response. Keep assertions focused on bucket inclusion.

- [ ] **Step 5: Run scanner tests and commit**

Run:

```bash
pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: all scanner and end-to-end tests pass.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
/opt/homebrew/bin/git commit -m "fix: include scan-capable shared buckets"
```

---

### Task 4: Level-Based Filelist Discovery Scheduler

**Files:**
- Create: `src/obs_scan_platform/filelist_discovery.py`
- Modify: `src/obs_scan_platform/models.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Produces: `FilelistDiscoveryScheduler(max_depth: int, task_limit: int)`
- Produces: `FilelistTask(path: str, depth: int)`
- Produces: `FilelistDiscoveryScheduler.current_level() -> list[FilelistTask]`
- Produces: `FilelistDiscoveryScheduler.record_folder(task, prefix: str) -> None`
- Produces: `FilelistDiscoveryScheduler.record_file(task, object_key: str) -> None`
- Produces: `FilelistDiscoveryScheduler.finish_level() -> bool`
- Produces: `FilelistDiscoveryScheduler.result() -> RootDiscovery`

- [ ] **Step 1: Write failing level-batching tests**

In `tests/test_scanner.py`, replace `test_discover_root_limits_recursive_filelist_tasks_but_keeps_discovered_prefixes` with:

```python
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
```

Add:

```python
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
```

Add metadata candidate coverage:

```python
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
```

- [ ] **Step 2: Run filelist tests and verify failure**

Run:

```bash
pytest tests/test_scanner.py::test_discover_root_processes_whole_level_even_when_it_exceeds_task_limit tests/test_scanner.py::test_discover_root_schedules_deeper_level_when_current_level_keeps_total_below_limit tests/test_scanner.py::test_discover_root_returns_metadata_files_not_covered_by_objectkeys_prefixes -v
```

Expected: the whole-level test fails under the old hard-cap logic.

- [ ] **Step 3: Add scheduler unit**

Create `src/obs_scan_platform/filelist_discovery.py`:

```python
from dataclasses import dataclass, field

from obs_scan_platform.models import RootDiscovery


@dataclass(frozen=True)
class FilelistTask:
    path: str
    depth: int


@dataclass
class FilelistDiscoveryScheduler:
    max_depth: int
    task_limit: int
    _current_level: list[FilelistTask] = field(default_factory=lambda: [FilelistTask("/", 1)])
    _next_level: list[FilelistTask] = field(default_factory=list)
    _queued_paths: set[str] = field(default_factory=lambda: {"/"})
    _scanned_paths: set[str] = field(default_factory=set)
    _discovered_prefixes: set[str] = field(default_factory=set)
    _direct_files: list[str] = field(default_factory=list)
    _total_tasks: int = 1
    _completed_tasks: int = 0

    @property
    def total_tasks(self) -> int:
        return self._total_tasks

    @property
    def completed_tasks(self) -> int:
        return self._completed_tasks

    def current_level(self) -> list[FilelistTask]:
        tasks = [task for task in self._current_level if task.path not in self._scanned_paths]
        self._current_level = []
        return tasks

    def record_folder(self, task: FilelistTask, prefix: str) -> None:
        if not prefix:
            return
        self._discovered_prefixes.add(prefix)
        queued_path = "/" + prefix
        if task.depth < self.max_depth and queued_path not in self._queued_paths:
            self._next_level.append(FilelistTask(queued_path, task.depth + 1))
            self._queued_paths.add(queued_path)

    def record_file(self, task: FilelistTask, object_key: str) -> None:
        if object_key:
            self._direct_files.append(str(object_key))

    def mark_completed(self, task: FilelistTask) -> None:
        self._scanned_paths.add(task.path)
        self._completed_tasks += 1

    def finish_level(self) -> bool:
        if not self._next_level:
            return False
        if self._total_tasks >= self.task_limit:
            self._next_level = []
            return False
        self._current_level = self._next_level
        self._next_level = []
        self._total_tasks += len(self._current_level)
        return True

    def result(self) -> RootDiscovery:
        prefixes = self._top_level_prefixes(self._discovered_prefixes)
        metadata_files = [
            object_key
            for object_key in self._direct_files
            if not any(object_key.startswith(prefix) for prefix in prefixes)
        ]
        return RootDiscovery(prefixes=prefixes, metadata_files=metadata_files)

    def _top_level_prefixes(self, prefixes: set[str]) -> list[str]:
        selected: list[str] = []
        for prefix in sorted(prefixes):
            if not any(prefix.startswith(parent) for parent in selected):
                selected.append(prefix)
        return selected
```

- [ ] **Step 4: Expand RootDiscovery model**

In `src/obs_scan_platform/models.py`, update:

```python
@dataclass(frozen=True)
class RootDiscovery:
    prefixes: list[str]
    metadata_files: list[str]

    @property
    def root_files(self) -> list[str]:
        return self.metadata_files
```

The `root_files` property preserves existing read-only test compatibility while scanner code moves to `metadata_files`.

- [ ] **Step 5: Refactor `_discover_root` to process whole levels**

In `src/obs_scan_platform/scanner.py`, import:

```python
from obs_scan_platform.filelist_discovery import FilelistDiscoveryScheduler, FilelistTask
```

Replace queue bookkeeping in `_discover_root` with level processing:

```python
scheduler = FilelistDiscoveryScheduler(max_depth=max_depth, task_limit=task_limit)
progress_bar = self._filelist_progress_bar(application, bucket, scheduler.total_tasks) if self.show_progress else None
if progress_bar is not None:
    progress_bar.total = scheduler.total_tasks
try:
    while True:
        tasks = scheduler.current_level()
        if not tasks:
            break
        for task in tasks:
            pointer = ""
            while True:
                request_body = encode_request_body(
                    {
                        "id": bucket.bucket_id,
                        "path": task.path,
                        "pointer": pointer,
                        "size": self.config.scan.page_size,
                    }
                )
                data = await client.get_json(
                    url,
                    params={"appid": application.appid, "requestbody": request_body},
                    headers={**JSON_HEADERS, "csb-token": application.apptoken},
                    endpoint="filelist",
                )
                payload = _result_payload(data)
                for item in _items_from_payload(payload, "files", "list", "items"):
                    object_type = str(item.get("objectType") or "").lower()
                    object_key = item.get("objectKey")
                    if object_type == "folder":
                        prefix = self._filelist_folder_prefix(task.path, object_key or item.get("name"))
                        scheduler.record_folder(task, prefix)
                    elif object_key:
                        scheduler.record_file(task, str(object_key))

                next_pointer = None
                if isinstance(payload, dict):
                    next_pointer = str(payload.get("nextOffset") or "")
                if not next_pointer or next_pointer == pointer:
                    break
                pointer = next_pointer

            scheduler.mark_completed(task)
            LOGGER.info(
                "filelist progress appid=%s bucket=%s completed=%s total=%s",
                application.appid,
                bucket.name,
                scheduler.completed_tasks,
                scheduler.total_tasks,
            )
            if progress_bar is not None:
                progress_bar.update(1)
        if not scheduler.finish_level():
            break
        if progress_bar is not None:
            progress_bar.total = scheduler.total_tasks
            progress_bar.refresh()
finally:
    if progress_bar is not None:
        progress_bar.close()

return scheduler.result()
```

- [ ] **Step 6: Run scanner filelist tests and commit**

Run:

```bash
pytest tests/test_scanner.py -v
```

Expected: scanner tests pass after updating old assertions from `root_files` to `metadata_files` where clearer.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/filelist_discovery.py src/obs_scan_platform/models.py src/obs_scan_platform/scanner.py tests/test_scanner.py
/opt/homebrew/bin/git commit -m "fix: make filelist discovery level based"
```

---

### Task 5: Bucket Phase Order and Objectkeys Worker Limit

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`
- Test: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Renames internal method: `_collect_root_files(...)` to `_collect_metadata_files(...)`
- Consumes: `RootDiscovery.metadata_files`
- Consumes: `ScanSettings.objectkeys_concurrency_limit()`

- [ ] **Step 1: Write failing phase-order test**

Add to `tests/test_scanner.py`:

```python
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


@pytest.mark.asyncio
async def test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.objectkeys_concurrency_per_bucket = 1
    client = PhaseOrderClient()

    result = await scanner._scan_bucket(application, bucket, client, "run-1", tmp_path, scan_started_ms=1000)

    assert result.status == ScanStatus.SUCCESS
    assert client.phase_events == ["bucket_endpoint", "filelist", "filelist", "metadata", "objectkeys"]
```

This test relies on Task 2 endpoint labels and Task 4 level discovery.

- [ ] **Step 2: Write objectkeys concurrency resolver test**

Replace `test_collect_prefixes_processes_all_prefixes_with_bounded_workers` setup in `tests/test_scanner.py`:

```python
scanner.config.scan.objectkeys_concurrency_per_bucket = 2
scanner.config.scan.per_bucket_prefix_concurrency = 5
```

Keep the assertion:

```python
assert client.max_active <= 2
```

This fails until `_collect_prefixes` uses `objectkeys_concurrency_limit()`.

- [ ] **Step 3: Run phase/concurrency tests and verify failure**

Run:

```bash
pytest tests/test_scanner.py::test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys tests/test_scanner.py::test_collect_prefixes_processes_all_prefixes_with_bounded_workers -v
```

Expected: at least one test fails until scanner call sites pass endpoint labels and use the new objectkeys limit.

- [ ] **Step 4: Rename and generalize metadata collection**

In `src/obs_scan_platform/scanner.py`, rename `_collect_root_files` to `_collect_metadata_files`:

```python
async def _collect_metadata_files(
    self,
    application: ApplicationConfig,
    bucket: BucketInfo,
    endpoint: str,
    object_keys: list[str],
    temp_dir: Path,
    client: OBSClient,
) -> None:
    rows: list[ObjectRow] = []
    queue: asyncio.Queue[str] = asyncio.Queue()
    for object_key in object_keys:
        queue.put_nowait(object_key)
```

Inside its `client.get_json` call, pass:

```python
endpoint="metadata"
```

Write output to the same temp file path:

```python
append_object_rows(temp_dir / "metadata_files.csv", rows)
```

Update tests that read `root_files.csv` to read `metadata_files.csv`.

- [ ] **Step 5: Enforce bucket phase order**

In `_scan_bucket`, use:

```python
endpoint = await self._get_bucket_endpoint(application, bucket, client)
discovery = await self._discover_root(application, bucket, client, thresholds)
await self._collect_metadata_files(application, bucket, endpoint, discovery.metadata_files, temp_dir, client)
await self._collect_prefixes(application, bucket, endpoint, discovery.prefixes, temp_dir, client)
aggregate_bucket(...)
```

Do not start `_collect_prefixes` until `_collect_metadata_files` returns.

- [ ] **Step 6: Use objectkeys concurrency resolver**

In `_collect_prefixes`, replace:

```python
worker_count = min(max(1, self.config.scan.per_bucket_prefix_concurrency), len(prefixes))
```

with:

```python
worker_count = min(max(1, self.config.scan.objectkeys_concurrency_limit()), len(prefixes))
```

- [ ] **Step 7: Update end-to-end tests**

In `tests/test_scan_end_to_end.py`, replace:

```python
config.scan.per_bucket_prefix_concurrency = 1
```

with:

```python
config.scan.objectkeys_concurrency_per_bucket = 1
```

Keep old-field compatibility covered only in `tests/test_config.py`.

- [ ] **Step 8: Run scanner and end-to-end tests and commit**

Run:

```bash
pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: all selected tests pass.

Commit:

```bash
/opt/homebrew/bin/git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
/opt/homebrew/bin/git commit -m "fix: order bucket scan phases"
```

---

### Task 6: Final Integration, Documentation, and Handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Modify as needed: tests touched by earlier tasks

**Interfaces:**
- Consumes: all changes from Tasks 1-5.
- Produces: clean branch pushed to GitHub.

- [ ] **Step 1: Update user-facing docs**

In `README.md` and `docs/scan-start-guide.md`, ensure these statements are present:

```markdown
- 默认全局请求并发为 `150`。
- 默认单桶 `objectkeys` 并发为 `30`，通过 `scan.objectkeys_concurrency_per_bucket` 配置。
- 旧配置项 `scan.per_bucket_prefix_concurrency` 仍兼容，但新配置建议使用 `scan.objectkeys_concurrency_per_bucket`。
- 命令行和 `scan.log` 默认不会打印完整 OBS 请求链接、query、requestbody 或 token。
- 请求失败仍会记录安全诊断信息，例如 `endpoint=objectkeys status=503 reason=busy`。
- 每个桶会先完成全部 `filelist` 发现和 metadata 获取，再开始 `objectkeys` 获取。
- `scan_shared_buckets: true` 会扫描字段完整、可尝试扫描的共享桶。
- `scan.filelist_task_limit_per_bucket` 是继续递归更深层级的目标阈值，不会截断当前层级已经发现的目录任务。
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
pytest tests/test_config.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: all targeted tests pass.

- [ ] **Step 3: Run full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass. Existing third-party deprecation warnings can remain if no test fails.

- [ ] **Step 4: Update handoff docs**

Update `docs/current-task.md` with:

```markdown
# Current Task

## Current task title

Implement OBS scan behavior corrections

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Implement the approved behavior corrections for sanitized request logs, shared bucket scanning, level-based filelist scheduling, bucket phase ordering, and request concurrency defaults.

## Completed work

- Synchronized `AGENTS.md` from `origin/master`.
- Updated scan concurrency defaults and legacy compatibility.
- Sanitized OBS request errors while preserving endpoint/status/reason diagnostics.
- Included scan-capable shared buckets when `scan_shared_buckets` is enabled.
- Added level-based filelist scheduling.
- Ensured metadata collection finishes before objectkeys collection for each bucket.
- Updated documentation and tests.

## Remaining work

None for this task.

## Key files changed

- `AGENTS.md`
- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/obs_client.py`
- `src/obs_scan_platform/filelist_discovery.py`
- `src/obs_scan_platform/models.py`
- `src/obs_scan_platform/scanner.py`
- `config/apps.example.yaml`
- `docs/scan-start-guide.md`
- `README.md`
- `tests/test_config.py`
- `tests/test_obs_client.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

- `pytest tests/test_config.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -v`
- `pytest -q`

## Validation result

- Record exact pass/fail output from the current run.

## Known risks

- Erroneous OBS empty-bucket API failures remain treated as real failures by explicit user decision.

## Next recommended action

Review the pushed branch or continue with any newly requested scanner behavior.
```

Update `docs/handoff.md` with the exact timestamp, before/after commit hashes, test output, and push result.

- [ ] **Step 5: Review git diff**

Run:

```bash
/opt/homebrew/bin/git status
/opt/homebrew/bin/git diff --stat
/opt/homebrew/bin/git diff
```

Expected: diff includes only files listed in this plan and contains no secrets, tokens, real company credentials, `.env`, virtual environments, build artifacts, or generated dependency folders.

- [ ] **Step 6: Commit final docs or remaining integration changes**

If all earlier tasks were committed separately, make a final docs commit:

```bash
/opt/homebrew/bin/git add README.md docs/scan-start-guide.md docs/current-task.md docs/handoff.md
/opt/homebrew/bin/git commit -m "docs: update scan behavior handoff"
```

If Task 6 includes code/test changes because earlier commits were batched, use:

```bash
/opt/homebrew/bin/git add .
/opt/homebrew/bin/git commit -m "fix: complete scan behavior corrections"
```

- [ ] **Step 7: Push branch**

Run:

```bash
/opt/homebrew/bin/git push -u origin HEAD
```

Expected: push succeeds to `origin/codex/obs-scan-platform`.

- [ ] **Step 8: Final verification after push**

Run:

```bash
/opt/homebrew/bin/git status --short --branch
/opt/homebrew/bin/git rev-parse HEAD
```

Expected: branch tracks `origin/codex/obs-scan-platform` with no local changes.

---

## Self-Review Checklist

- Spec coverage:
  - Sanitized logs and request errors: Task 2.
  - Shared bucket inclusion: Task 3.
  - Level-based filelist scheduling: Task 4.
  - Metadata-before-objectkeys ordering: Task 5.
  - Global `150` and objectkeys `30` defaults: Task 1 and Task 5.
  - Empty-bucket OBS-interface failure remains out of scope: Global Constraints and Task 6 handoff.
- Scope check:
  - The plan affects one scanner subsystem and does not add database storage, API streaming, or CSV schema changes.
- Type consistency:
  - `ScanSettings.objectkeys_concurrency_limit()` is introduced in Task 1 and consumed in Task 5.
  - `RootDiscovery.metadata_files` is introduced in Task 4 and consumed in Task 5.
  - `OBSClient.get_json(..., endpoint=...)` is introduced in Task 2 and consumed by scanner tasks.
