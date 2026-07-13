# Global Bucket Concurrency and Metadata Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove application-level scan throttling, enforce one run-wide bucket limit, log per-bucket metadata task progress, and finalize each bucket's temporary directory as soon as its result is known.

**Architecture:** `Scanner.run()` creates one bucket semaphore and passes it to every concurrently started application scan. Existing application boundaries remain intact, metadata workers update local outcome counters, and bucket wrappers perform retention cleanup before releasing the shared permit. Manifest conversion no longer performs deletion.

**Tech Stack:** Python 3.11+, asyncio, Pydantic 2, httpx, pytest, pytest-asyncio, standard logging and shutil.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-13-global-bucket-concurrency-and-metadata-progress-design.md` exactly.
- Use TDD for every behavior change: demonstrate RED, add the minimum implementation, then demonstrate GREEN.
- `bucket_concurrency` is the run-wide active bucket limit across all applications.
- All enabled applications start without an application concurrency limit.
- `listbuckets` does not consume a bucket permit; every HTTP call remains constrained by `global_request_concurrency`.
- Do not add a metadata tqdm bar or change the CSV or manifest schemas.
- Preserve `metadata_concurrency_per_bucket` and `objectkeys_concurrency_per_bucket` semantics.
- Preserve historical specifications and plans; update only current operator documentation and mandatory handoff files.
- Do not commit secrets, generated results, virtual environments, or machine-specific credentials.

## File Map

- Modify `src/obs_scan_platform/config.py`: remove the active `app_concurrency` setting.
- Modify `src/obs_scan_platform/scanner.py`: share the bucket semaphore, emit metadata progress, and finalize temp directories per bucket.
- Modify `config/apps.example.yaml`: remove `app_concurrency` from the supported example.
- Modify `tests/test_config.py`: verify legacy input is ignored and modeled output omits the field.
- Modify `tests/test_scanner.py`: verify global scheduling, metadata counters, and immediate temp finalization.
- Modify `tests/test_scan_end_to_end.py`: remove direct assignments to the deleted setting and retain end-to-end regression coverage.
- Modify `README.md` and `docs/scan-start-guide.md`: document the global bucket limit and metadata log fields.
- Modify `docs/current-task.md` and `docs/handoff.md`: record implementation, validation, review, commits, and push state.

---

### Task 1: Remove Application Throttling and Share the Bucket Limit

**Files:**
- Modify: `src/obs_scan_platform/config.py:7-24`
- Modify: `src/obs_scan_platform/scanner.py:159-278`
- Modify: `config/apps.example.yaml:3-17`
- Modify: `tests/test_config.py:9-77`
- Modify: `tests/test_scanner.py:1163-1243`
- Modify: `tests/test_scan_end_to_end.py:410-430,600-615`

**Interfaces:**
- Consumes: `ScanSettings.bucket_concurrency: int` and the existing `Scanner.request_semaphore`.
- Produces: `Scanner._scan_application(..., scan_started_ms: int, bucket_semaphore: asyncio.Semaphore) -> dict[str, Any]`.
- Produces: one shared semaphore per `Scanner.run()` invocation.

- [ ] **Step 1: Establish the baseline**

Run:

```powershell
python -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all existing tests pass. Record the exact count and duration in the task notes.

- [ ] **Step 2: Write failing configuration migration tests**

Add to `tests/test_config.py`:

```python
def test_legacy_app_concurrency_is_ignored_and_omitted_from_modeled_config(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  app_concurrency: 1
  bucket_concurrency: 3
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

    assert config.scan.bucket_concurrency == 3
    assert not hasattr(config.scan, "app_concurrency")
    assert "app_concurrency" not in config.masked_dict()["scan"]
```

Also remove `app_concurrency` from the primary YAML fixture at the top of `tests/test_config.py`; compatibility is covered by the dedicated test above.

- [ ] **Step 3: Write failing run-wide scheduling tests**

Add these helpers/tests to `tests/test_scanner.py` near the existing run/application tests:

```python
def second_application() -> ApplicationConfig:
    return ApplicationConfig(
        appid="app.two",
        name="App Two",
        endpoint="http://app-two-obs.example",
        apptoken="token-2",
    )


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
        return [BucketInfo(f"{application.appid}-id", f"{application.appid}-bucket", "HEC", "cn-east-3", "owner", None)]

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
```

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_config.py::test_legacy_app_concurrency_is_ignored_and_omitted_from_modeled_config tests/test_scanner.py::test_run_applies_bucket_concurrency_globally_across_applications tests/test_scanner.py::test_run_starts_all_application_enumerations_without_app_limit -q
```

Expected: all three tests fail against the old design: the modeled field still exists, per-application bucket semaphores allow a peak above one, and `app_concurrency: 1` serializes enumeration.

- [ ] **Step 5: Implement the minimal configuration and scheduler change**

Delete this field from `ScanSettings` in `src/obs_scan_platform/config.py`:

```python
app_concurrency: int = 2
```

Replace the application semaphore block in `Scanner.run()` with one shared bucket semaphore:

```python
LOGGER.info("scan start run_id=%s applications=%s", run_id, len(applications))
bucket_semaphore = asyncio.Semaphore(self.config.scan.bucket_concurrency)

app_entries = await asyncio.gather(
    *(
        self._scan_application(
            application,
            run_id,
            results_dir,
            started_ms,
            bucket_semaphore,
        )
        for application in applications
    )
)
```

Change the `_scan_application` signature to:

```python
async def _scan_application(
    self,
    application: ApplicationConfig,
    run_id: str,
    results_dir: Path,
    scan_started_ms: int,
    bucket_semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
```

Delete the per-application semaphore construction:

```python
bucket_semaphore = asyncio.Semaphore(self.config.scan.bucket_concurrency)
```

Keep the existing `async with bucket_semaphore:` in `scan_bucket_with_limit`. Update the direct `_scan_application` call in `test_scan_application_keeps_other_buckets_after_unexpected_bucket_failure` to pass `asyncio.Semaphore(scanner.config.scan.bucket_concurrency)`.

Remove `app_concurrency` assignments from `tests/test_scan_end_to_end.py` and remove the field from `config/apps.example.yaml`.

- [ ] **Step 6: Run focused and relevant tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_config.py tests/test_scanner.py::test_run_applies_bucket_concurrency_globally_across_applications tests/test_scanner.py::test_run_starts_all_application_enumerations_without_app_limit tests/test_scanner.py::test_scan_application_keeps_other_buckets_after_unexpected_bucket_failure tests/test_scan_end_to_end.py -q
```

Expected: all selected tests pass; no test references `config.scan.app_concurrency`.

- [ ] **Step 7: Review and commit Task 1**

Run:

```powershell
git diff --check
git diff -- src/obs_scan_platform/config.py src/obs_scan_platform/scanner.py config/apps.example.yaml tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py
git add src/obs_scan_platform/config.py src/obs_scan_platform/scanner.py config/apps.example.yaml tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "feat: make bucket concurrency global"
```

Expected: one focused commit containing only configuration, scheduling, and their tests.

---

### Task 2: Log Per-Bucket Metadata Task Progress

**Files:**
- Modify: `src/obs_scan_platform/scanner.py:648-704`
- Modify: `tests/test_scanner.py:1245-1417`

**Interfaces:**
- Consumes: `_collect_metadata_files(..., object_keys: list[str], partial_errors: PartialErrorSummary | None = None) -> None`.
- Produces: `metadata start`, `metadata progress`, `metadata finish`, and `metadata skipped` log records.
- Invariant: `completed == succeeded + failed <= total`.

- [ ] **Step 1: Add a deterministic mixed-outcome test client**

Add near the existing metadata clients in `tests/test_scanner.py`:

```python
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
```

- [ ] **Step 2: Write failing metadata progress tests**

Add:

```python
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
        "metadata progress appid=app.one bucket=bucket-name-1 completed=3 total=3 succeeded=1 failed=2",
        "metadata finish appid=app.one bucket=bucket-name-1 completed=3 total=3 succeeded=1 failed=2",
    ]
    assert partial_errors.metadata_failed_files == 1


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
```

If the existing warning is captured at `WARNING` but not `INFO` due logger configuration, filter the exact warning separately and keep the progress sequence assertions exact. Do not weaken counter assertions.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_scanner.py::test_metadata_logs_progress_for_success_failure_and_invalid_response tests/test_scanner.py::test_metadata_logs_skipped_for_empty_task_list -q
```

Expected: both fail because metadata currently emits neither stage nor progress records.

- [ ] **Step 4: Implement minimal metadata counters and logs**

At the start of `_collect_metadata_files`, add:

```python
total = len(object_keys)
if total == 0:
    LOGGER.info("metadata skipped appid=%s bucket=%s total=0", application.appid, bucket.name)
    return

completed = 0
succeeded = 0
failed = 0
LOGGER.info("metadata start appid=%s bucket=%s total=%s", application.appid, bucket.name, total)
```

Change the worker so outcome accounting occurs exactly once:

```python
async def worker() -> None:
    nonlocal completed, succeeded, failed
    while True:
        try:
            object_key = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        task_succeeded = False
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
                task_succeeded = True
        except Exception as exc:
            if partial_errors is not None:
                partial_errors.record("metadata", object_key, exc, scope="object_key")
            LOGGER.warning(
                "metadata object failure appid=%s bucket=%s object_key=%s error=%s",
                application.appid,
                bucket.name,
                object_key,
                _sanitize_reason(str(exc)),
            )
        finally:
            if task_succeeded:
                succeeded += 1
            else:
                failed += 1
            completed += 1
            LOGGER.info(
                "metadata progress appid=%s bucket=%s completed=%s total=%s succeeded=%s failed=%s",
                application.appid,
                bucket.name,
                completed,
                total,
                succeeded,
                failed,
            )
            queue.task_done()
```

After workers finish, before writing rows, add:

```python
LOGGER.info(
    "metadata finish appid=%s bucket=%s completed=%s total=%s succeeded=%s failed=%s",
    application.appid,
    bucket.name,
    completed,
    total,
    succeeded,
    failed,
)
```

Do not add a progress-bar helper or modify `show_progress` behavior.

- [ ] **Step 5: Run metadata and phase-order regressions**

Run:

```powershell
python -m pytest tests/test_scanner.py -k "metadata or phase or objectkeys" -q
```

Expected: all selected tests pass, including continuation from metadata failures into objectkeys.

- [ ] **Step 6: Review and commit Task 2**

Run:

```powershell
git diff --check
git diff -- src/obs_scan_platform/scanner.py tests/test_scanner.py
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: log metadata task progress"
```

Expected: one focused progress-logging commit.

---

### Task 3: Finalize Each Bucket's Temporary Directory Immediately

**Files:**
- Modify: `src/obs_scan_platform/scanner.py:196-278,873-899`
- Modify: `tests/test_scanner.py:1163-1211,1913-1977`

**Interfaces:**
- Consumes: final `BucketScanResult`, `ScanSettings.keep_temp_files`, and `results_dir/temp_subdir/appid/bucket`.
- Produces: retention finalization before the shared bucket semaphore is released.
- Preserves: manifest `temp_dir` only when retention is enabled.

- [ ] **Step 1: Replace manifest-time cleanup tests with wrapper-finalization tests**

Replace `test_bucket_manifest_deletes_temp_dir_for_every_status_when_retention_disabled` with:

```python
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
```

- [ ] **Step 2: Write a failing timing test proving cleanup does not wait for sibling buckets**

Add:

```python
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
```

Keep the retention-enabled manifest test. Change the missing/disabled manifest test to prove serialization no longer deletes:

```python
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
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_scanner.py::test_scan_application_deletes_each_bucket_temp_dir_after_final_result tests/test_scanner.py::test_finished_bucket_temp_dir_is_deleted_before_sibling_finishes tests/test_scanner.py::test_bucket_manifest_does_not_delete_temp_dir_when_retention_disabled -q
```

Expected: wrapper-finalization tests fail because deletion still happens only during manifest conversion; serialization test fails because manifest conversion deletes the directory.

- [ ] **Step 4: Move cleanup into the bucket wrapper**

Refactor `scan_bucket_with_limit` so it assigns a result on both paths, then finalizes the directory before returning:

```python
async def scan_bucket_with_limit(bucket: BucketInfo) -> BucketScanResult:
    async with bucket_semaphore:
        started_ms = _now_ms()
        started_monotonic = time.monotonic()
        try:
            result = await self._scan_bucket(
                application,
                bucket,
                client,
                run_id,
                results_dir,
                scan_started_ms,
            )
        except Exception as exc:
            ended_ms = _now_ms()
            elapsed_seconds = time.monotonic() - started_monotonic
            LOGGER.exception(
                "bucket failure appid=%s bucket=%s error=unexpected_exception",
                application.appid,
                bucket.name,
            )
            result = BucketScanResult(
                appid=application.appid,
                bucket_name=bucket.name,
                bucket_id=bucket.bucket_id,
                status=ScanStatus.FAILED,
                csv_path=None,
                thresholds=self.config.thresholds_for(application, bucket.name),
                error=str(exc),
                started_ms=started_ms,
                ended_ms=ended_ms,
                started_at=_iso_utc(started_ms),
                ended_at=_iso_utc(ended_ms),
                elapsed_seconds=elapsed_seconds,
                request_elapsed_seconds=elapsed_seconds,
                processing_elapsed_seconds=0.0,
            )

        temp_dir = results_dir / self.config.scan.temp_subdir / application.appid / bucket.name
        if not self.config.scan.keep_temp_files and temp_dir.exists():
            shutil.rmtree(temp_dir)
        return result
```

In `_bucket_result_to_manifest`, replace cleanup behavior with serialization-only behavior:

```python
if temp_dir is not None and self.config.scan.keep_temp_files:
    manifest["temp_dir"] = str(temp_dir)
return manifest
```

Do not catch and suppress `shutil.rmtree` errors.

- [ ] **Step 5: Extend the existing unexpected-bucket regression**

In `test_scan_application_keeps_other_buckets_after_unexpected_bucket_failure`, make the fake failing bucket create its temp directory before raising, and add:

```python
assert not (tmp_path / scanner.config.scan.temp_subdir / application.appid / "bad-bucket").exists()
```

This proves the outer fallback result also reaches finalization.

- [ ] **Step 6: Run temp-retention and bucket-failure regressions**

Run:

```powershell
python -m pytest tests/test_scanner.py -k "temp or retention or unexpected_bucket or finished_bucket" -q
python -m pytest tests/test_scan_end_to_end.py -q
```

Expected: all selected tests pass; retained directories and `temp_dir` manifest entries remain unchanged when `keep_temp_files=true`.

- [ ] **Step 7: Review and commit Task 3**

Run:

```powershell
git diff --check
git diff -- src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "fix: finalize bucket temp files immediately"
```

Expected: one focused finalization commit.

---

### Task 4: Document, Review, Verify, and Publish

**Files:**
- Modify: `README.md:55-70,111-134`
- Modify: `docs/scan-start-guide.md:55-70` and its progress-log section
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: the completed configuration and scanner behavior from Tasks 1-3.
- Produces: current operator guidance and a self-contained Git handoff.

- [ ] **Step 1: Update operator documentation**

Update the concurrency sections in README and the Chinese guide to include this exact semantic content:

```yaml
scan:
  bucket_concurrency: 4
  global_request_concurrency: 150
  metadata_concurrency_per_bucket: 8
  objectkeys_concurrency_per_bucket: 30
```

Document:

- applications have no independent scan concurrency limit;
- `bucket_concurrency` is shared across all applications and covers the full bucket lifecycle through temp finalization;
- `listbuckets` does not consume bucket capacity but does consume global request capacity;
- metadata log fields are `completed`, `total`, `succeeded`, and `failed`;
- metadata `total` is the filelist-produced metadata task count;
- `total=0` produces a skipped record;
- cleanup is immediate per bucket when `keep_temp_files=false`.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass. Record the exact count and duration. Fix only failures caused by this task; document unrelated failures without hiding them.

- [ ] **Step 3: Request code review**

Invoke `superpowers:requesting-code-review`. Review the complete range from `efad134` through the implementation HEAD against the design spec. Address verified findings with TDD and rerun affected tests.

- [ ] **Step 4: Perform verification-before-completion**

Invoke `superpowers:verification-before-completion`, then freshly run:

```powershell
python -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
python -m pytest -q
```

Expected: both commands pass in fresh output after all review fixes.

- [ ] **Step 5: Update mandatory task and handoff documents**

Write `docs/current-task.md` with:

- title `Global bucket concurrency, metadata progress, and immediate temp finalization`;
- branch `codex/obs-scan-platform`;
- status `completed` only if final verification passed;
- user goals and approved semantics;
- completed and remaining work;
- exact files changed;
- exact validation commands/results;
- known risks, especially concurrent `listbuckets` request pressure and filesystem deletion failures;
- next recommended action.

Write `docs/handoff.md` with:

- current timestamp and environment;
- current branch;
- latest commit before the task: `ff5a0516096752de302b1f641bfa27ae4edea437`;
- design commit: `efad134`;
- implementation commit list;
- decisions and rationale;
- RED/GREEN evidence;
- code-review findings and fixes;
- final test status;
- uncommitted changes;
- exact resume commands for another machine.

- [ ] **Step 6: Inspect the complete diff and secrets before final commit**

Run:

```powershell
git status
git diff --stat efad134^..HEAD
git diff
git diff --check
git diff | Select-String -Pattern '(?i)(api[_-]?key|secret|password|private[_-]?key|bearer\s+[A-Za-z0-9._-]+)'
```

Expected: only requested code, tests, current docs, design/plan, and handoff changes; no secrets or unexpected machine-specific paths. Review benign fixture matches manually rather than ignoring the scan.

- [ ] **Step 7: Commit documentation and handoff**

Run:

```powershell
git add README.md docs/scan-start-guide.md docs/current-task.md docs/handoff.md docs/superpowers/plans/2026-07-13-global-bucket-concurrency-and-metadata-progress.md
git commit -m "docs: document global bucket scan controls"
```

Expected: documentation and repository handoff are committed.

- [ ] **Step 8: Finish the branch and push**

Invoke `superpowers:finishing-a-development-branch`. The user previously selected keeping the feature branch and pushing it, so do not merge into `main` or `master` and do not delete the workspace.

Run:

```powershell
git push -u origin HEAD
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

Expected: push succeeds, local and remote hashes match, and the working tree is clean. If push fails, record the exact command and error in `docs/handoff.md`, commit that handoff update if useful, and provide the manual recovery command without blind retries.

---

## Success Criteria

- `ScanSettings` no longer exposes `app_concurrency`; old YAML input remains loadable and ignored.
- All enabled applications enumerate concurrently.
- The number of active bucket scans across the run never exceeds `bucket_concurrency`.
- Every non-empty metadata stage logs start, one progress record per task, and finish with correct counters.
- Empty metadata stages log exactly one skipped record.
- Metadata task failures do not prevent later metadata tasks or objectkeys execution.
- With `keep_temp_files=false`, each bucket directory is removed immediately after its final result and before its permit is released.
- With `keep_temp_files=true`, all statuses retain temp files and manifest `temp_dir` behavior.
- Full tests and required review/verification workflows pass.
- `docs/current-task.md` and `docs/handoff.md` are complete.
- The feature branch is committed and pushed without secrets or unrelated changes.
