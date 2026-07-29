# Global Aggregation Request Barrier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize all CSV and Parquet bucket aggregations globally within one scan run, drain admitted HTTP attempts, and pause new attempts/retries until the complete queued aggregation batch finishes.

**Architecture:** Add a writer-preferred `ScanPhaseCoordinator` that owns both global request capacity and request-versus-aggregation phase state. `OBSClient` places each individual HTTP attempt inside a reader scope through response validation and JSON parsing; `Scanner` places the complete CSV/Parquet format dispatch inside a writer scope, so all queued writers run serially before readers resume.

**Tech Stack:** Python 3.11+, asyncio, contextlib async context managers, HTTPX, pytest, pytest-asyncio, Git

## Global Constraints

- Global aggregation concurrency is fixed at one per `Scanner.run` across every application, bucket, and overview format.
- Once any aggregation waits, no new HTTP attempt or retry may start.
- Already admitted attempts must finish response reading, status/business validation, and JSON parsing before aggregation begins.
- Existing scanner call sites must synchronously consume the returned payload before their next `await`, completing current-page conversion and objectkeys CSV append before aggregation can run.
- All queued CSV and Parquet aggregations run consecutively before request admission reopens.
- Paused request tasks resume after aggregation; they are not cancelled or discarded.
- Preserve request concurrency, retry counts/delays, timeouts, keep-alive limits, bucket concurrency, payloads, pagination, outputs, and partial-failure behavior.
- Request, aggregation, and waiter cancellation must not leak capacity or leave the gate closed.
- Do not add a YAML option for aggregation concurrency and do not coordinate separate processes or independent scan runs.
- Update `docs/current-task.md` and `docs/handoff.md`, review the full diff, commit on `codex/parquet-overview`, and push that branch rather than `main` or `master`.

---

### Task 1: Implement the writer-preferred scan phase coordinator

**Files:**
- Create: `src/obs_scan_platform/scan_coordination.py`
- Create: `tests/test_scan_coordination.py`

**Interfaces:**
- Consumes: positive `max_concurrent_requests: int`.
- Produces: `ScanPhaseCoordinator.request_attempt()` and `ScanPhaseCoordinator.aggregation()`, both async context managers returning `None`.
- Guarantees: readers are bounded, waiting writers close reader admission, writers are mutually exclusive, and all state is released in `finally`.

- [ ] **Step 1: Write failing coordinator ordering tests**

Create `tests/test_scan_coordination.py` with deterministic event-controlled tests. The first test protects drain-before-write and writer priority:

```python
import asyncio

import pytest

from obs_scan_platform.scan_coordination import ScanPhaseCoordinator


@pytest.mark.asyncio
async def test_aggregation_drains_active_request_and_blocks_late_request():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=2)
    active_entered = asyncio.Event()
    release_active = asyncio.Event()
    aggregation_entered = asyncio.Event()
    release_aggregation = asyncio.Event()
    late_entered = asyncio.Event()
    events: list[str] = []

    async def active_request() -> None:
        async with coordinator.request_attempt():
            events.append("active-request-enter")
            active_entered.set()
            await release_active.wait()
            events.append("active-request-exit")

    async def aggregate() -> None:
        async with coordinator.aggregation():
            events.append("aggregation-enter")
            aggregation_entered.set()
            await release_aggregation.wait()
            events.append("aggregation-exit")

    async def late_request() -> None:
        async with coordinator.request_attempt():
            events.append("late-request-enter")
            late_entered.set()

    active_task = asyncio.create_task(active_request())
    await active_entered.wait()
    aggregation_task = asyncio.create_task(aggregate())
    await asyncio.sleep(0)
    late_task = asyncio.create_task(late_request())
    await asyncio.sleep(0)

    assert not aggregation_entered.is_set()
    assert not late_entered.is_set()

    release_active.set()
    await aggregation_entered.wait()
    assert not late_entered.is_set()

    release_aggregation.set()
    await asyncio.gather(active_task, aggregation_task, late_task)

    assert events == [
        "active-request-enter",
        "active-request-exit",
        "aggregation-enter",
        "aggregation-exit",
        "late-request-enter",
    ]
```

Add a second test that proves a batch of writers remains closed to readers:

```python
@pytest.mark.asyncio
async def test_queued_aggregations_run_serially_before_requests_resume():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=3)
    active_request_entered = asyncio.Event()
    release_active_request = asyncio.Event()
    aggregation_entries: asyncio.Queue[str] = asyncio.Queue()
    aggregation_releases = {
        "aggregation-1": asyncio.Event(),
        "aggregation-2": asyncio.Event(),
    }
    late_request_entered = asyncio.Event()
    active_aggregations = 0
    max_active_aggregations = 0

    async def active_request() -> None:
        async with coordinator.request_attempt():
            active_request_entered.set()
            await release_active_request.wait()

    async def aggregate(name: str) -> None:
        nonlocal active_aggregations, max_active_aggregations
        async with coordinator.aggregation():
            active_aggregations += 1
            max_active_aggregations = max(max_active_aggregations, active_aggregations)
            aggregation_entries.put_nowait(name)
            await aggregation_releases[name].wait()
            active_aggregations -= 1

    async def late_request() -> None:
        async with coordinator.request_attempt():
            late_request_entered.set()

    active_task = asyncio.create_task(active_request())
    await active_request_entered.wait()
    aggregation_tasks = [
        asyncio.create_task(aggregate("aggregation-1")),
        asyncio.create_task(aggregate("aggregation-2")),
    ]
    await asyncio.sleep(0)
    late_task = asyncio.create_task(late_request())
    await asyncio.sleep(0)

    release_active_request.set()
    first_name = await aggregation_entries.get()
    assert not late_request_entered.is_set()
    aggregation_releases[first_name].set()
    second_name = await aggregation_entries.get()
    assert {first_name, second_name} == {"aggregation-1", "aggregation-2"}
    assert not late_request_entered.is_set()
    aggregation_releases[second_name].set()

    await asyncio.gather(active_task, *aggregation_tasks, late_task)
    assert max_active_aggregations == 1
    assert late_request_entered.is_set()
```

Add cancellation/error tests with literal assertions:

```python
@pytest.mark.asyncio
async def test_cancelled_aggregation_waiter_reopens_request_admission():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)
    release_request = asyncio.Event()
    request_entered = asyncio.Event()

    async def active_request() -> None:
        async with coordinator.request_attempt():
            request_entered.set()
            await release_request.wait()

    active_task = asyncio.create_task(active_request())
    await request_entered.wait()
    waiting_writer = asyncio.create_task(_enter_aggregation(coordinator))
    await asyncio.sleep(0)
    waiting_writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_writer
    release_request.set()
    await active_task

    async with coordinator.request_attempt():
        admitted_after_cancel = True

    assert admitted_after_cancel is True


async def _enter_aggregation(coordinator: ScanPhaseCoordinator) -> None:
    async with coordinator.aggregation():
        return
```

Protect both active-context cleanup paths:

```python
@pytest.mark.asyncio
async def test_aggregation_error_reopens_request_admission():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)

    with pytest.raises(RuntimeError, match="aggregation failed"):
        async with coordinator.aggregation():
            raise RuntimeError("aggregation failed")

    async with coordinator.request_attempt():
        admitted_after_error = True

    assert admitted_after_error is True


@pytest.mark.asyncio
async def test_cancelled_active_request_allows_aggregation():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)
    request_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def request() -> None:
        async with coordinator.request_attempt():
            request_entered.set()
            await never_release.wait()

    request_task = asyncio.create_task(request())
    await request_entered.wait()
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    async with coordinator.aggregation():
        aggregation_entered = True

    assert aggregation_entered is True
```

- [ ] **Step 2: Run coordinator tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py -q
```

Expected: collection fails with `ModuleNotFoundError` because
`obs_scan_platform.scan_coordination` does not exist.

- [ ] **Step 3: Implement `ScanPhaseCoordinator` minimally**

Create `src/obs_scan_platform/scan_coordination.py`:

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ScanPhaseCoordinator:
    def __init__(self, max_concurrent_requests: int) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be at least 1")
        self._request_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._condition = asyncio.Condition()
        self._active_request_attempts = 0
        self._waiting_aggregations = 0
        self._aggregation_active = False

    @asynccontextmanager
    async def request_attempt(self) -> AsyncIterator[None]:
        async with self._request_semaphore:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._aggregation_active
                    and self._waiting_aggregations == 0
                )
                self._active_request_attempts += 1
            try:
                yield
            finally:
                async with self._condition:
                    self._active_request_attempts -= 1
                    if self._active_request_attempts == 0:
                        self._condition.notify_all()

    @asynccontextmanager
    async def aggregation(self) -> AsyncIterator[None]:
        admitted = False
        async with self._condition:
            self._waiting_aggregations += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._aggregation_active
                    and self._active_request_attempts == 0
                )
                self._aggregation_active = True
                admitted = True
            finally:
                self._waiting_aggregations -= 1
                if not admitted:
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._aggregation_active = False
                self._condition.notify_all()
```

