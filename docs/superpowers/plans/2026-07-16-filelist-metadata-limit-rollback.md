# Filelist Metadata Limit Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound each bucket's cumulative metadata task count by rolling filelist discovery back to the previous complete BFS frontier when a configurable limit is exceeded.

**Architecture:** `FilelistDiscoveryScheduler` captures one level checkpoint before returning each BFS level. `Scanner._discover_root()` evaluates the effective cumulative metadata count after the whole level completes, restores the checkpoint on overflow, logs the rollback, and returns the restored frontier to the existing metadata/objectkeys phases.

**Tech Stack:** Python 3.11+, asyncio, Pydantic, pytest

## Global Constraints

- Add `scan.metadata_task_limit_per_bucket` with default `10000`.
- Exactly the configured limit is allowed; rollback only when the count is greater.
- Evaluate cumulative effective metadata tasks after each complete BFS level.
- Roll back the entire bucket level, never an individual branch or partial concurrent level.
- Use `/` as the sole objectkeys prefix when root itself exceeds the limit.
- Preserve request failure details, metadata concurrency, manifest schema, and CSV schema.

---

### Task 1: Configuration Contract

**Files:**
- Modify: `src/obs_scan_platform/config.py:8-23`
- Modify: `config/apps.example.yaml:1-17`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: YAML key `scan.metadata_task_limit_per_bucket`
- Produces: `ScanSettings.metadata_task_limit_per_bucket: int` with default `10000`

- [ ] **Step 1: Write failing configuration tests**

Extend the default-settings test and add an explicit-load test:

```python
# Add this assertion to test_scan_settings_new_concurrency_defaults:
assert config.scan.metadata_task_limit_per_bucket == 10000


def test_load_config_sets_metadata_task_limit_per_bucket(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  metadata_task_limit_per_bucket: 4321
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

    assert config.scan.metadata_task_limit_per_bucket == 4321
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py -k metadata_task_limit -q
```

Expected: FAIL because `ScanSettings` has no metadata-task limit field.

- [ ] **Step 3: Add the configuration field and example**

Add to `ScanSettings`:

```python
metadata_task_limit_per_bucket: int = 10000
```

Add to `config/apps.example.yaml`:

```yaml
scan:
  metadata_task_limit_per_bucket: 10000
```

- [ ] **Step 4: Run configuration tests to verify GREEN**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

### Task 2: Level Checkpoint and Rollback

**Files:**
- Modify: `src/obs_scan_platform/filelist_discovery.py`
- Modify: `src/obs_scan_platform/scanner.py:511-560`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `FilelistDiscoveryScheduler.current_level()`, folder/file/empty/failure events, and `ScanSettings.metadata_task_limit_per_bucket`
- Produces: `metadata_task_count: int`, `current_level_depth: int`, and `rollback_current_level() -> int`

- [ ] **Step 1: Write failing boundary tests**

Add scanner tests using the existing `FakeClient` and `make_scanner()` helpers:

```python
@pytest.mark.asyncio
async def test_discover_root_allows_metadata_count_equal_to_limit():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 2
    discovery = await scanner._discover_root(
        application,
        bucket,
        FakeClient(
            [
                {"result": {"files": [
                    {"objectType": "object", "objectKey": "root.txt"},
                    {"objectType": "folder", "objectKey": "alpha/"},
                ], "nextOffset": ""}},
                {"result": {"files": [
                    {"objectType": "object", "objectKey": "alpha/direct.txt"},
                ], "nextOffset": ""}},
            ]
        ),
    )
    assert discovery.prefixes == []
    assert discovery.metadata_files == ["root.txt", "alpha/direct.txt"]


@pytest.mark.asyncio
async def test_discover_root_overflow_uses_root_prefix():
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = FakeClient([{"result": {"files": [
        {"objectType": "object", "objectKey": "one.txt"},
        {"objectType": "object", "objectKey": "two.txt"},
        {"objectType": "folder", "objectKey": "alpha/"},
    ], "nextOffset": ""}}])
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
            {"result": {"files": [
                {"objectType": "object", "objectKey": "root.txt"},
                {"objectType": "folder", "objectKey": "alpha/"},
                {"objectType": "folder", "objectKey": "bravo/"},
            ], "nextOffset": ""}},
            {"result": {"files": [
                {"objectType": "object", "objectKey": "alpha/one.txt"},
                {"objectType": "object", "objectKey": "alpha/two.txt"},
                {"objectType": "folder", "objectKey": "alpha/child/"},
            ], "nextOffset": ""}},
            {"result": {"files": [
                {"objectType": "folder", "objectKey": "bravo/child/"},
            ], "nextOffset": ""}},
        ]
    )
    discovery = await scanner._discover_root(application, bucket, client)
    assert discovery.prefixes == ["alpha/", "bravo/"]
    assert discovery.metadata_files == ["root.txt"]
    assert [decode_request_body(call)["path"] for call in client.calls] == ["/", "/alpha/", "/bravo/"]
```

