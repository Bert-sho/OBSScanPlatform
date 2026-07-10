# OBS Scan Interface Fallbacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement endpoint-specific OBS fallback behavior, partial bucket results, bounded `partial_errors` manifest details, and per-bucket `objectkeys` progress reporting.

**Architecture:** Keep retry behavior centralized in `OBSClient`; implement fallback decisions at scanner call sites where scope is known. Add a small partial-error accumulator in `models.py`, thread it through bucket scan phases, and use it to select `success` versus `partial_failed` after CSV aggregation.

**Tech Stack:** Python 3.11+, asyncio, httpx, pytest, tqdm, dataclasses, existing OBSScanPlatform scanner/test helpers.

## Global Constraints

- Do not add user-facing fallback configuration.
- Do not change global retry timing or concurrency defaults.
- Do not change API routes, CLI behavior, or CSV schema beyond manifest status/details.
- Preserve empty `filelist` `objects={}` and empty `objectkeys` non-error behavior.
- Use `partial_errors` as the only new manifest field for bounded local failure summaries.
- Reserve existing bucket `error` for hard failures where no complete or partial CSV is available.
- Use sanitized error strings that do not include request URLs or tokens.
- Keep `objectkeys` progress measured by prefix count, not page count.
- Do not fix unrelated Windows-specific full-suite test failures in this task.

---

## File Structure

- Modify `src/obs_scan_platform/models.py`
  - Add `PartialErrorSample` and `PartialErrorSummary`.
  - Add `partial_errors: PartialErrorSummary | None` to `BucketScanResult`.
- Modify `src/obs_scan_platform/scanner.py`
  - Pass a `PartialErrorSummary` through bucket scan phases.
  - Treat root `filelist` failures as hard failures.
  - Treat child `filelist`, per-object `metadata`, and per-prefix `objectkeys` failures as local partial errors.
  - Add `objectkeys` progress bar and logs.
  - Include `partial_errors` in bucket manifest output.
- Modify `tests/test_scanner.py`
  - Add unit tests for partial error model, manifest output, local fallback behavior, and objectkeys progress logs.
- Modify `tests/test_scan_end_to_end.py`
  - Add an end-to-end partial failure case proving CSV output and manifest status remain usable.
- Modify `docs/current-task.md` and `docs/handoff.md`
  - Record implementation status, validation commands, and follow-up state.

---

### Task 1: Partial Error Model And Manifest Output

**Files:**
- Modify: `src/obs_scan_platform/models.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Produces: `PartialErrorSample(endpoint: str, target: str, status: int | None, reason: str)`
- Produces: `PartialErrorSummary.record(endpoint: str, target: str, error: BaseException | str) -> None`
- Produces: `PartialErrorSummary.has_errors() -> bool`
- Produces: `PartialErrorSummary.to_manifest() -> dict[str, Any]`
- Produces: `BucketScanResult.partial_errors: PartialErrorSummary | None`
- Consumes: `OBSRequestError.status_code` and `OBSRequestError.reason`

- [ ] **Step 1: Write failing tests for partial error summary and manifest output**

Append these tests to `tests/test_scanner.py` near `test_bucket_manifest_temp_dir_cleanup_and_retention`:

```python
from obs_scan_platform.models import PartialErrorSummary


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
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty -q
```

Expected: both tests fail because `PartialErrorSummary` does not exist and `BucketScanResult` has no `partial_errors` field.

- [ ] **Step 3: Implement the partial error model**

In `src/obs_scan_platform/models.py`, add imports and dataclasses:

```python
from dataclasses import dataclass, field
from typing import Any