- [ ] **Step 4: Run coordinator tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py -q
```

Expected: every coordinator ordering, serialization, error, and cancellation test passes with no warning.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/obs_scan_platform/scan_coordination.py tests/test_scan_coordination.py
git commit -m "feat: coordinate scan request and aggregation phases"
```

---

### Task 2: Route every OBS HTTP attempt through the coordinator

**Files:**
- Modify: `src/obs_scan_platform/obs_client.py`
- Modify: `tests/test_obs_client.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `ScanPhaseCoordinator` from Task 1.
- Changes: `OBSClient.__init__` replaces `request_semaphore` with required `phase_coordinator: ScanPhaseCoordinator`.
- Produces: one request reader scope per retry attempt, covering send, response validation, bounded body access, JSON parsing, and OBS business validation.

- [ ] **Step 1: Write failing request-scope tests**

Update the test imports and client factory:

```python
from obs_scan_platform.scan_coordination import ScanPhaseCoordinator


def make_client(
    handler,
    *,
    max_retries: int = 3,
    retry_base_delay_seconds: float = 0,
    phase_coordinator: ScanPhaseCoordinator | None = None,
) -> OBSClient:
    return OBSClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://obs.example",
        ),
        phase_coordinator=phase_coordinator or ScanPhaseCoordinator(1),
        max_retries=max_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
        retry_max_delay_seconds=retry_base_delay_seconds,
    )
```

Add a recording subclass and verify JSON parsing is inside the request scope:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RecordingCoordinator(ScanPhaseCoordinator):
    def __init__(self, events: list[str]) -> None:
        super().__init__(1)
        self.events = events

    @asynccontextmanager
    async def request_attempt(self) -> AsyncIterator[None]:
        self.events.append("scope-enter")
        async with super().request_attempt():
            yield
        self.events.append("scope-exit")


@pytest.mark.asyncio
async def test_get_json_parses_response_inside_request_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[str] = []
    original_json = httpx.Response.json

    def recording_json(response: httpx.Response, **kwargs):
        events.append("json")
        return original_json(response, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        events.append("send")
        return httpx.Response(200, json={"value": 1}, request=request)

    monkeypatch.setattr(httpx.Response, "json", recording_json)
    client = make_client(handler, phase_coordinator=RecordingCoordinator(events))
    try:
        assert await client.get_json("/test", params={}) == {"value": 1}
    finally:
        await client.close()

    assert events == ["scope-enter", "send", "json", "scope-exit"]
```

Add a retry-pause test using a real coordinator:

```python
@pytest.mark.asyncio
async def test_get_json_retry_waits_for_aggregation():
    coordinator = ScanPhaseCoordinator(1)
    first_attempt_seen = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_attempt_seen.set()
            return httpx.Response(503, json={"success": False, "msg": "busy"})
        return httpx.Response(200, json={"success": True, "value": 1})

    client = make_client(
        handler,
        max_retries=1,
        retry_base_delay_seconds=0.05,
        phase_coordinator=coordinator,
    )
    request_task = asyncio.create_task(client.get_json("/test", params={}))
    try:
        await first_attempt_seen.wait()
        await asyncio.sleep(0)
        async with coordinator.aggregation():
            await asyncio.sleep(0.06)
            assert calls == 1

        data = await request_task
    finally:
        await client.close()

    assert data["value"] == 1
    assert calls == 2
```