- [ ] **Step 2: Run boundary tests to verify RED**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py -k 'metadata_count_equal or overflow_restores or overflow_uses_root' -q
```

Expected: tests fail because discovery does not checkpoint or roll back levels.

- [ ] **Step 3: Add scheduler checkpoint state**

Add focused state and helpers to `FilelistDiscoveryScheduler`:

```python
_rollback_prefixes: set[str] = field(default_factory=set)
_rollback_direct_files: list[str] = field(default_factory=list)
_current_level_depth: int = 1

@property
def current_level_depth(self) -> int:
    return self._current_level_depth

def _metadata_files(self) -> list[str]:
    prefixes = sorted(self._discovered_prefixes)
    return [
        object_key
        for object_key in self._direct_files
        if not any(object_key.startswith(prefix) for prefix in prefixes)
    ]

@property
def metadata_task_count(self) -> int:
    return len(self._metadata_files())

def rollback_current_level(self) -> int:
    self._discovered_prefixes = set(self._rollback_prefixes)
    self._direct_files = list(self._rollback_direct_files)
    self._current_level = []
    self._next_level = []
    return len(self._discovered_prefixes)
```

When `current_level()` returns tasks, capture `self._discovered_prefixes` and
`self._direct_files`. Use `{ "/" }` as the rollback prefix set for the root
task. Record the task depth. Make `record_empty()` also remove the empty task's
prefix from `_rollback_prefixes`. Make `result()` reuse `_metadata_files()`.

- [ ] **Step 4: Enforce the limit after each complete level**

In `_discover_root()`, immediately after `_gather_cancel_on_error(...)`:

```python
metadata_task_count = scheduler.metadata_task_count
metadata_task_limit = max(1, self.config.scan.metadata_task_limit_per_bucket)
if metadata_task_count > metadata_task_limit:
    rejected_depth = scheduler.current_level_depth
    restored_prefix_total = scheduler.rollback_current_level()
    LOGGER.info(
        "filelist metadata limit rollback appid=%s bucket=%s depth=%s "
        "metadata_tasks=%s limit=%s prefixes=%s",
        application.appid,
        bucket.name,
        rejected_depth,
        metadata_task_count,
        metadata_task_limit,
        restored_prefix_total,
    )
    break
```

- [ ] **Step 5: Run boundary and scanner tests to verify GREEN**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py -k 'metadata_count_equal or overflow_restores or overflow_uses_root or child_filelist_failure or whole_level' -q
```

Expected: all selected scheduler tests pass.

### Task 3: Empty/Failure Semantics, Logging, and Bucket Integration

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: finalized `RootDiscovery(prefixes, metadata_files)` after rollback
- Produces: one rollback INFO record and existing metadata/objectkeys phases using bounded discovery output

- [ ] **Step 1: Add edge and integration tests**

Add tests that assert:

```python
@pytest.mark.asyncio
async def test_discover_root_rollback_excludes_empty_prefix_and_logs(caplog):
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = FakeClient(
        [
            {"result": {"files": [
                {"objectType": "folder", "objectKey": "alpha/"},
                {"objectType": "folder", "objectKey": "empty/"},
            ], "nextOffset": ""}},
            {"result": {"files": [
                {"objectType": "object", "objectKey": "alpha/one.txt"},
                {"objectType": "object", "objectKey": "alpha/two.txt"},
            ], "nextOffset": ""}},
            {"result": {"files": [], "nextOffset": ""}},
        ]
    )
    with caplog.at_level(logging.INFO, logger="obs_scan_platform.scanner"):
        discovery = await scanner._discover_root(application, bucket, client)

    assert discovery.prefixes == ["alpha/"]
    assert any(
        "filelist metadata limit rollback appid=app.one bucket=bucket-name-1 "
        "depth=2 metadata_tasks=2 limit=1 prefixes=1" in record.getMessage()
        for record in caplog.records
    )


class RootOverflowBucketClient:
    def __init__(self) -> None:
        self.phase_events: list[str] = []
        self.objectkeys_prefixes: list[str] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        del url, headers
        self.phase_events.append(endpoint)
        if endpoint == "bucket_endpoint":
            return {"result": "http://bucket-endpoint/"}
        if endpoint == "filelist":
            return {"result": {"files": [
                {"objectType": "object", "objectKey": "one.txt"},
                {"objectType": "object", "objectKey": "two.txt"},
            ], "nextOffset": ""}}
        if endpoint == "metadata":
            raise AssertionError("metadata must be skipped after root rollback")
        if endpoint == "objectkeys":
            prefix = base64.urlsafe_b64decode(params["objectkey"]).decode("utf-8")
            self.objectkeys_prefixes.append(prefix)
            return {"result": {"objectkeys": [
                {"objectKey": "one.txt", "size": "1", "lastModifyTime": "1000"},
                {"objectKey": "two.txt", "size": "2", "lastModifyTime": "1000"},
            ], "truncated": "false"}}
        raise AssertionError(endpoint)


@pytest.mark.asyncio
async def test_scan_bucket_root_overflow_skips_metadata_and_scans_root_prefix(tmp_path):
    scanner, application, bucket = make_scanner()
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    client = RootOverflowBucketClient()

    result = await scanner._scan_bucket(
        application,
        bucket,
        client,
        "run-1",
        tmp_path,
        scan_started_ms=1000,
    )

    assert result.status == ScanStatus.SUCCESS
    assert client.phase_events == ["bucket_endpoint", "filelist", "objectkeys"]
    assert client.objectkeys_prefixes == ["/"]


class MetadataLimitFailureClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_json(self, url, *, params, headers=None, endpoint="unknown"):
        del url, headers, endpoint
        request_body = decode_request_body({"params": params})
        path = request_body["path"]
        self.calls.append(path)
        if path == "/":
            return {"result": {"files": [
                {"objectType": "folder", "objectKey": "bad/"},
                {"objectType": "folder", "objectKey": "noisy/"},
            ], "nextOffset": ""}}
        if path == "/bad/":
            raise detailed_request_error("filelist", "directory unavailable")
        return {"result": {"files": [
            {"objectType": "object", "objectKey": "noisy/one.txt"},
            {"objectType": "object", "objectKey": "noisy/two.txt"},
        ], "nextOffset": ""}}


@pytest.mark.asyncio
async def test_metadata_limit_rollback_preserves_filelist_failure():
    scanner, application, bucket = make_scanner()
    scanner.config.defaults.filelist_depth = 2
    scanner.config.scan.metadata_task_limit_per_bucket = 1
    partial_errors = PartialErrorSummary()

    discovery = await scanner._discover_root(
        application,
        bucket,
        MetadataLimitFailureClient(),
        partial_errors=partial_errors,
    )

    assert discovery.prefixes == ["bad/", "noisy/"]
    assert discovery.metadata_files == []
    assert partial_errors.to_manifest()["filelist_failed_dirs"] == 1
```

These tests use the existing base64, logging, partial-error, and request-body
helpers already imported by `tests/test_scanner.py`.

- [ ] **Step 2: Run edge tests to verify RED or confirm Task 2 coverage**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -k 'metadata_limit or root_overflow or rollback' -q
```

Expected: new logging/integration assertions fail until all Task 2 behavior is connected; already-covered assertions may pass and remain as regression coverage.

- [ ] **Step 3: Document configuration and rollback semantics**

Add this operator-facing behavior to README:

```markdown
`scan.metadata_task_limit_per_bucket` defaults to `10000`. After each complete
filelist BFS level, the scanner checks the bucket's cumulative metadata task
count. If it exceeds the limit, discovery rolls the whole bucket back to the
previous frontier; root overflow uses `/` as one objectkeys prefix.
```

Update the scheduler paragraph in CLAUDE.md to state:

```markdown
Before each BFS level, the scheduler checkpoints the current prefix frontier
and metadata candidates. A completed level that exceeds
`metadata_task_limit_per_bucket` is rolled back as a whole; `/` is the root
fallback. Empty directories are removed from the checkpoint and failed
directories remain frontier prefixes.
```

Do not add manifest fields.

- [ ] **Step 4: Run relevant suites**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all relevant tests pass.

### Task 4: Review, Verification, Handoff, and Push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: reviewed implementation, exact test output, and Git state
- Produces: self-contained handoff, conventional commit, and pushed branch

- [ ] **Step 1: Request independent code review**

Review configuration compatibility, checkpoint copying, cumulative effective
counting, root `/`, empty/failure behavior, concurrent whole-level determinism,
and test fidelity. Fix every Critical and Important finding before proceeding.

- [ ] **Step 2: Run completion verification**

Run the relevant suites and then:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

Record exact pass/fail counts and distinguish the six known Windows baseline
failures from regressions.

- [ ] **Step 3: Update mandatory handoff files**

Record task status, branch, before/after commits, decisions, changed files,
exact commands/results, known risks, uncommitted state, and resume instructions
in both mandatory documents.

- [ ] **Step 4: Inspect, commit, and push**

Run `git status`, `git diff --stat`, `git diff`, and `git diff --check`. Inspect
for unrelated changes, secrets, and machine-specific paths. Stage intended
files, commit with `feat: bound metadata tasks during filelist discovery`, push
with `git push -u origin HEAD`, then verify local/remote hash parity and a clean
worktree.