from obs_scan_platform.obs_client import OBSRequestError
```

Keep the existing `dataclass` import by replacing `from dataclasses import dataclass` with `from dataclasses import dataclass, field`.

Add these classes before `BucketScanResult`:

```python
@dataclass(frozen=True)
class PartialErrorSample:
    endpoint: str
    target: str
    status: int | None
    reason: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target": self.target,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class PartialErrorSummary:
    sample_limit: int = 10
    filelist_failed_dirs: int = 0
    metadata_failed_files: int = 0
    objectkeys_failed_prefixes: int = 0
    samples: list[PartialErrorSample] = field(default_factory=list)

    def record(self, endpoint: str, target: str, error: BaseException | str) -> None:
        if endpoint == "filelist":
            self.filelist_failed_dirs += 1
        elif endpoint == "metadata":
            self.metadata_failed_files += 1
        elif endpoint == "objectkeys":
            self.objectkeys_failed_prefixes += 1
        status = error.status_code if isinstance(error, OBSRequestError) else None
        reason = error.reason if isinstance(error, OBSRequestError) else str(error)
        if len(self.samples) < self.sample_limit:
            self.samples.append(
                PartialErrorSample(
                    endpoint=endpoint,
                    target=target,
                    status=status,
                    reason=reason[:200],
                )
            )

    def has_errors(self) -> bool:
        return (
            self.filelist_failed_dirs > 0
            or self.metadata_failed_files > 0
            or self.objectkeys_failed_prefixes > 0
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "filelist_failed_dirs": self.filelist_failed_dirs,
            "metadata_failed_files": self.metadata_failed_files,
            "objectkeys_failed_prefixes": self.objectkeys_failed_prefixes,
            "samples": [sample.to_manifest() for sample in self.samples],
        }
```

Update `BucketScanResult`:

```python
@dataclass(frozen=True)
class BucketScanResult:
    appid: str
    bucket_name: str
    bucket_id: str
    status: ScanStatus
    csv_path: Path | None
    thresholds: Thresholds
    error: str | None = None
    partial_errors: PartialErrorSummary | None = None
```

- [ ] **Step 4: Add manifest serialization**

In `src/obs_scan_platform/scanner.py`, update imports:

```python
from obs_scan_platform.models import (
    BucketInfo,
    BucketScanResult,
    ObjectRow,
    PartialErrorSummary,
    RootDiscovery,
    ScanStatus,
)
```

In `_bucket_result_to_manifest`, add this block after the base manifest dict is created:

```python
        if result.partial_errors is not None and result.partial_errors.has_errors():
            manifest["partial_errors"] = result.partial_errors.to_manifest()
```

- [ ] **Step 5: Run focused tests and commit**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty tests/test_scanner.py::test_bucket_manifest_temp_dir_cleanup_and_retention -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add src/obs_scan_platform/models.py src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: add partial scan error summaries"
```

---

### Task 2: Filelist Root Hard Failure And Child Partial Fallback

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `PartialErrorSummary.record(endpoint="filelist", target=path, error=exc)`
- Produces: `_discover_root(..., partial_errors: PartialErrorSummary | None = None) -> RootDiscovery`
- Produces: child `filelist` failure recording without scheduling descendants

- [ ] **Step 1: Add a fake client and failing filelist tests**

Append this helper and tests to `tests/test_scanner.py` near existing filelist discovery tests:

```python
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
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_discover_root_records_child_filelist_failure_and_continues tests/test_scanner.py::test_discover_root_propagates_root_filelist_failure -q
```

Expected: first test fails because `_discover_root` has no `partial_errors` parameter and child exceptions propagate.

- [ ] **Step 3: Thread partial errors through filelist discovery**

Update `_discover_root` signature:

```python
    async def _discover_root(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
        thresholds: Thresholds | None = None,
        partial_errors: PartialErrorSummary | None = None,
    ) -> RootDiscovery:
```

Update the `_process_filelist_task` call:

```python
                        self._process_filelist_task(
                            application,
                            bucket,
                            client,
                            scheduler,
                            task,
                            url,
                            progress_bar,
                            partial_errors,
                        )
```

Update `_process_filelist_task` signature:

```python
    async def _process_filelist_task(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
        scheduler: FilelistDiscoveryScheduler,
        task: FilelistTask,
        url: str,
        progress_bar: Any | None,
        partial_errors: PartialErrorSummary | None,
    ) -> None:
```

- [ ] **Step 4: Implement child fallback and progress completion**

Replace the body of `_process_filelist_task` with this structure, preserving existing parsing logic inside the `try` block:

