# Current Task

## Current task title

Apply `_tmp` retention flag to every bucket status

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Use the existing configuration flag to control temporary files consistently: `scan.keep_temp_files: false` deletes `_tmp` files for `success`, `partial_failed`, and `failed` buckets; `true` retains all of them.

## Completed work

- Confirmed the existing `scan.keep_temp_files` flag and default `false` already exist.
- Approved and committed the design specification.
- Added a TDD regression matrix for all three terminal statuses and both flag values.
- Added coverage for a missing temporary directory when retention is disabled.
- Removed the bucket-status condition from `_bucket_result_to_manifest()` cleanup.
- Updated README and the Chinese scan guide with the all-status semantics.

## Remaining work

- None.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `README.md`
- `docs/scan-start-guide.md`
- `docs/superpowers/specs/2026-07-13-temp-retention-flag-design.md`
- `docs/superpowers/plans/2026-07-13-temp-retention-flag.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py -k 'bucket_manifest_' -q
```

## Validation result

- Baseline: `75 passed in 1.29s`.
- RED: `3 failed, 5 passed, 60 deselected`, proving failed/partial-failed cleanup and missing-directory manifest behavior were incorrect.
- Focused GREEN: `8 passed, 60 deselected in 0.45s`.
- Relevant suite after implementation: `81 passed in 1.26s`.
- Independent review validation: `81 passed in 1.17s`; no code findings.
- Final verification: `81 passed in 1.19s`.

## Known risks

- With the default `false`, diagnostic temporary CSVs from failed and partially failed buckets are now intentionally removed. Operators who need those files must set `keep_temp_files: true` before scanning.
- Existing Windows-wide test portability failures from the previous task remain outside this change; task verification uses the scanner/config/end-to-end suites.

## Next recommended action

Use `keep_temp_files: true` for scans where failed-bucket diagnostic CSVs must be retained.
