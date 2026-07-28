# Current Task

## Current task title

Correct and verify the objectkeys `bucketId` query contract

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The objectkeys correction is implemented, reviewed, and pushed. Status remains `wip` solely because the full suite retains five pre-existing Windows failures.

## User goal

Send the bucket internal ID to `/rest/boto3/s3/list/bucket/objectkeys` as `bucketId`, retain `bucketid` as the bucket-name field, leave metadata request construction unchanged, and preserve pagination and all other objectkeys fields.

## Completed work

- Added the focused outbound-boundary regression assertion for `bucketid`, `bucketId`, and absence of `bucketld`.
- Verified RED: the focused test failed with `params.get("bucketId")` equal to `None` before the production fix.
- Changed only `Scanner._collect_prefix`'s objectkeys internal-ID key from `bucketld` to `bucketId`.
- Updated every objectkeys request-boundary assertion in the scanner and end-to-end tests, including owned and shared buckets; metadata assertions and request construction remain unchanged.
- Preserved objectkeys pagination (`nextmarker`) and all non-ID query fields unchanged.
- Included the existing plan at `docs/superpowers/plans/2026-07-28-objectkeys-bucket-id.md`.
- Completed task-level review and final review with no Critical or Important findings; the single stale-status Minor is corrected in the final documentation update.
- Pushed `codex/obs-scan-platform` through `84450c5368b3cd1e51d585a77aa5e2060f70c53f`.

## Remaining work

- No objectkeys-contract work remains.
- Address the five unrelated Windows portability/environment failures in a separate task before marking the repository fully green.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/superpowers/plans/2026-07-28-objectkeys-bucket-id.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_prefix_stops_when_truncated_string_false -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

## Validation result

- RED command: failed as intended with `assert None == 'bucket-id-1'` at `params.get("bucketId")`.
- Fresh GREEN focused regression: `1 passed in 0.41s`.
- Fresh relevant scanner and end-to-end suites: `96 passed in 1.66s`.
- Fresh full suite: `215 passed, 5 failed, 1 warning in 4.50s`; failures are the documented Windows baseline.
- `compileall` passed.
- `git diff --check eccf3fe..84450c5` passed.
- Final review found no Critical or Important issues; its only non-blocking stale-status Minor is corrected here.

## Known risks

- The external objectkeys API is validated at the outbound request boundary only; no live service run was performed.
- Full-suite status remains blocked by two Windows symlink-privilege failures plus CRLF, backslash-path, and CLI path-separator portability failures.

## Next recommended action

Create a separate Windows portability task for the five baseline failures; that is the only remaining prerequisite for repository status `completed`.
