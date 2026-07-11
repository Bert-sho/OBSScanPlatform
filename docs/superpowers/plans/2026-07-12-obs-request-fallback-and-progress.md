# OBS Request Fallback and Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing first-version OBS fallback implementation with unredacted failed-attempt diagnostics, three-retry classification, detailed bucket manifest errors and timing, recoverable filelist failures, and complete objectkeys progress counters.

**Architecture:** `OBSClient` remains the transport boundary and will prepare exact request URLs, classify retries, log every failed attempt, and raise a structured final `OBSRequestError`. `Scanner` remains the business boundary and will convert only `OBSRequestError` instances into endpoint-specific partial or hard failures; `models.py` will preserve existing `partial_errors` while adding detailed request errors, timing fields, and objectkeys progress state.

**Tech Stack:** Python 3.11+, asyncio, httpx, dataclasses, Pydantic 2, tqdm, pytest, pytest-asyncio.

## Global Constraints

- Work from the current `codex/obs-scan-platform` branch at or after design commit `3fcd433`; do not recreate the first-version fallback work already present.
- Preserve all five OBS endpoint paths and request parameter names.
- Successful `httpx` and `httpcore` request URLs must remain suppressed.
- Every failed attempt must log its complete unredacted URL and at most 2048 response-body characters.
- `max_retries=3` means one initial attempt plus three retries, for four total attempts.
- Retry connection errors, timeouts, HTTP 408, HTTP 429, HTTP 5xx, `success=false`, and invalid JSON; do not retry other HTTP 4xx responses.
- `listbuckets` remains an application hard failure; `bucket_endpoint` remains a bucket hard failure.
- All `filelist` request failures, including root `/`, are recoverable bucket partial failures.
- `metadata` and `objectkeys` final request failures are local to one object and one prefix respectively.
- Preserve data obtained from successful pages before a later `filelist` or `objectkeys` page failure.
- Partial buckets must aggregate available data and generate a partial or header-only CSV.
- Preserve the final CSV schema, existing API routes, manifest `error`, and existing `partial_errors`.
- Add manifest `errors`, `started_ms`, `ended_ms`, `started_at`, `ended_at`, and `elapsed_seconds` to every bucket.
- Failure URLs and response bodies are intentionally sensitive and must never be placed in committed fixtures containing real credentials.

---

## File responsibility map

- `src/obs_scan_platform/config.py`: scanner defaults, including `max_retries=3`.
- `src/obs_scan_platform/obs_client.py`: exact request preparation, retry classification, response truncation, failed-attempt logs, and structured `OBSRequestError`.
- `src/obs_scan_platform/models.py`: detailed request error records, compatible partial summaries, objectkeys progress state, and bucket result timing.
- `src/obs_scan_platform/filelist_discovery.py`: level scheduling and retention of results from successful pages; remove rollback behavior made obsolete by the approved retention rule.
- `src/obs_scan_platform/scanner.py`: endpoint-specific fallback, timing boundaries, manifest serialization, aggregation, and objectkeys progress updates.
- `tests/test_config.py`: retry-default regression.
- `tests/test_obs_client.py`: transport, logging, response-body, and retry classification regressions.
- `tests/test_models.py`: detailed error, compatible summary, and progress state unit tests.
- `tests/test_scanner.py`: scanner fallback, timing, manifest, concurrency, and tqdm/log progress tests.
- `tests/test_scan_end_to_end.py`: multi-failure partial CSV and persisted manifest coverage.
- `config/apps.example.yaml`, `README.md`, `docs/scan-start-guide.md`: operator-facing defaults, sensitive diagnostics, partial status, timing, and progress behavior.
- `docs/current-task.md`, `docs/handoff.md`: mandatory cross-machine execution state.

---

### Task 1: Structured request diagnostics and retry classification

**Files:**
- Modify: `src/obs_scan_platform/config.py:8-23`
- Modify: `src/obs_scan_platform/obs_client.py:1-152`
- Modify: `config/apps.example.yaml:3-17`
- Test: `tests/test_config.py:54-76`
- Test: `tests/test_obs_client.py:29-397`

**Interfaces:**
- Consumes: `OBSClient.get_json(url, *, params, headers=None, endpoint="unknown")` and existing empty-result exceptions for `filelist` and `objectkeys`.
- Produces: `OBSRequestError(endpoint, status_code, reason, url, response_body, response_body_truncated, response_body_original_chars, exception_type, attempts)` and unchanged `get_json(...) -> dict[str, Any]`.

- [ ] **Step 1: Add the failing retry-default test**

Add this assertion to `test_scan_settings_new_concurrency_defaults`:

```python
assert config.scan.max_retries == 3
```

Run:

```bash
pytest tests/test_config.py::test_scan_settings_new_concurrency_defaults -q
```

