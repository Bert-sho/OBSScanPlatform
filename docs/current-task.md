# Current Task

## Current task title

Task 3 review fix: avoid overlapping prefix object scans

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Fix the Task 3 recursive filelist discovery so final `RootDiscovery.prefixes` contains only non-overlapping objectkeys scan targets. Directories that have discovered child directory prefixes should not also be returned as objectkeys prefixes, because objectkeys prefix scans are recursive and would double-count child objects.

## Completed work

- Added a red regression by changing the depth-2 recursive discovery expectation so expanded parent prefix `alpha/` is not returned with child prefix `alpha/beta/`.
- Updated the task-limit regression so expanded parent `alpha/` is excluded while unexpanded boundary prefixes `alpha/beta/` and `bravo/` are retained.
- Changed `_discover_root()` to track discovered directory prefixes separately from parent prefixes that have child directory prefixes.
- Returned `discovered_prefixes - parent_prefixes` as sorted final objectkeys scan prefixes.
- Preserved root file metadata behavior and non-root object filtering behavior.
- Kept leaf directories and depth/task-limit boundary directories as final objectkeys prefixes.
- Did not modify `config.py`.

## Remaining work

None for this repair.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

- Red: `pytest tests/test_scanner.py -k 'discover_root_recurses_to_filelist_depth or discover_root_limits_recursive_filelist_tasks' -v`
- Green required: `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`
- Full suite: `pytest -q`
- Final status: `/opt/homebrew/bin/git status --short --branch`

## Validation result

- Red run failed before the fix: `2 failed, 17 deselected in 0.14s`; both failures showed `alpha/` was still present in final prefixes.
- Green required run after the fix: `20 passed in 0.16s`.
- Full suite after the fix: `75 passed, 1 warning in 0.40s`.
- Warning: existing Starlette deprecation warning from `fastapi.testclient` importing `httpx`.

## Known risks

- If a directory has both direct object files and child directories, returning only child directory prefixes avoids duplicate recursive scans but does not add a separate direct-files-only collection mode. This follows the requested simpler approach and avoids expanding scope.
- The handoff records the previous Task 3 implementation commit `4a404aa355f599ad874c2e94ab21cbbe0b077730` as supplemented by this repair commit.

## Next recommended action

Review the pushed repair commit or continue with Task 4.
