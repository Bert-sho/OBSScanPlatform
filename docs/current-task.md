# Current Task

## Current task title

Task 3: Metadata Per-Object Partial Fallback

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Add per-object metadata fallback so a single metadata failure is recorded as partial, the remaining metadata files still write to CSV, and `_collect_metadata_files` accepts an optional `partial_errors` parameter.

## Completed work

- Added a focused regression test that forces one metadata object to fail while the others continue to write.
- Threaded an optional `PartialErrorSummary` through `_collect_metadata_files()`.
- Recorded per-object metadata failures as partial errors and logged the failure without aborting the rest of the metadata collection.
- Kept the metadata worker concurrency and CSV writing behavior unchanged for successful objects.

## Remaining work

- None for Task 3.
- Later tasks still need objectkeys fallback integration and the bucket-level final status handling.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `docs/current-task.md`
- `docs/handoff.md`
- `.superpowers/sdd/task-3-report.md`

## Validation commands run

- `& 'C:\\Users\\lzh\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_records_failure_and_keeps_other_files -q`
- `& 'C:\\Users\\lzh\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_records_failure_and_keeps_other_files tests/test_scanner.py::test_collect_metadata_files_uses_bucket_name_as_bucketid_and_writes_csv tests/test_scanner.py::test_collect_metadata_files_processes_all_files_with_bounded_workers -q`

## Validation result

- The first focused run failed as expected before the scanner change because `_collect_metadata_files()` did not accept `partial_errors`.
- After the fix, the focused metadata suite passed: `3 passed in 0.41s`.

## Known risks

- This task only covers metadata fallback. Objectkeys fallback behavior is intentionally left for later tasks.
- The new partial-error threading is only populated from metadata paths in this task.

## Next recommended action

Start the next approved fallback task and extend partial-error plumbing into objectkeys collection.