Expected: FAIL because the current default is `5`.

- [ ] **Step 2: Change the retry default and example configuration**

Use the same exact value in both runtime and example configuration:

```python
class ScanSettings(BaseModel):
    max_retries: int = 3
```

```yaml
scan:
  max_retries: 3
```

Run:

```bash
pytest tests/test_config.py::test_scan_settings_new_concurrency_defaults -q
```

Expected: PASS.

- [ ] **Step 3: Replace sanitization-oriented client tests with failing structured-error tests**

Add a small test helper and explicit assertions in `tests/test_obs_client.py`:

```python
def make_client(handler, *, max_retries: int = 3) -> OBSClient:
    return OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=max_retries,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_calls"),
    [(408, 4), (429, 4), (503, 4), (400, 1), (401, 1), (403, 1), (404, 1)],
)
async def test_get_json_classifies_retryable_http_statuses(status_code: int, expected_calls: int):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="upstream failure", request=request)

    client = make_client(handler)
    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("/test", params={"token": "raw-token"}, endpoint="filelist")
    finally:
        await client.close()

    assert calls == expected_calls
    assert exc_info.value.attempts == expected_calls
```

Add separate tests for `success=false`, invalid JSON, and a timeout:

```python
@pytest.mark.asyncio
async def test_get_json_retries_success_false_and_invalid_json():
    responses = [
        httpx.Response(200, json={"success": False, "msg": "busy"}),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"success": True, "value": 1}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    client = make_client(handler)
    try:
        data = await client.get_json("/test", params={}, endpoint="metadata")
    finally:
        await client.close()

    assert data["value"] == 1
    assert responses == []


@pytest.mark.asyncio
async def test_get_json_timeout_has_full_request_context():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out", request=request)

    client = make_client(handler, max_retries=0)
    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("/test", params={"token": "raw-token"}, endpoint="metadata")
    finally:
        await client.close()

    error = exc_info.value
    assert error.status_code is None
    assert error.url == "https://obs.example/test?token=raw-token"
    assert error.response_body == "<no response>"
    assert error.exception_type == "ReadTimeout"
    assert error.attempts == 1
```

Run:

```bash
pytest tests/test_obs_client.py -q
```

Expected: FAIL because the current client does not retry 408/429 or invalid JSON and does not retain request details.

- [ ] **Step 4: Add failing failed-attempt logging and body-limit tests**

```python
@pytest.mark.asyncio
async def test_get_json_logs_unredacted_failed_attempt(caplog: pytest.LogCaptureFixture):
    body = "x" * 2050

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=body, request=request)

    client = make_client(handler, max_retries=0)
    try:
        with caplog.at_level(logging.WARNING, logger="obs_scan_platform.obs_client"):
            with pytest.raises(OBSRequestError) as exc_info:
                await client.get_json(
                    "/test",
                    params={"token": "raw-token", "requestbody": "raw-body"},
                    endpoint="objectkeys",
                )
    finally:
        await client.close()

    error = exc_info.value
    assert len(error.response_body) == 2048
    assert error.response_body_truncated is True
    assert error.response_body_original_chars == 2050
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://obs.example/test?token=raw-token&requestbody=raw-body" in message
    assert "response_body=" + ("x" * 2048) in message
    assert "response_body_truncated=true" in message
    assert "response_body_original_chars=2050" in message
    assert "attempt=1 max_attempts=1" in message
```

Run:

```bash
pytest tests/test_obs_client.py::test_get_json_logs_unredacted_failed_attempt -q
```

Expected: FAIL because failed attempts are not logged and bodies are not retained.

- [ ] **Step 5: Implement structured request errors and helpers**

In `src/obs_scan_platform/obs_client.py`, add module logging and use this error contract:

```python
import logging

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BODY_CHARS = 2048


class OBSRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        reason: str,
        url: str = "",
        response_body: str = "<no response>",
        response_body_truncated: bool = False,
        response_body_original_chars: int = 0,
        exception_type: str = "OBSRequestError",
        attempts: int = 1,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.reason = reason
        self.url = url
        self.response_body = response_body
        self.response_body_truncated = response_body_truncated
        self.response_body_original_chars = response_body_original_chars
        self.exception_type = exception_type
        self.attempts = attempts
        status = "unknown" if status_code is None else str(status_code)
        super().__init__(f"OBS request failed endpoint={endpoint} status={status} reason={reason}")


def _bounded_body(text: str) -> tuple[str, bool, int]:
    original_chars = len(text)
    return text[:MAX_RESPONSE_BODY_CHARS], original_chars > MAX_RESPONSE_BODY_CHARS, original_chars


def _is_retryable(error: OBSRequestError) -> bool:
    if error.status_code is None:
        return True
    if error.status_code in (408, 429) or error.status_code >= 500:
        return True
    return error.exception_type in {"OBSBusinessError", "InvalidJSON"}


def _log_failed_attempt(error: OBSRequestError, max_attempts: int) -> None:
    LOGGER.warning(
        "OBS request attempt failed endpoint=%s attempt=%s max_attempts=%s status=%s "
        "reason=%s url=%s response_body=%s response_body_truncated=%s "
        "response_body_original_chars=%s exception_type=%s",
        error.endpoint,
        error.attempts,
        max_attempts,
        "unknown" if error.status_code is None else error.status_code,
        error.reason,
        error.url,
        error.response_body,
        str(error.response_body_truncated).lower(),
        error.response_body_original_chars,
        error.exception_type,
    )
```

