# Current Task

## Current task title

Task 1: Config Defaults and Objectkeys Concurrency Resolver

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Implement the Task 1 config changes for the OBS scan platform:

- set `scan.global_request_concurrency` default to `150`
- set per-bucket objectkeys concurrency default to `30`
- keep `scan.per_bucket_prefix_concurrency` accepted for legacy YAML configs
- add `ScanSettings.objectkeys_concurrency_limit()`
- update sample config and operator docs

## Completed work

- Added three config regression tests covering the new defaults, legacy compatibility, and precedence between new and old fields.
- Implemented `ScanSettings.objectkeys_concurrency_limit()` in `src/obs_scan_platform/config.py`.
- Updated `ScanSettings` defaults so `global_request_concurrency` defaults to `150`.
- Added the new `objectkeys_concurrency_per_bucket` field while preserving `per_bucket_prefix_concurrency`.
- Switched scanner objectkeys worker sizing to use the new resolver.
- Updated `config/apps.example.yaml` to show the new recommended settings.
- Updated `docs/scan-start-guide.md` to explain the new per-bucket objectkeys concurrency guidance.

## Remaining work

- None for Task 1.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `config/apps.example.yaml`
- `docs/scan-start-guide.md`
- `docs/handoff.md`
- `.superpowers/sdd/task-1-report.md`

## Validation commands run

- `pytest tests/test_config.py -v`
- `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`
- `pytest -q`

## Validation result

- `pytest tests/test_config.py -v`: 9 passed
- `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`: 25 passed
- `pytest -q`: 84 passed, 1 warning

## Known risks

- `per_bucket_prefix_concurrency` is still accepted, but the new resolver now centralizes the decision in `ScanSettings`. If future scan phases need different per-bucket concurrency, they should use the resolver rather than the raw field.
- The full suite reported one existing `StarletteDeprecationWarning`; it did not block the task.

## Next recommended action

- Commit and push the Task 1 changes, then pick up the next approved task in the plan.
