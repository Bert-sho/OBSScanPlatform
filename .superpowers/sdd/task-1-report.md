# Task 1 Report: Config Defaults and Objectkeys Concurrency Resolver

## TDD evidence

### RED

Command:

```bash
pytest tests/test_config.py -v
```

Observed result before implementation:

- 3 new tests failed as expected:
  - `test_scan_settings_new_concurrency_defaults`
  - `test_legacy_per_bucket_prefix_concurrency_still_sets_objectkeys_limit`
  - `test_new_objectkeys_concurrency_field_wins_over_legacy_field`
- Failure modes matched the missing implementation:
  - default `global_request_concurrency` was still `50` instead of `150`
  - `ScanSettings.objectkeys_concurrency_limit()` did not exist

### GREEN

Implementation:

- Added `objectkeys_concurrency_per_bucket` and `objectkeys_concurrency_limit()` to `src/obs_scan_platform/config.py`
- Raised the default `global_request_concurrency` to `150`
- Switched scanner objectkeys worker sizing to use the new resolver
- Updated the example YAML and scan-start guide
- Added the regression tests in `tests/test_config.py`

Validation:

```bash
pytest tests/test_config.py -v
pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v
pytest -q
```

Observed result after implementation:

- `pytest tests/test_config.py -v`: 9 passed
- `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`: 25 passed
- `pytest -q`: 84 passed, 1 warning

## Commands run

- `sed -n '1,260p' src/obs_scan_platform/config.py`
- `sed -n '1,260p' tests/test_config.py`
- `sed -n '1,220p' config/apps.example.yaml`
- `sed -n '1,260p' docs/scan-start-guide.md`
- `rg -n "per_bucket_prefix_concurrency|global_request_concurrency|objectkeys_concurrency|filelist_task_limit_per_bucket" src tests`
- `pytest tests/test_config.py -v`
- `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`
- `pytest -q`

## Output summary

- Config tests passed after the resolver and defaults were added.
- Scanner and end-to-end tests stayed green after switching the scanner to the new resolver.
- Full suite is green with one existing deprecation warning unrelated to this task.

## Files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `config/apps.example.yaml`
- `docs/scan-start-guide.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Self-review

- The change is tightly scoped to Task 1 and does not touch FastAPI request parameters or progress streaming.
- The legacy field is preserved for compatibility, and the new field wins when both are present.
- The scanner now consults one resolver method instead of reading the old field directly.
- No CSV schema changes were introduced.
- No empty-bucket failure behavior was softened.

## Concerns

- The new resolver centralizes a small policy decision. Future scan phases should use it instead of duplicating per-bucket concurrency logic.
- The full suite warning is pre-existing and not caused by this work.

## Review fix

- Review finding summary: `docs/current-task.md` and `docs/handoff.md` still described Task 1 as if it were waiting to be committed, even though commit `3ac8dc7c01f083756ba5105a17a702fdbfc44c65` already existed and had been pushed.
- Files changed: `docs/current-task.md`, `docs/handoff.md`
- Validation commands and result:
  - `/opt/homebrew/bin/git diff --check` -> passed
  - `rg -n "Pending the Task 1 commit|ready to commit|Commit and push the Task 1 changes" docs/current-task.md docs/handoff.md` -> no matches