Refactor `get_json()` to prepare the exact request with `self.http.build_request("GET", ...)`, send it with `self.http.send(request)`, convert HTTP failures, JSON parse failures, `success=false`, and `httpx.RequestError` into the structure above, log every failed attempt, and either retry or raise. Preserve the two current empty-result exceptions before creating an `OBSBusinessError`.

Use this exact loop boundary:

```python
max_attempts = self.max_retries + 1
for attempt_number in range(1, max_attempts + 1):
    request = self.http.build_request("GET", url, params=params, headers=headers)
    try:
        async with self.request_semaphore:
            response = await self.http.send(request)
        if response.status_code >= 400:
            body, truncated, original_chars = _bounded_body(response.text)
            error = OBSRequestError(
                endpoint=endpoint,
                status_code=response.status_code,
                reason=_response_reason(response),
                url=str(request.url),
                response_body=body,
                response_body_truncated=truncated,
                response_body_original_chars=original_chars,
                exception_type="HTTPStatusError",
                attempts=attempt_number,
            )
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            body, truncated, original_chars = _bounded_body(response.text)
            raise OBSRequestError(
                endpoint=endpoint,
                status_code=response.status_code,
                reason=f"Invalid JSON: {exc}",
                url=str(request.url),
                response_body=body,
                response_body_truncated=truncated,
                response_body_original_chars=original_chars,
                exception_type="InvalidJSON",
                attempts=attempt_number,
            ) from exc
    except OBSRequestError as error:
        _log_failed_attempt(error, max_attempts)
        if not _is_retryable(error) or attempt_number == max_attempts:
            raise
    except httpx.RequestError as exc:
        error = OBSRequestError(
            endpoint=endpoint,
            status_code=None,
            reason=f"{exc.__class__.__name__}: {exc}",
            url=str(request.url),
            response_body="<no response>",
            response_body_truncated=False,
            response_body_original_chars=0,
            exception_type=exc.__class__.__name__,
            attempts=attempt_number,
        )
        _log_failed_attempt(error, max_attempts)
        if attempt_number == max_attempts:
            raise error from exc
```

After parsed JSON is available, keep the current empty-result checks and use this business-result block inside the same `try`:

```python
success = data.get("success")
if success is False or (isinstance(success, str) and success.lower() == "false"):
    if not _has_failure_reason(data) and endpoint == "filelist" and _has_empty_filelist_objects(data):
        return data
    if not _has_failure_reason(data) and endpoint == "objectkeys" and _has_empty_objectkeys(data):
        return data
    body, truncated, original_chars = _bounded_body(response.text)
    raise OBSRequestError(
        endpoint=endpoint,
        status_code=response.status_code,
        reason=_json_failure_reason(data),
        url=str(request.url),
        response_body=body,
        response_body_truncated=truncated,
        response_body_original_chars=original_chars,
        exception_type="OBSBusinessError",
        attempts=attempt_number,
    )
return data
```

After each retryable caught failure, preserve the existing configured backoff:

```python
delay = min(
    self.retry_max_delay_seconds,
    self.retry_base_delay_seconds * (2 ** (attempt_number - 1)),
)
if delay > 0:
    await asyncio.sleep(delay)
```

- [ ] **Step 6: Run request and config tests**

Run:

```bash
pytest tests/test_config.py tests/test_obs_client.py -q
```

Expected: all tests PASS, including empty `filelist` and empty `objectkeys` compatibility tests.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/obs_scan_platform/config.py src/obs_scan_platform/obs_client.py config/apps.example.yaml tests/test_config.py tests/test_obs_client.py
git commit -m "feat: add detailed OBS request diagnostics"
```

---

### Task 2: Detailed bucket error and progress models

**Files:**
- Modify: `src/obs_scan_platform/models.py:1-160`
- Test: `tests/test_models.py:1-end`
- Test: `tests/test_scanner.py:1432-1519`

**Interfaces:**
- Consumes: structured `OBSRequestError` from Task 1.
- Produces: `RequestFailureDetail.from_error(error, *, scope, scope_value)`, `PartialErrorSummary.record(endpoint, target, error, *, scope)`, `PartialErrorSummary.summary_text()`, `ObjectkeysProgress`, and expanded `BucketScanResult`.

- [ ] **Step 1: Write failing detailed-error model tests**

Add to `tests/test_models.py`:

```python
def request_error(endpoint: str = "objectkeys") -> OBSRequestError:
    return OBSRequestError(
        endpoint=endpoint,
        status_code=503,
        reason="Service Unavailable: busy",
        url="https://obs.example/test?token=raw-token",
        response_body='{"success":false,"msg":"busy"}',
        response_body_truncated=False,
        response_body_original_chars=30,
        exception_type="OBSBusinessError",
        attempts=4,
    )