- [ ] **Step 2: Run the new OBSClient tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_obs_client.py::test_get_json_parses_response_inside_request_attempt tests/test_obs_client.py::test_get_json_retry_waits_for_aggregation -q
```

Expected: tests fail because `OBSClient` does not accept `phase_coordinator` and still requires `request_semaphore`.

- [ ] **Step 3: Replace the semaphore dependency and scope each attempt**

Import `ScanPhaseCoordinator`, change the constructor field, and place all
attempt-specific work inside the coordinator scope:

```python
class OBSClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        phase_coordinator: ScanPhaseCoordinator,
        max_retries: int,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
    ) -> None:
        self.http = http
        self.phase_coordinator = phase_coordinator
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
```

Inside each `for attempt_number` iteration, retain the existing request
construction and error objects but use this exact control-flow boundary:

```python
try:
    async with self.phase_coordinator.request_attempt():
        response = await self.http.send(request)
        if response.status_code >= 400:
            body, truncated, original_chars = _bounded_body(response.text)
            raise OBSRequestError(
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
        success = data.get("success")
        if success is False or (
            isinstance(success, str) and success.lower() == "false"
        ):
            if (
                not _has_failure_reason(data)
                and endpoint == "filelist"
                and _has_empty_filelist_objects(data)
            ):
                return data
            if (
                not _has_failure_reason(data)
                and endpoint == "objectkeys"
                and _has_empty_objectkeys(data)
            ):
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

Keep both existing exception branches, retry classification, logging, and
backoff after the scope unchanged. The next loop iteration must acquire a new
reader scope.

- [ ] **Step 4: Update existing direct OBSClient constructions**

In `tests/test_obs_client.py` and the one direct construction in
`tests/test_scanner.py`, import `ScanPhaseCoordinator` and replace:

```python
request_semaphore=asyncio.Semaphore(N),
```

with:

```python
phase_coordinator=ScanPhaseCoordinator(N),
```

Remove `asyncio` imports only where this replacement makes them unused; keep
all other test behavior unchanged.

- [ ] **Step 5: Run OBSClient and focused scanner tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_obs_client.py tests/test_scanner.py -q
```

Expected: all tests pass, including existing retry counts/error detail and the
new scope-order/retry-pause tests.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/obs_scan_platform/obs_client.py tests/test_obs_client.py tests/test_scanner.py
git commit -m "feat: pause OBS attempts for aggregation"
```

---

### Task 3: Wire the global coordinator into scanner aggregation and finish delivery

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Create: `docs/superpowers/plans/2026-07-29-global-aggregation-request-barrier.md`

**Interfaces:**
- Consumes: `ScanPhaseCoordinator` and the updated `OBSClient` constructor.
- Produces: one coordinator shared by all application clients and both aggregation formats in the scan; one global writer scope around the existing format dispatch.

- [ ] **Step 1: Write failing scanner wiring and aggregation-scope tests**

Extend the existing AsyncClient-construction boundary test so the fake
`OBSClient` captures constructor kwargs, then assert:

```python
assert captured_obs_client_kwargs["phase_coordinator"] is scanner.phase_coordinator
```

Add a recording coordinator for aggregation tests:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RecordingAggregationCoordinator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def aggregation(self) -> AsyncIterator[None]:
        self.events.append("aggregation-scope-enter")
        try:
            yield
        finally:
            self.events.append("aggregation-scope-exit")
```

For the existing CSV scanner aggregation test, assign the recording
coordinator to the scanner, append `"csv-aggregate"` in the monkeypatched
`aggregate_bucket`, and assert:

```python
assert events == [
    "aggregation-scope-enter",
    "csv-aggregate",
    "aggregation-scope-exit",
]
```

For the Parquet branch, reuse the existing Parquet `_scan_bucket` fixture,
return a literal part path from the monkeypatched aggregator, and assert the
same scope order:

```python
events: list[str] = []
scanner.phase_coordinator = RecordingAggregationCoordinator(events)
scanner.config.scan.overview_format = "parquet"
part_path = tmp_path / application.appid / bucket.name / "part-00001.parquet"

def capture_parquet_aggregation(**kwargs):
    events.append("parquet-aggregate")
    return (part_path,)

monkeypatch.setattr(
    "obs_scan_platform.scanner.aggregate_bucket_parquet",
    capture_parquet_aggregation,
)

result = await scanner._scan_bucket(
    application,
    bucket,
    client,
    "run-1",
    tmp_path,
    scan_started_ms=1000,
)

assert events == [
    "aggregation-scope-enter",
    "parquet-aggregate",
    "aggregation-scope-exit",
]
assert result.overview_files == (part_path,)
```

- [ ] **Step 2: Run scanner tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q
```

Expected: failures show `Scanner` has no `phase_coordinator`, still passes
`request_semaphore` to `OBSClient`, and calls aggregators outside the recording
writer scope.

- [ ] **Step 3: Create and share one coordinator per Scanner**

In `Scanner.__init__`, replace the raw request semaphore with:

```python
self.phase_coordinator = ScanPhaseCoordinator(
    config.scan.global_request_concurrency
)
```

Pass the exact same instance to every application client:

```python
client = OBSClient(
    http=http,
    phase_coordinator=self.phase_coordinator,
    max_retries=self.config.scan.max_retries,
    retry_base_delay_seconds=self.config.scan.retry_base_delay_seconds,
    retry_max_delay_seconds=self.config.scan.retry_max_delay_seconds,
)
```

Remove the obsolete `self.request_semaphore` field and import
`ScanPhaseCoordinator` from the new module.

- [ ] **Step 4: Put the complete CSV/Parquet dispatch inside one writer scope**

Keep `phase_boundary` placement and both aggregator argument lists unchanged,
but wrap the existing format branch:

```python
phase_boundary = time.monotonic()
async with self.phase_coordinator.aggregation():
    if overview_format == "csv":
        aggregate_bucket(
            run_id=run_id,
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            temp_dir=temp_dir,
            output_path=csv_output_path,
            thresholds=thresholds,
            scan_started_ms=scan_started_ms,
            max_directories_in_memory=(
                self.config.scan.aggregation_max_directories_in_memory
            ),
            keep_temp_files=self.config.scan.keep_temp_files,
        )
        csv_path = csv_output_path
        overview_path = csv_output_path
        overview_files = (csv_output_path,)
    else:
        overview_files = aggregate_bucket_parquet(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            temp_dir=temp_dir,
            output_dir=parquet_output_dir,
            scan_started_ms=scan_started_ms,
            max_depth=self.config.scan.max_depth,
            file_type_map=self.config.scan.file_type_map,
            max_directories_in_memory=(
                self.config.scan.aggregation_max_directories_in_memory
            ),
            keep_temp_files=self.config.scan.keep_temp_files,
        )
        overview_path = parquet_output_dir
```

- [ ] **Step 5: Update end-to-end fakes and run behavior suites**

The end-to-end `FakeOBSClient` already accepts `**kwargs`; record the supplied
coordinator on each instance so a test with two applications can assert both
received the same object:

```python
self.phase_coordinator = kwargs["phase_coordinator"]
```

Keep the `Scanner` instance used to run the end-to-end test and assert every
fake client shares its coordinator:

```python
scanner = Scanner(config)
manifest = await scanner.run(run_id="run-1")

assert manifest["status"] == "success"
assert FakeOBSClient.instances
assert all(
    client.phase_coordinator is scanner.phase_coordinator
    for client in FakeOBSClient.instances
)
```

Then run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
```

Expected: all request coordination, scanner, CSV aggregation, Parquet
aggregation, and end-to-end tests pass.

- [ ] **Step 6: Document the fixed global phase behavior**

Update both READMEs and `docs/scan-start-guide.md` with these exact operational
facts:

- aggregation concurrency is fixed at one across every application and bucket
  in one scan run;
- when aggregation waits, no new request attempt or retry starts;
- admitted responses finish reading/parsing and current synchronous page
  processing before aggregation;
- all queued CSV/Parquet aggregations run serially, then paused requests resume;
- this is not a YAML option and does not coordinate independent processes.

- [ ] **Step 7: Update mandatory task and handoff records**

Record the task goal, branch `codex/parquet-overview`, before-task commit
`efc8bf9`, design commit `0087650`, implementation commits, RED/GREEN evidence,
review results, exact validation commands/results, known five-test Windows
baseline, live-scan limitation, push state, changed files, and exact resume
commands in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 8: Run fresh completion verification and inspect for unrelated changes or secrets**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat
git diff
```

Expected: the affected suites and compilation pass; the full suite has no new
failure beyond the five documented Windows baseline failures; the diff contains
only the approved coordinator, integrations, tests, and documentation and no
secret or machine-specific credential.

- [ ] **Step 9: Commit, review, correct findings, and push**

```powershell
git add .
git commit -m "feat: serialize aggregation with request draining"
git push -u origin HEAD
```

Expected: task and final reviews have no open Critical/Important finding,
commits are pushed to `origin/codex/parquet-overview`, local and remote HEAD are
equal, and the tracked working tree is clean.
