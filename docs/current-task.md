# Current Task

## Current task title

Correct and verify the metadata `bucketId` query contract

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The metadata-contract change is ready for controller review. This remains `wip` because the repository retains five known Windows full-suite baseline failures.

## User goal

Send the bucket internal ID to `/rest/boto3/s3/object/metadata` using `bucketId`, preserve `bucketid` as the bucket-name field, and leave the objectkeys endpoint's `bucketld` contract unchanged.

## Completed work

- Renamed and strengthened the focused metadata regression test to assert `bucketid`, `bucketId`, and absence of `bucketld`.
- Verified RED: the new assertion failed because `params.get("bucketId")` was `None` before production code changed.
- Changed only the metadata request parameter in `Scanner._collect_metadata_files` from `bucketld` to `bucketId`.
- Updated the end-to-end fake metadata endpoint to enforce the corrected contract; its objectkeys assertions still use `bucketld`.
- Added the implementation plan and recorded this self-contained handoff.

## Remaining work

- Controller review, final verification, and push only. The implementer has not pushed.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/superpowers/plans/2026-07-28-metadata-bucket-id.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_uses_correct_bucket_query_fields_and_writes_csv -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
```

## Validation result

- RED command failed as intended: `params.get("bucketId")` was `None`, expected `'bucket-id-1'`.
- Focused regression: `1 passed in 0.31s` after the fix.
- Relevant scanner and end-to-end suites: `96 passed in 1.68s`.
- Full suite: `215 passed, 5 failed, 1 warning in 4.28s`; all five failures match the documented Windows baseline.
- `compileall` passed.
- The repository's five pre-existing Windows full-suite failures remain a known baseline and must be rechecked by the controller before marking the task complete.

## Known risks

- The real external metadata API contract is verified here through the outbound request boundary; no live API integration run was performed.
- The five pre-existing Windows portability/environment failures still prevent a full-suite-green task status.

## Next recommended action

Review the local commit, run final verification including the full suite if required, then push `codex/obs-scan-platform` from the controller session.