def test_request_failure_detail_serializes_complete_request_context():
    detail = RequestFailureDetail.from_error(
        request_error(),
        scope="prefix",
        scope_value="photos/2025/",
    )

    assert detail.to_manifest() == {
        "endpoint": "objectkeys",
        "scope": "prefix",
        "scope_value": "photos/2025/",
        "url": "https://obs.example/test?token=raw-token",
        "status_code": 503,
        "reason": "Service Unavailable: busy",
        "response_body": '{"success":false,"msg":"busy"}',
        "response_body_truncated": False,
        "response_body_original_chars": 30,
        "exception_type": "OBSBusinessError",
        "attempts": 4,
    }


def test_partial_error_summary_keeps_compatible_samples_and_complete_errors():
    summary = PartialErrorSummary(sample_limit=1)
    summary.record("metadata", "root.txt", request_error("metadata"), scope="object_key")
    summary.record("objectkeys", "logs/", request_error(), scope="prefix")

    assert summary.metadata_failed_files == 1
    assert summary.objectkeys_failed_prefixes == 1
    assert len(summary.samples) == 1
    assert len(summary.errors) == 2
    assert summary.summary_text() == (
        "2 request failures; metadata=1, objectkeys=1; first: metadata "
        "object_key=root.txt status=503 reason=Service Unavailable: busy"
    )
```

Run:

```bash
pytest tests/test_models.py -q
```

Expected: FAIL because the detailed type and expanded summary do not exist.

- [ ] **Step 2: Write failing objectkeys progress model tests**

```python
def test_objectkeys_progress_tracks_pages_objects_and_prefix_outcomes():
    progress = ObjectkeysProgress(total=3)
    progress.record_page(4)
    progress.record_success()
    progress.record_page(2)
    progress.record_failure()

    assert progress.completed == 2
    assert progress.succeeded == 1
    assert progress.failed == 1
    assert progress.pages == 2
    assert progress.objects == 6
    assert progress.succeeded + progress.failed == progress.completed <= progress.total
```

Run:

```bash
pytest tests/test_models.py::test_objectkeys_progress_tracks_pages_objects_and_prefix_outcomes -q
```

Expected: FAIL because `ObjectkeysProgress` does not exist.

- [ ] **Step 3: Implement the model contracts**

Add these dataclasses to `models.py`:

```python
from collections import Counter


@dataclass(frozen=True)
class RequestFailureDetail:
    endpoint: str
    scope: str
    scope_value: str
    url: str
    status_code: int | None
    reason: str
    response_body: str
    response_body_truncated: bool
    response_body_original_chars: int
    exception_type: str
    attempts: int

    @classmethod
    def from_error(
        cls,
        error: OBSRequestError,
        *,
        scope: str,
        scope_value: str,
    ) -> "RequestFailureDetail":
        return cls(
            endpoint=error.endpoint,
            scope=scope,
            scope_value=scope_value,
            url=error.url,
            status_code=error.status_code,
            reason=error.reason,
            response_body=error.response_body,
            response_body_truncated=error.response_body_truncated,
            response_body_original_chars=error.response_body_original_chars,
            exception_type=error.exception_type,
            attempts=error.attempts,
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "scope": self.scope,
            "scope_value": self.scope_value,
            "url": self.url,
            "status_code": self.status_code,
            "reason": self.reason,
            "response_body": self.response_body,
            "response_body_truncated": self.response_body_truncated,
            "response_body_original_chars": self.response_body_original_chars,
            "exception_type": self.exception_type,
            "attempts": self.attempts,
        }


@dataclass
class ObjectkeysProgress:
    total: int
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    pages: int = 0
    objects: int = 0

    def record_page(self, object_count: int) -> None:
        self.pages += 1
        self.objects += object_count

    def record_success(self) -> None:
        self.succeeded += 1
        self.completed += 1

    def record_failure(self) -> None:
        self.failed += 1
        self.completed += 1
