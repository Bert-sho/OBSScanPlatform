# Bucket Phase Timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-bucket request-stage and local-processing wall-clock durations to `manifest.json` while preserving total elapsed timing.

**Architecture:** Record one monotonic bucket start, one boundary after all remote collection completes, and one final timestamp. Store the two derived phase durations on `BucketScanResult` and serialize them beside the existing `elapsed_seconds`.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest

## Global Constraints

- Use monotonic wall-clock durations, not summed individual HTTP request durations.
- Keep existing `started_*`, `ended_*`, and `elapsed_seconds` fields unchanged.
- Add only per-bucket `request_elapsed_seconds` and `processing_elapsed_seconds`.
- Do not change status, error, CSV, temp retention, or concurrency behavior.

---

### Task 1: Add phase timing to successful and failed bucket results

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/models.py`
- Modify: `src/obs_scan_platform/scanner.py`

**Interfaces:**
- Consumes: `time.monotonic()` at bucket start, collection/processing boundary, and finish
- Produces: `BucketScanResult.request_elapsed_seconds: float` and `processing_elapsed_seconds: float`

- [x] **Step 1: Write failing timing tests**

Update the deterministic success test to use monotonic values `[10.0, 12.0, 13.25]` and assert:

```python
assert result.elapsed_seconds == 3.25
assert result.request_elapsed_seconds == 2.0
assert result.processing_elapsed_seconds == 1.25
```

Update the bucket-endpoint failure test to assert its existing `1.5` seconds are request time and processing time is `0.0`. Add a processing failure test that monkeypatches `aggregate_bucket` to raise after the boundary and asserts both measured phases.

- [x] **Step 2: Run focused tests to verify RED**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py -k 'timing or endpoint_request_failure or processing_failure' -q
```

Expected: failures because phase fields do not exist and the success path currently consumes only two monotonic timestamps.

- [x] **Step 3: Add model fields and measure phases**

Add these defaulted fields to `BucketScanResult`:

```python
request_elapsed_seconds: float = 0.0
processing_elapsed_seconds: float = 0.0
```

In `_scan_bucket()`, keep `phase_boundary: float | None = None`; set it from `time.monotonic()` after `_collect_prefixes()` and before `aggregate_bucket()`. At every return, use one final monotonic timestamp and derive:

```python
request_elapsed_seconds = ended - bucket_started if phase_boundary is None else phase_boundary - bucket_started
processing_elapsed_seconds = 0.0 if phase_boundary is None else ended - phase_boundary
elapsed_seconds = ended - bucket_started
```

Populate the same fields in the outer unexpected bucket wrapper using its full elapsed duration as request time and `0.0` processing time.

- [x] **Step 4: Run focused tests to verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

### Task 2: Serialize and document phase timing

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `README.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: phase fields from `BucketScanResult`
- Produces: bucket manifest keys `request_elapsed_seconds` and `processing_elapsed_seconds`

- [x] **Step 1: Write failing manifest serialization assertions**

Add explicit manifest assertions for both fields and end-to-end non-negative assertions. Preserve the existing total timing assertions.

- [x] **Step 2: Run serialization tests to verify RED**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty tests/test_scan_end_to_end.py::test_scanner_run_completes_with_mocked_obs_and_directory_csv -q
```

Expected: failures because the manifest does not serialize the new fields.

- [x] **Step 3: Serialize fields and update documentation**

Add both values in `_bucket_result_to_manifest()` beside `elapsed_seconds`. Document the collection/processing boundary, failure semantics, and the fact that concurrent bucket durations must not be summed as run wall-clock time.

- [x] **Step 4: Run relevant validation**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_models.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all selected tests pass.

- [x] **Step 5: Review, verify, commit, and push**

Request independent review, update mandatory handoff documents, run fresh relevant validation, inspect `git status`, `git diff --stat`, and `git diff`, commit with `feat: split bucket phase timing`, then `git push -u origin HEAD`.
