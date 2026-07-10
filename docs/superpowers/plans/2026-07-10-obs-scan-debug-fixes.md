# OBS Scan Debug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix empty bucket handling, shared bucket discovery, noisy request URL logging, and same-level `filelist` scan throughput.

**Architecture:** Keep the existing scanner architecture. Extend parsing helpers to understand documented OBS response shapes, treat empty `filelist` `objects={}` as a terminal empty listing, and run only same-level filelist directory tasks concurrently while each directory still paginates sequentially.

**Tech Stack:** Python 3.11+, asyncio, httpx, pydantic, pytest, pytest-asyncio.

## Global Constraints

- Use TDD: add failing tests before implementation.
- Keep changes surgical; do not refactor unrelated scanner flow.
- Do not log full OBS request URLs, query strings, request bodies, or tokens.
- Update `docs/current-task.md` and `docs/handoff.md` before completion.

---

### Task 1: Empty Filelist Response Handling

**Files:**
- Modify: `tests/test_obs_client.py`
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/obs_client.py`
- Modify: `src/obs_scan_platform/scanner.py`

**Interfaces:**
- Consumes: `OBSClient.get_json(url, params, headers=None, endpoint="filelist")`
- Produces: `_discover_root(...) -> RootDiscovery` that returns no prefixes and no metadata files when filelist reports an empty `objects={}` payload.

- [ ] **Step 1: Write failing tests**

Add tests proving `OBSClient` returns filelist empty-objects responses without raising, and `_scan_bucket` skips later filelist/objectkeys calls for that bucket shape.

- [ ] **Step 2: Run red tests**

Run:

```bash
C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_obs_client.py::test_get_json_returns_empty_filelist_objects_even_when_success_false tests/test_scanner.py::test_scan_bucket_treats_empty_filelist_objects_as_empty_bucket -q
```

Expected: both tests fail because empty `success=false` filelist responses raise and `objects` is not parsed as a terminal empty listing.

- [ ] **Step 3: Implement minimal code**

Add a small OBS-client predicate for `endpoint == "filelist"` and empty `objects` dict, and make scanner filelist parsing include documented `objects`.

- [ ] **Step 4: Run green tests**

Run the same command. Expected: both tests pass.

### Task 2: Shared Bucket Discovery Shapes

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/scanner.py`

**Interfaces:**
- Consumes: `_result_payload(data)` and bucket list dictionaries.
- Produces: `_list_buckets(...)` that scans owned buckets plus shared buckets when they are returned in separate list fields and `scan_shared_buckets` is true.

- [ ] **Step 1: Write failing test**

Add a test where `listbuckets` returns owned buckets in `buckets` and shared buckets in `sharedBuckets`.

- [ ] **Step 2: Run red test**

Run:

```bash
C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_scanner.py::test_list_buckets_includes_shared_bucket_lists_when_enabled -q
```

Expected: fail because `_items_from_payload` returns only one list.

- [ ] **Step 3: Implement minimal code**

Add a bucket-list helper that combines known bucket-list keys instead of returning only the first list.

- [ ] **Step 4: Run green test**

Run the same command. Expected: pass.

### Task 3: Same-Level Filelist Concurrency and Sanitized Logs

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/scanner.py`

**Interfaces:**
- Consumes: `FilelistDiscoveryScheduler.current_level()` and `record_folder/record_file/mark_completed`.
- Produces: `_discover_root(...)` that processes tasks from the same level concurrently, logs progress without full URLs, and preserves level boundaries.

- [ ] **Step 1: Write failing tests**

Add a concurrency test using `ConcurrentFakeClient` with multiple level-two directories and a log test that verifies no full request URL is emitted during filelist discovery.

- [ ] **Step 2: Run red tests**

Run:

```bash
C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_scanner.py::test_discover_root_processes_same_filelist_level_concurrently tests/test_scanner.py::test_discover_root_progress_logs_do_not_include_request_urls -q
```

Expected: concurrency test fails because level tasks are serial.

- [ ] **Step 3: Implement minimal code**

Extract per-directory filelist processing into an async helper. Use `asyncio.gather` for the current level only, protected by the existing global request semaphore in `OBSClient`; keep pagination inside each task sequential.

- [ ] **Step 4: Run green tests**

Run the same command. Expected: pass.

### Task 4: Validation, Handoff, Commit, Push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Produces: committed and pushed branch with clear resume instructions.

- [ ] **Step 1: Run focused tests**

```bash
C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

- [ ] **Step 2: Run full tests**

```bash
C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q
```

- [ ] **Step 3: Update handoff docs**

Record changed files, exact validation commands, pass/fail status, latest commit before and after this session, and resume instructions.

- [ ] **Step 4: Review, commit, and push**

```bash
git status
git diff --stat
git diff
git add .
git commit -m "fix: correct obs scan filelist edge cases"
git push -u origin HEAD
```