```

Extend `PartialErrorSummary` with `errors: list[RequestFailureDetail]`. Keep temporary source compatibility with existing direct callers by accepting strings and generic exceptions, but append a detailed error only for `OBSRequestError`; Task 3 removes generic scanner fallback. Keep the current bounded sanitized samples:

```python
errors: list[RequestFailureDetail] = field(default_factory=list)

def record(
    self,
    endpoint: str,
    target: str,
    error: BaseException | str,
    *,
    scope: str = "target",
) -> None:
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
                reason=_sanitize_reason(reason)[:200],
            )
        )
    if isinstance(error, OBSRequestError):
        self.errors.append(RequestFailureDetail.from_error(error, scope=scope, scope_value=target))

def summary_text(self) -> str | None:
    if not self.errors:
        return None
    counts = Counter(detail.endpoint for detail in self.errors)
    count_text = ", ".join(f"{endpoint}={counts[endpoint]}" for endpoint in sorted(counts))
    first = self.errors[0]
    status = "unknown" if first.status_code is None else str(first.status_code)
    return (
        f"{len(self.errors)} request failures; {count_text}; first: {first.endpoint} "
        f"{first.scope}={first.scope_value} status={status} reason={first.reason}"
    )
```

Extend `BucketScanResult` with defaults so existing direct test construction stays source-compatible:

```python
errors: list[RequestFailureDetail] = field(default_factory=list)
started_ms: int = 0
ended_ms: int = 0
started_at: str = ""
ended_at: str = ""
elapsed_seconds: float = 0.0
```

- [ ] **Step 4: Update existing summary tests for the new typed input**

Replace string and generic `RuntimeError` inputs in summary tests with the `request_error()` helper. Keep assertions proving `partial_errors.samples` stays capped and sanitized, while `summary.errors` retains the full URL/body for the new manifest field.

Run:

```bash
pytest tests/test_models.py tests/test_scanner.py -q
```

Expected: existing scanner tests may still fail at call sites that have not yet passed `scope`; all model tests PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/obs_scan_platform/models.py tests/test_models.py tests/test_scanner.py
git commit -m "feat: model detailed bucket request failures"
```

---

### Task 3: Endpoint fallback, partial CSV, bucket timing, and manifest serialization

**Files:**
- Modify: `src/obs_scan_platform/filelist_discovery.py:58-83`
- Modify: `src/obs_scan_platform/scanner.py:140-520,751-769`
- Test: `tests/test_scanner.py:67-136,495-548,898-1031,1280-1519`

**Interfaces:**
- Consumes: `RequestFailureDetail`, typed `PartialErrorSummary.record(...)`, and expanded `BucketScanResult` from Task 2.
- Produces: recoverable root/child filelist behavior, request-only local catches, complete bucket timing, `error` summary, `partial_errors`, and `errors` manifest fields.

- [ ] **Step 1: Replace the root hard-failure test with a failing partial-result test**

Make `FailingRootFilelistClient` raise a structured `OBSRequestError`, then replace `test_discover_root_propagates_root_filelist_failure` with:

```python
@pytest.mark.asyncio
async def test_discover_root_records_root_request_failure_and_returns_empty_discovery():
    scanner, application, bucket = make_scanner()
    errors = PartialErrorSummary()

    discovery = await scanner._discover_root(
        application,
        bucket,
        FailingRootFilelistClient(),
        partial_errors=errors,
    )

    assert discovery.prefixes == []
    assert discovery.metadata_files == []
    assert errors.filelist_failed_dirs == 1
    assert errors.errors[0].scope == "directory"
    assert errors.errors[0].scope_value == "/"
```

Rename `test_discover_root_prunes_partial_child_filelist_results_after_paginated_failure` to `test_discover_root_preserves_successful_child_filelist_pages_after_later_failure` and assert page-one discoveries remain instead of being pruned:

```python
assert discovery.prefixes == ["alpha/", "bravo/"]
assert "alpha/page-one.txt" not in discovery.metadata_files
```

The direct file is covered by the retained `alpha/` objectkeys prefix.

Run:

```bash
pytest tests/test_scanner.py::test_discover_root_records_root_request_failure_and_returns_empty_discovery tests/test_scanner.py::test_discover_root_preserves_successful_child_filelist_pages_after_later_failure -q
```

Expected: FAIL because root currently re-raises and child failure currently rolls back successful page data.

- [ ] **Step 2: Prove unexpected exceptions are not downgraded**

Change recoverable fake clients to raise `OBSRequestError`. Add separate clients raising `RuntimeError` and tests:

```python
@pytest.mark.asyncio
async def test_metadata_unexpected_exception_propagates(tmp_path: Path):
    scanner, application, bucket = make_scanner()
    with pytest.raises(RuntimeError, match="metadata parser bug"):
        await scanner._collect_metadata_files(
            application,
            bucket,
            "http://bucket-endpoint",
            ["bad.txt"],
            tmp_path,
            UnexpectedMetadataClient(),
            partial_errors=PartialErrorSummary(),
        )


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
```