```python
        path = task.path
        try:
            pointer = ""
            while True:
                request_body = encode_request_body(
                    {
                        "id": bucket.bucket_id,
                        "path": path,
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
                if _has_empty_filelist_objects(payload):
                    scheduler.record_empty(task)
                    break

                for item in _items_from_payload(payload, "objects", "files", "list", "items"):
                    object_type = str(item.get("objectType") or "").lower()
                    object_key = item.get("objectKey")
                    if object_type == "folder":
                        prefix = self._filelist_folder_prefix(path, object_key or item.get("name"))
                        if prefix:
                            scheduler.record_folder(task, prefix)
                            if progress_bar is not None and progress_bar.total != scheduler.pending_total_tasks:
                                progress_bar.total = scheduler.pending_total_tasks
                                progress_bar.refresh()
                    elif object_key:
                        scheduler.record_file(task, str(object_key))

                next_pointer = None
                if isinstance(payload, dict):
                    next_pointer = str(payload.get("nextOffset") or "")
                if not next_pointer or next_pointer == pointer:
                    break
                pointer = next_pointer
        except Exception as exc:
            if path == "/":
                raise
            scheduler.record_empty(task)
            if partial_errors is not None:
                partial_errors.record("filelist", path, exc)
            LOGGER.warning(
                "filelist directory failure appid=%s bucket=%s path=%s error=%s",
                application.appid,
                bucket.name,
                path,
                exc,
            )
        finally:
            scheduler.mark_completed(task)
            LOGGER.info(
                "filelist progress appid=%s bucket=%s completed=%s total=%s",
                application.appid,
                bucket.name,
                scheduler.completed_tasks,
                scheduler.pending_total_tasks,
            )
            if progress_bar is not None:
                progress_bar.update(1)
```

Remove the old duplicate `scheduler.mark_completed(...)`, `LOGGER.info(...)`, and `progress_bar.update(1)` block at the end of the method.

- [ ] **Step 5: Run filelist tests and commit**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_discover_root_records_child_filelist_failure_and_continues tests/test_scanner.py::test_discover_root_propagates_root_filelist_failure tests/test_scanner.py::test_discover_root_processes_same_filelist_level_concurrently tests/test_scanner.py::test_discover_root_progress_logs_do_not_include_request_urls -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: continue after child filelist failures"
```

---

### Task 3: Metadata Per-Object Partial Fallback

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `PartialErrorSummary.record(endpoint="metadata", target=object_key, error=exc)`
- Produces: `_collect_metadata_files(..., partial_errors: PartialErrorSummary | None = None) -> None`

- [ ] **Step 1: Write failing metadata fallback test**

Append this test near existing metadata collection tests:

```python
class MetadataPartialFailureClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        object_key = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if object_key == "bad.txt":
            raise RuntimeError("metadata unavailable")
        return {
            "result": {
                "objectKey": {
                    "objectKey": object_key,
                    "size": "12",
                    "lastModifyTime": "1000",
                }
            }
        }


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
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_records_failure_and_keeps_other_files -q
```

Expected: fails because `_collect_metadata_files` does not accept `partial_errors` and per-object exceptions abort collection.

- [ ] **Step 3: Implement per-object metadata fallback**

Update `_collect_metadata_files` signature:

```python
    async def _collect_metadata_files(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        object_keys: list[str],
        temp_dir: Path,
        client: OBSClient,
        partial_errors: PartialErrorSummary | None = None,
    ) -> None:
```

Inside the worker, replace the current `try/finally` around `queue.task_done()` with:

```python
                try:
                    data = await client.get_json(
                        _endpoint(endpoint, "/rest/boto3/s3/object/metadata"),
                        params={
                            "vendor": bucket.vendor,
                            "region": bucket.region,
                            "bucketid": bucket.name,
                            "apptoken": application.apptoken,
                            "objectkey": encode_object_key("/" + object_key.lstrip("/")),
                            "bucketld": bucket.bucket_id,
                        },
                        headers=JSON_HEADERS,
                        endpoint="metadata",
                    )
                    row = self._metadata_to_object_row(object_key, data)
                    if row is not None:
                        rows.append(row)
                except Exception as exc:
                    if partial_errors is not None:
                        partial_errors.record("metadata", object_key, exc)
                    LOGGER.warning(
                        "metadata object failure appid=%s bucket=%s object_key=%s error=%s",
                        application.appid,
                        bucket.name,
                        object_key,
                        exc,
                    )
                finally:
                    queue.task_done()
