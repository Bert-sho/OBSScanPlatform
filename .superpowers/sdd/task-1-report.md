# Task 1 Report: Partial Error Model And Manifest Output

## Summary

Implemented the partial error model and bucket-manifest serialization required for
Task 1 only. No later filelist, metadata, or objectkeys fallback behavior was added.

## RED checkpoint

Focused test command run before implementation:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty -q
```

Observed result:

- `ImportError: cannot import name 'PartialErrorSummary' from 'obs_scan_platform.models'`

That failure confirmed the missing model surface the task was targeting.

## GREEN checkpoint

Focused validation after implementation:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty tests/test_scanner.py::test_bucket_manifest_temp_dir_cleanup_and_retention -q
```

Observed result:

- `3 passed in 0.36s`

## Changed files

- `src/obs_scan_platform/models.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `docs/current-task.md`
- `docs/handoff.md`

## What changed

- Added `PartialErrorSample` and `PartialErrorSummary`.
- Added `partial_errors` to `BucketScanResult`.
- Added manifest serialization for `partial_errors` when the summary has recorded
  failures.
- Added the two brief-provided tests covering summary counters, capped samples, and
  manifest output.

## Self-review

- The change is intentionally small and stays within the Task 1 scope.
- The new summary records endpoint-specific counts and bounded samples, which matches
  the brief and keeps the manifest output stable.
- The manifest code only emits `partial_errors` when there is at least one recorded
  failure, so empty summaries do not add noise.

## Concerns

- This task does not implement the later failure-recording behavior in the filelist,
  metadata, or objectkeys scan paths.
- The scanner import for `PartialErrorSummary` is intentionally present to match the
  brief, even though Task 1 does not yet use the type in scan execution flow.

## Review fix addendum

### Summary

Sanitized `PartialErrorSummary.record()` so `partial_errors.samples[].reason` no
longer preserves raw request URLs or credential-style query text.

### RED checkpoint

Focused regression command:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_partial_error_summary_redacts_urls_and_credential_query_text tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty -q
```

Observed result:

- `test_partial_error_summary_redacts_urls_and_credential_query_text` failed because
  the sample reason still contained `http://obs.example` / `https://obs.example`
  text before sanitization.

### GREEN checkpoint

Focused validation after the sanitizer fix:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_partial_error_summary_counts_and_caps_samples tests/test_scanner.py::test_partial_error_summary_redacts_urls_and_credential_query_text tests/test_scanner.py::test_bucket_manifest_includes_partial_errors_and_keeps_error_empty tests/test_scanner.py::test_bucket_manifest_temp_dir_cleanup_and_retention -q
```

Observed result:

- `4 passed in 0.42s`

### Changed files

- `src/obs_scan_platform/models.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `docs/current-task.md`
- `docs/handoff.md`
- `.superpowers/sdd/task-1-report.md`

### Concerns

- Sanitization currently targets URLs and common credential query keys. If later
  tasks record different secret shapes, the redaction rules may need to grow.