Run both tests. Expected: FAIL because current workers catch every `Exception`.

- [ ] **Step 3: Implement request-only local fallback and successful-page retention**

In `_process_filelist_task`, replace the generic catch and root re-raise with:

```python
except OBSRequestError as exc:
    if partial_errors is not None:
        partial_errors.record("filelist", path, exc, scope="directory")
    LOGGER.warning(
        "filelist directory failure appid=%s bucket=%s path=%s status=%s reason=%s",
        application.appid,
        bucket.name,
        path,
        "unknown" if exc.status_code is None else exc.status_code,
        exc.reason,
    )
```

Do not call `rollback_failed_task`; already recorded page data and queued descendants remain valid partial results. Remove `rollback_failed_task()` from `filelist_discovery.py` after confirming `rg -n "rollback_failed_task"` has no remaining call sites.

In metadata and objectkeys workers, catch only `OBSRequestError`, call typed `record` with `scope="object_key"` or `scope="prefix"`, and allow all other exceptions to escape to `_scan_bucket`.

- [ ] **Step 4: Add failing bucket timing and hard endpoint detail tests**

First preserve application isolation with a `listbuckets` regression. Monkeypatch `_list_buckets` to raise a structured request error only for `app.bad`, run two enabled applications, and assert:

```python
assert manifest["status"] == "partial_failed"
assert manifest["applications"][0]["status"] == "failed"
assert manifest["applications"][0]["buckets"] == []
assert manifest["applications"][1]["status"] == "success"
```

Add a direct `bucket_endpoint` request failure test asserting the failed bucket has `csv_path=None`, other bucket tests still pass, and its detailed request error uses `scope="bucket"` and `scope_value=bucket.name`.

Add deterministic clock tests by monkeypatching `obs_scan_platform.scanner._now_ms` and `time.monotonic`:

```python
@pytest.mark.asyncio
async def test_bucket_result_records_start_end_and_elapsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scanner, application, bucket = make_scanner()
    wall_times = iter([1_000, 4_000])
    monotonic_times = iter([10.0, 13.25])
    monkeypatch.setattr("obs_scan_platform.scanner._now_ms", lambda: next(wall_times))
    monkeypatch.setattr("obs_scan_platform.scanner.time.monotonic", lambda: next(monotonic_times))

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
```

For the bucket endpoint result, also assert `status=failed`, a detailed `error` string, one `errors` item, and all timing fields.

Run the new tests. Expected: FAIL because timing and detailed hard errors are not populated.

- [ ] **Step 5: Implement bucket timing and result assembly**

Add this formatter near `_now_ms()`:

```python
def _iso_utc(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
```

At `_scan_bucket` entry capture both clocks. At every return capture end values and populate the expanded result fields. For a partial success use:

```python
error=partial_errors.summary_text(),
partial_errors=partial_errors if partial_errors.has_errors() else None,
errors=list(partial_errors.errors),
started_ms=started_ms,
ended_ms=ended_ms,
started_at=_iso_utc(started_ms),
ended_at=_iso_utc(ended_ms),
elapsed_seconds=elapsed_seconds,
```

Catch `OBSRequestError` at the bucket hard-failure boundary to create one `RequestFailureDetail` with `scope="bucket"` and `scope_value=bucket.name`. Keep a separate generic `except Exception` for programming, aggregation, and filesystem failures; these hard-fail the bucket without fabricating a request error.

In the outer `_scan_application.scan_bucket_with_limit` safety catch, measure wall and monotonic time around the delegated call so even a monkeypatched or unexpectedly escaping bucket failure receives timing fields.

- [ ] **Step 6: Serialize all bucket fields compatibly**

Extend `_bucket_result_to_manifest`:

```python
manifest = {
    "bucket_name": result.bucket_name,
    "bucket_id": result.bucket_id,
    "status": result.status.value,
    "csv_path": str(result.csv_path) if result.csv_path is not None else None,
    "thresholds": result.thresholds.model_dump(mode="json"),
    "error": result.error,
    "errors": [error.to_manifest() for error in result.errors],
    "started_ms": result.started_ms,
    "ended_ms": result.ended_ms,
    "started_at": result.started_at,
    "ended_at": result.ended_at,
    "elapsed_seconds": result.elapsed_seconds,
}
```

Keep existing conditional `partial_errors` and temp cleanup behavior unchanged.

- [ ] **Step 7: Run focused fallback and manifest tests**

```bash
pytest tests/test_models.py tests/test_scanner.py -q
```