```

- [ ] **Step 4: Run metadata tests and commit**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_records_failure_and_keeps_other_files tests/test_scanner.py::test_collect_metadata_files_uses_bucket_name_as_bucketid_and_writes_csv tests/test_scanner.py::test_collect_metadata_files_processes_all_files_with_bounded_workers -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: continue after metadata object failures"
```

---

### Task 4: Objectkeys Per-Prefix Fallback And Progress

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `PartialErrorSummary.record(endpoint="objectkeys", target=prefix, error=exc)`
- Produces: `_collect_prefixes(..., partial_errors: PartialErrorSummary | None = None) -> None`
- Produces: `_objectkeys_progress_bar(application, bucket, total) -> Any | None`
- Produces: objectkeys logs for skipped, start, progress, prefix failure, and finish

- [ ] **Step 1: Write failing objectkeys fallback and progress tests**

Append these tests near existing objectkeys prefix tests:

```python
class ObjectkeysPartialFailureClient:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        self.calls.append({"url": url, "params": params, "headers": headers})
        prefix = base64.urlsafe_b64decode(params["objectkey"].encode("utf-8")).decode("utf-8").lstrip("/")
        if prefix == "bad/":
            raise RuntimeError("objectkeys unavailable")
        return {
            "result": {
                "objectkeys": [
                    {"objectKey": f"{prefix}file.txt", "size": "5", "lastModifyTime": "2000"},
                ],
                "truncated": "false",
            }
        }


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
    assert any("objectkeys finish appid=app.one bucket=bucket-name-1 completed=3 total=3 failed=1" in message for message in messages)
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
    assert progress_bar.closed
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_collect_prefixes_records_prefix_failure_and_keeps_other_prefixes tests/test_scanner.py::test_collect_prefixes_updates_objectkeys_progress_for_success_and_failure -q
```

Expected: failures because `_collect_prefixes` does not accept `partial_errors`, does not catch prefix failures, and has no `_objectkeys_progress_bar`.

- [ ] **Step 3: Add objectkeys progress helper**

Add this method near `_filelist_progress_bar`:

```python
    def _objectkeys_progress_bar(self, application: ApplicationConfig, bucket: BucketInfo, total: int) -> Any | None:
        if not self.show_progress:
            return None
        from tqdm import tqdm

        return tqdm(total=total, desc=f"{application.appid}/{bucket.name} objectkeys", unit="prefix")
```

- [ ] **Step 4: Implement per-prefix fallback and logs**

Update `_collect_prefixes` signature:

```python
    async def _collect_prefixes(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        prefixes: list[str],
        temp_dir: Path,
        client: OBSClient,
        partial_errors: PartialErrorSummary | None = None,
    ) -> None:
```

At the start of `_collect_prefixes`, add:

```python
        total = len(prefixes)
        if total == 0:
            LOGGER.info("objectkeys skipped appid=%s bucket=%s total=0", application.appid, bucket.name)
            return
        completed = 0
        failed = 0
        progress_bar = self._objectkeys_progress_bar(application, bucket, total)
        if progress_bar is not None:
            progress_bar.total = total
        LOGGER.info("objectkeys start appid=%s bucket=%s total=%s", application.appid, bucket.name, total)
```

Replace the worker body with:

```python
        async def worker() -> None:
            nonlocal completed, failed
            while True:
                try:
                    prefix = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._collect_prefix(application, bucket, endpoint, prefix, temp_dir, client)
                except Exception as exc:
                    failed += 1
                    if partial_errors is not None:
                        partial_errors.record("objectkeys", prefix, exc)
                    LOGGER.warning(
                        "objectkeys prefix failure appid=%s bucket=%s prefix=%s error=%s",
                        application.appid,
                        bucket.name,
                        prefix,
                        exc,
                    )
                finally:
                    completed += 1
                    LOGGER.info(
                        "objectkeys progress appid=%s bucket=%s completed=%s total=%s failed=%s",
                        application.appid,
                        bucket.name,
                        completed,
                        total,
                        failed,
                    )
                    if progress_bar is not None:
                        progress_bar.update(1)
                    queue.task_done()
```

