# Temporary File Retention Flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scan.keep_temp_files` delete or retain every bucket temporary directory regardless of terminal scan status.

**Architecture:** Keep retention centralized in `Scanner._bucket_result_to_manifest()`. Remove the status condition from cleanup so the existing boolean alone determines deletion and manifest exposure.

**Tech Stack:** Python 3.11+, Pydantic configuration, pytest

## Global Constraints

- Keep the existing `scan.keep_temp_files: bool = False` configuration and default.
- `false` removes temporary directories for `success`, `partial_failed`, and `failed`.
- `true` retains temporary directories for all three statuses and includes `temp_dir` in the manifest.
- Do not change scan status, final CSV, error reporting, or `_tmp` layout.

---

### Task 1: Make retention independent of bucket status

**Files:**
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/scanner.py`

**Interfaces:**
- Consumes: `Scanner.config.scan.keep_temp_files`, `BucketScanResult.status`, and optional `temp_dir`
- Produces: manifest with retained `temp_dir` only when `keep_temp_files` is `true`

- [x] **Step 1: Write the failing parameterized test**

Replace the status-specific cleanup assertions with parameterized coverage equivalent to:

```python
@pytest.mark.parametrize("status", [ScanStatus.SUCCESS, ScanStatus.PARTIAL_FAILED, ScanStatus.FAILED])
def test_bucket_manifest_deletes_temp_dir_for_every_status_when_retention_disabled(tmp_path, status):
    scanner, _, bucket = make_scanner()
    scanner.config.scan.keep_temp_files = False
    temp_dir = tmp_path / status.value
    temp_dir.mkdir()
    result = BucketScanResult(
        appid="app.one",
        bucket_name=bucket.name,
        bucket_id=bucket.bucket_id,
        status=status,
        csv_path=None,
        thresholds=scanner.config.defaults,
    )
    manifest = scanner._bucket_result_to_manifest(result, temp_dir)
    assert not temp_dir.exists()
    assert "temp_dir" not in manifest
```

Add the matching `keep_temp_files = True` parameterized test asserting all directories remain and `manifest['temp_dir'] == str(temp_dir)`.

- [x] **Step 2: Run the focused tests to verify RED**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py -k 'bucket_manifest_temp_dir' -q
```

Expected: failed/partial-failed deletion cases fail because current code retains their directories.

- [x] **Step 3: Implement the minimum cleanup change**

Change the retention branch to:

```python
if not self.config.scan.keep_temp_files:
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    return manifest
manifest["temp_dir"] = str(temp_dir)
```

- [x] **Step 4: Run the focused tests to verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

### Task 2: Document and validate the behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Produces: explicit user-facing all-status retention semantics and cross-machine handoff state

- [x] **Step 1: Update user documentation**

State in both user guides that `keep_temp_files: false` deletes `_tmp` files for successful, partially failed, and failed buckets, while `true` preserves all of them.

- [x] **Step 2: Run relevant validation**

Run:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all selected tests pass.

- [x] **Step 3: Request code review and address findings**

Review against the approved design, especially all three statuses, missing temp directories, manifest exposure, and unchanged configuration defaults.

- [x] **Step 4: Update mandatory handoff documents**

Record the current branch, starting commit `0a98f37`, exact validation results, changed files, risks, and resume steps.

- [x] **Step 5: Verify, commit, and push**

Inspect `git status`, `git diff --stat`, and `git diff`; run fresh relevant validation; commit with `feat: control temp retention for all scan statuses`; push with `git push -u origin HEAD`.