Expected: all tests PASS. Confirm the old URL-redaction tests now distinguish request-layer raw failure logs from scanner progress logs: successful URLs remain absent, failed-attempt logs are raw by design.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/obs_scan_platform/filelist_discovery.py src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: refine OBS endpoint fallback and bucket timing"
```

---

### Task 4: Complete objectkeys progress counters and tqdm display

**Files:**
- Modify: `src/obs_scan_platform/scanner.py:528-533,604-723`
- Test: `tests/test_scanner.py:169-184,1192-1277`

**Interfaces:**
- Consumes: `ObjectkeysProgress` from Task 2 and typed request fallback from Task 3.
- Produces: `_collect_prefix(..., progress: ObjectkeysProgress | None = None) -> None` and progress logs/postfix containing `completed`, `total`, `succeeded`, `failed`, `pages`, and `objects`.

- [ ] **Step 1: Expand the fake tqdm object and write failing postfix assertions**

Extend `DummyProgressBar`:

```python
def __init__(self):
    self.total = 0
    self.updates: list[int] = []
    self.refreshes = 0
    self.closed = False
    self.postfixes: list[dict[str, int]] = []

def set_postfix(self, values: dict[str, int], *, refresh: bool = False) -> None:
    self.postfixes.append(dict(values))
```

Update `test_collect_prefixes_updates_objectkeys_progress_for_success_and_failure` to assert:

```python
assert progress_bar.postfixes[-1] == {
    "succeeded": 1,
    "failed": 1,
    "pages": 1,
    "objects": 1,
}
```

Run the test. Expected: FAIL because scanner never sets a postfix or tracks page/object totals.

- [ ] **Step 2: Add a failing paginated partial-prefix progress test**

Create a fake client where `partial/` returns one successful page with two objects and then raises `OBSRequestError` on page two, while `good/` returns one page with one object. Assert the final log contains:

```text
completed=2 total=2 succeeded=1 failed=1 pages=2 objects=3
```

Also assert all three successful rows exist in temp CSV files, proving page-one data survives the later failure.

Run:

```bash
pytest tests/test_scanner.py::test_objectkeys_progress_keeps_successful_pages_from_failed_prefix -q
```

Expected: FAIL because current `_collect_prefix` does not report page/object counts.

- [ ] **Step 3: Pass shared progress state through objectkeys pagination**

Change `_collect_prefix` to accept `progress: ObjectkeysProgress | None = None`, preserving existing direct call compatibility. After each successfully parsed page and after writing valid rows, call:

```python
if progress is not None:
    progress.record_page(len(rows))
```

This update happens before evaluating `truncated`, so pages from a prefix that fails later remain counted.

In `_collect_prefixes`, replace integer locals with:

```python
progress = ObjectkeysProgress(total=len(prefixes))
```

For a successful prefix call `progress.record_success()`. For a caught `OBSRequestError`, record the partial error and call `progress.record_failure()`. Unexpected exceptions must propagate without being counted as a completed request task.

- [ ] **Step 4: Centralize log and tqdm updates after each request-task outcome**

After success or recoverable failure, emit:

```python
LOGGER.info(
    "objectkeys progress appid=%s bucket=%s completed=%s total=%s "
    "succeeded=%s failed=%s pages=%s objects=%s",
    application.appid,
    bucket.name,
    progress.completed,
    progress.total,
    progress.succeeded,
    progress.failed,
    progress.pages,
    progress.objects,
)
```

Update tqdm with:

```python
progress_bar.set_postfix(
    {
        "succeeded": progress.succeeded,
        "failed": progress.failed,
        "pages": progress.pages,
        "objects": progress.objects,
    },
    refresh=False,
)
progress_bar.update(1)
```

Use the same six counters in the finish log. Preserve `objectkeys skipped ... total=0` and do not create a bar for empty prefix lists.

- [ ] **Step 5: Run objectkeys and scanner regression tests**

```bash
pytest tests/test_scanner.py -q
```

Expected: all tests PASS and the invariant `succeeded + failed == completed <= total` holds in every asserted progress line.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: expand objectkeys scan progress"
```

---

### Task 5: End-to-end partial scan, operator documentation, and final handoff

**Files:**
- Modify: `tests/test_scan_end_to_end.py:272-end`
- Modify: `README.md:71-136`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: completed request, model, scanner, manifest, and progress behavior from Tasks 1-4.
- Produces: persisted end-to-end proof, operator guidance, mandatory task state, and cross-machine resume instructions.

- [ ] **Step 1: Build a multi-failure end-to-end fake using structured request errors**

Replace `RuntimeError` partial failures with `OBSRequestError` values containing synthetic URLs and bodies. Make one bucket experience:

- one root or child `filelist` final request failure;
- one metadata object final request failure;
- one objectkeys prefix failure after a successful first page;
- at least one successful metadata object and one successful objectkeys prefix.

Use only synthetic values such as `token=test-token`; never use real credentials.

- [ ] **Step 2: Add persisted manifest and CSV integration assertions**