Wrap worker execution in `try/finally`:

```python
        try:
            worker_count = min(max(1, self.config.scan.objectkeys_concurrency_limit()), len(prefixes))
            if worker_count:
                await asyncio.gather(*(worker() for _ in range(worker_count)))
        finally:
            if progress_bar is not None:
                progress_bar.close()
        LOGGER.info(
            "objectkeys finish appid=%s bucket=%s completed=%s total=%s failed=%s",
            application.appid,
            bucket.name,
            completed,
            total,
            failed,
        )
```

Remove the old `worker_count` block at the end of `_collect_prefixes`.

- [ ] **Step 5: Run objectkeys tests and commit**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_collect_prefixes_records_prefix_failure_and_keeps_other_prefixes tests/test_scanner.py::test_collect_prefixes_updates_objectkeys_progress_for_success_and_failure tests/test_scanner.py::test_collect_prefixes_processes_all_prefixes_with_bounded_workers tests/test_scanner.py::test_collect_prefix_stops_when_truncated_string_false -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: track objectkeys prefix progress"
```

---

### Task 5: Bucket-Level Integration And End-To-End Partial Result

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Consumes: `PartialErrorSummary.has_errors()`
- Produces: `_scan_bucket()` returns `ScanStatus.PARTIAL_FAILED` with `csv_path` and `partial_errors` when local collection failures occurred and aggregation succeeded.

- [ ] **Step 1: Write bucket integration test**

Append this test near `test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys`:

```python
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
                raise RuntimeError("prefix boom")
            return {
                "result": {
                    "objectkeys": [{"objectKey": "good/file.txt", "size": "5", "lastModifyTime": "2000"}],
                    "truncated": "false",
                }
            }
        raise AssertionError(endpoint)


@pytest.mark.asyncio
async def test_scan_bucket_returns_partial_failed_with_csv_for_objectkeys_failure(tmp_path: Path):
    scanner, application, bucket = make_scanner()
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
    assert result.error is None
    assert result.csv_path == tmp_path / application.appid / f"{bucket.name}.csv"
    assert result.csv_path.exists()
    assert result.partial_errors is not None
    assert result.partial_errors.to_manifest()["objectkeys_failed_prefixes"] == 1
```

- [ ] **Step 2: Write end-to-end manifest test**

Append to `tests/test_scan_end_to_end.py`:

```python
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
                            {"objectType": "object", "objectKey": "root.txt"},
                        ],
                        "nextOffset": "",
                    }
                }
            return {"result": {"files": [], "nextOffset": ""}}

        if url.endswith("/rest/boto3/s3/object/metadata"):
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
            prefix = _decode_base64_text(params["objectkey"]).lstrip("/")
            if prefix == "bad/":
                raise RuntimeError("prefix unavailable")
            return {
                "result": {
                    "objectkeys": [
                        {"objectKey": "good/file.txt", "size": "5", "lastModifyTime": "2000"},
                    ],
                    "truncated": "false",
                }
            }

        raise AssertionError(f"unexpected OBS URL: {url}")


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
    assert bucket["error"] is None
    assert bucket["partial_errors"]["objectkeys_failed_prefixes"] == 1
    assert bucket["partial_errors"]["samples"][0]["target"] == "bad/"
    assert csv_path.exists()
```

- [ ] **Step 3: Run the tests and verify they fail**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_scan_bucket_returns_partial_failed_with_csv_for_objectkeys_failure tests/test_scan_end_to_end.py::test_scanner_run_marks_bucket_partial_failed_and_keeps_csv -q
```

Expected: tests fail because `_scan_bucket` does not create or pass `PartialErrorSummary`, and status remains `failed` or `success`.

- [ ] **Step 4: Integrate partial errors into `_scan_bucket`**

In `_scan_bucket`, create the accumulator after `thresholds`:

```python
        partial_errors = PartialErrorSummary()
```

Update phase calls:

```python
            discovery = await self._discover_root(application, bucket, client, thresholds, partial_errors)
            await self._collect_metadata_files(
                application,
                bucket,
                endpoint,
                discovery.metadata_files,
                temp_dir,
                client,
                partial_errors,
            )
            await self._collect_prefixes(
                application,
                bucket,
                endpoint,
                discovery.prefixes,
                temp_dir,
                client,
                partial_errors,
            )
```