Extend `test_scanner_run_marks_bucket_partial_failed_and_keeps_csv`:

```python
assert bucket["status"] == "partial_failed"
assert bucket["csv_path"] == str(csv_path)
assert bucket["error"].startswith("3 request failures;")
assert len(bucket["errors"]) == 3
assert {error["endpoint"] for error in bucket["errors"]} == {"filelist", "metadata", "objectkeys"}
assert all(error["url"].startswith("https://") for error in bucket["errors"])
assert all("response_body" in error for error in bucket["errors"])
assert bucket["partial_errors"]["filelist_failed_dirs"] == 1
assert bucket["partial_errors"]["metadata_failed_files"] == 1
assert bucket["partial_errors"]["objectkeys_failed_prefixes"] == 1
assert bucket["started_ms"] <= bucket["ended_ms"]
assert bucket["started_at"].endswith("Z")
assert bucket["ended_at"].endswith("Z")
assert bucket["elapsed_seconds"] >= 0
assert csv_path.exists()
```

Read the persisted `manifest.json` and assert the persisted bucket equals the returned bucket. Assert the CSV contains successful partial rows and excludes failed-only objects.

Run:

```bash
pytest tests/test_scan_end_to_end.py::test_scanner_run_marks_bucket_partial_failed_and_keeps_csv -q
```

Expected: PASS only after Tasks 1-4 are complete.

- [ ] **Step 3: Run targeted scanner validation**

```bash
pytest tests/test_config.py tests/test_obs_client.py tests/test_models.py tests/test_scanner.py tests/test_scan_end_to_end.py -v
```

Expected: all targeted tests PASS.

- [ ] **Step 4: Update operator documentation**

Document these exact behaviors in `README.md` and `docs/scan-start-guide.md`:

- normal successful request URLs are suppressed;
- failed attempts log unredacted URLs and up to 2048 response characters;
- logs and manifests must be handled as sensitive data;
- default retries are three after the initial request;
- the five endpoint fallback boundaries;
- partial CSV interpretation using `status`, `error`, `partial_errors`, and `errors`;
- bucket start/end/elapsed fields;
- objectkeys prefix progress with succeeded, failed, pages, and objects;
- CLI shows tqdm while API scans write the same progress to `scan.log` without a terminal bar.

Do not claim partial results are complete or safe to consume without checking status.

- [ ] **Step 5: Run the full suite on macOS**

```bash
pytest -q
```

Expected: all tests PASS. If any of the six previously documented Windows-only assumptions fail on macOS, investigate them as current failures rather than copying the old Windows waiver.

- [ ] **Step 6: Update mandatory task and handoff documents**

Set `docs/current-task.md` to `completed` only if targeted and full validation pass. Record:

- current branch;
- all commits created by Tasks 1-4;
- exact test commands and counts;
- changed files;
- sensitive-output risk;
- remaining work, if any.

Update `docs/handoff.md` with:

- current timestamp and macOS environment;
- branch and latest pre-session commit;
- summary and rationale;
- rejected approaches and failed attempts;
- exact validation output;
- uncommitted state;
- exact cross-machine resume commands.

- [ ] **Step 7: Review the final diff and secret boundary**

```bash
git status
git diff --stat
git diff
git diff --check
rg -n "raw-token|raw-body|test-token" src config README.md docs tests
```

Expected: only synthetic test/documentation values are present; no real tokens, `.env`, databases, temp CSVs, results, `__pycache__`, or virtual environments are staged.

- [ ] **Step 8: Commit final integration and documentation**

```bash
git add tests/test_scan_end_to_end.py README.md docs/scan-start-guide.md docs/current-task.md docs/handoff.md
git commit -m "docs: finalize OBS fallback diagnostics handoff"
```

- [ ] **Step 9: Push and verify the remote tip**

```bash
git push -u origin HEAD
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

Expected: push succeeds, the two hashes match, and the worktree is clean.

---

## Final review checklist

- [ ] Every design requirement maps to at least one task and one test.
- [ ] No successful request URL appears in normal CLI or `scan.log` output.
- [ ] Every failed attempt includes full URL, response details, and attempt counters.
- [ ] Empty `success=false` filelist/objectkeys compatibility remains intact.
- [ ] Only `OBSRequestError` is locally downgraded; unrelated exceptions hard-fail.
- [ ] Root and child filelist failures preserve successful page data and yield partial CSVs.
- [ ] Manifest retains `partial_errors`, adds complete `errors`, and populates partial `error` summaries.
- [ ] Every bucket state contains start/end/elapsed timing.
- [ ] Objectkeys tqdm and logs report completed/total/succeeded/failed/pages/objects.
- [ ] Targeted and full macOS tests pass before completion is claimed.
- [ ] Mandatory handoff files are current, committed, and pushed.