Before the success log and return, compute status:

```python
        status = ScanStatus.PARTIAL_FAILED if partial_errors.has_errors() else ScanStatus.SUCCESS
```

Update the finish log to use `status.value`.

Return:

```python
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=status,
            csv_path=output_path,
            thresholds=thresholds,
            partial_errors=partial_errors if partial_errors.has_errors() else None,
        )
```

- [ ] **Step 5: Run integration tests and commit**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_scan_bucket_returns_partial_failed_with_csv_for_objectkeys_failure tests/test_scan_end_to_end.py::test_scanner_run_marks_bucket_partial_failed_and_keeps_csv tests/test_scanner.py::test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys tests/test_scan_end_to_end.py::test_scanner_run_completes_with_mocked_obs_and_directory_csv -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "feat: mark partial bucket scans"
```

---

### Task 6: Validation, Docs, And Final Branch State

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: completed Tasks 1-5
- Produces: validated branch with task docs updated

- [ ] **Step 1: Run focused scanner validation**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: focused scanner tests pass.

- [ ] **Step 2: Run full validation**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

Expected: scanner changes should not introduce new failures. If the same known Windows platform failures remain, record the exact output in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 3: Update task docs**

Update `docs/current-task.md` with:

```markdown
## Current task title

Implement OBS interface fallback strategies and objectkeys progress reporting

## Task status

`completed`

## Completed work

- Added bounded `partial_errors` manifest support.
- Added child `filelist` partial fallback while preserving root `filelist` hard failure.
- Added per-object `metadata` partial fallback.
- Added per-prefix `objectkeys` partial fallback.
- Added per-bucket `objectkeys` tqdm progress and logs.
- Kept partial CSV output for locally incomplete bucket scans.

## Validation commands run

- `python -m pytest tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -q`
- `python -m pytest -q`

## Validation result

- Record the exact pass/fail summary from Step 1 and Step 2, including the number of
  passed tests and the exact names of any remaining failures.
```

Use the exact command output summary observed locally in the final `Validation result`.

- [ ] **Step 4: Update handoff docs**

Update `docs/handoff.md` with:

```markdown
## Summary of what changed

- Implemented endpoint-specific fallback behavior for the five OBS interfaces.
- Added `partial_errors` summary data to bucket manifests.
- Added objectkeys prefix progress reporting.

## Current test/build status

- Record the exact validation commands and results from this task, including the number
  of passed tests and the exact names of any remaining failures.

## Exact resume instructions for the next Codex session

1. Run `git status --short --branch`.
2. Re-run focused scanner validation if more scanner changes are requested.
3. Use the latest commit on `codex/obs-scan-platform` as the starting point.
```

Use the actual commit hashes and any remaining validation failures from the local run.

- [ ] **Step 5: Review diff for unrelated changes and secrets**

Run:

```powershell
git status --short --branch
git diff --stat
git diff
```

Expected: only scanner, tests, and docs related to this task are changed. No tokens, request URLs with credentials, `.env`, virtual environments, or generated dependency folders are staged.

- [ ] **Step 6: Commit docs and push**

Run:

```powershell
git add docs/current-task.md docs/handoff.md
git commit -m "docs: record obs fallback implementation"
git push -u origin HEAD
```

Expected: push succeeds to `origin/codex/obs-scan-platform`.

---

## Self-Review

- Spec coverage: every approved requirement maps to a task. Task 1 covers `partial_errors`; Task 2 covers root versus child `filelist`; Task 3 covers `metadata`; Task 4 covers `objectkeys` fallback, progress, and logs; Task 5 covers bucket status and end-to-end CSV/manifest behavior; Task 6 covers validation and handoff.
- Placeholder scan: no placeholder requirements remain in the executable tasks. The docs update step instructs implementers to replace validation summaries with actual observed output because those values are only known after execution.
- Type consistency: `PartialErrorSummary`, `partial_errors`, `_discover_root(..., partial_errors=...)`, `_collect_metadata_files(..., partial_errors=...)`, and `_collect_prefixes(..., partial_errors=...)` are named consistently across tasks.
